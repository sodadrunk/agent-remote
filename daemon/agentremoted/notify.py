"""Tell someone who is not looking that a session wants them.

Every client learns about a blocked or finished turn from the status stream,
which only reaches a client that is open. A phone in a pocket hears nothing.
This module is the daemon's own outbound alert: one message when a turn

  * needs an answer   (a question or a tool permission is waiting),
  * failed, or
  * finished          (and nothing is queued behind it),

posted to an ntfy topic (https://ntfy.sh, or a self-hosted server) and/or a
plain webhook. It is off until `notify_ntfy_url` or `notify_webhook_url` is
set in config.json.

The watcher polls the job managers rather than hooking every place a gate is
opened: the states are already derivable from a job (the same rule Focus
uses, focus.state_for), and a poll cannot miss a path a hook forgot.

Privacy: a push leaves this machine and may sit on someone else's server, so
by default it names only the harness, the project folder and the host. Set
`notify_detail` to add the session title and what is being asked.
"""

import json
import logging
import os
import platform
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from . import focus as focus_store

log = logging.getLogger("agentremoted.notify")

POLL_S = 1.5
HTTP_TIMEOUT_S = 6.0

STATE_NEEDS_ANSWER = focus_store.STATE_NEEDS_ANSWER
STATE_FAILED = focus_store.STATE_FAILED
STATE_TURN_FINISHED = focus_store.STATE_TURN_FINISHED
ALL_STATES = (STATE_NEEDS_ANSWER, STATE_FAILED, STATE_TURN_FINISHED)

# ntfy priority (1-5) and tag (emoji shortcode) per state.
_NTFY_STYLE = {
    STATE_NEEDS_ANSWER: (4, "raising_hand"),
    STATE_FAILED: (4, "x"),
    STATE_TURN_FINISHED: (3, "white_check_mark"),
}

_PROVIDER_LABEL = {
    "claude": "Claude", "grok": "Grok", "codex": "Codex",
    "deepseek": "DeepSeek", "cursor": "Cursor", "copilot": "Copilot",
    "codebuddy": "CodeBuddy",
}


def _cfg(config, name, default=None):
    try:
        value = getattr(config, name)
    except AttributeError:
        return default
    return default if value is None else value


def snapshot(job) -> dict:
    """What the watcher needs from a job, read under its lock."""
    with job.lock:
        perm = job.pending_permission or None
        question = job.pending_question or None
        gate_id = ""
        gate_kind = ""
        gate_detail = ""
        if question:
            gate_kind = "question"
            gate_id = "q:%s" % question.get("request_id", "")
            qs = question.get("questions") or []
            if qs and isinstance(qs[0], dict):
                gate_detail = str(qs[0].get("question")
                                  or qs[0].get("header") or "")
        elif perm:
            gate_kind = "permission"
            gate_id = "p:%s" % perm.get("request_id", "")
            gate_detail = " ".join(
                str(x) for x in (perm.get("tool_name"), perm.get("detail"))
                if x)
        return {
            "job_id": job.id,
            "session_id": job.new_session_id or job.session_id or "",
            "provider": job.provider or "",
            "cwd": job.cwd or "",
            "account": getattr(job, "account", "main") or "main",
            "status": job.status,
            "watch": bool(getattr(job, "watch", False)),
            "queued_next": bool(job.next_job_id),
            "started_at": job.started_at,
            "error": job.error or "",
            "gate_kind": gate_kind,
            "gate_id": gate_id,
            "gate_detail": gate_detail,
        }


def event_for(snap: dict) -> tuple:
    """(state, dedupe_key) this job is in right now, or ("", "").

    The key changes when there is something new to say: a second question in
    the same job has a new request id, so it alerts again; the same gate seen
    on the next poll does not.
    """
    if snap["gate_id"]:
        return STATE_NEEDS_ANSWER, snap["gate_id"]
    status = snap["status"]
    if snap["watch"]:
        return "", ""           # a watch job runs no turn of its own
    if status == "error":
        return STATE_FAILED, "failed"
    if status == "done" and not snap["queued_next"]:
        return STATE_TURN_FINISHED, "finished"
    return "", ""               # running, stopped by hand, or chained on


class Notifier:
    """Polls every job manager and posts one message per state change."""

    def __init__(self, config, bundles, focus=None, send=None, now=time.time):
        self.config = config
        self.bundles = bundles or {}
        self.focus = focus
        self._send = send or self._send_http
        self._now = now
        self._seen = {}         # job_id -> last dedupe key acted on
        self._primed = False
        self._stop = threading.Event()
        self._thread = None
        self._last_error_log = 0.0

    # ---- configuration ----------------------------------------------

    @property
    def ntfy_url(self) -> str:
        return str(_cfg(self.config, "notify_ntfy_url", "") or "").strip()

    @property
    def webhook_url(self) -> str:
        return str(_cfg(self.config, "notify_webhook_url", "") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.ntfy_url or self.webhook_url)

    def _states(self) -> tuple:
        raw = _cfg(self.config, "notify_on", None)
        if not isinstance(raw, (list, tuple)) or not raw:
            return ALL_STATES
        return tuple(s for s in ALL_STATES if s in raw)

    # ---- lifecycle ---------------------------------------------------

    def start(self):
        if self._thread is not None or not self.enabled:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="notify")
        self._thread.start()
        targets = [t for t, on in (("ntfy", self.ntfy_url),
                                   ("webhook", self.webhook_url)) if on]
        log.info("notifications on (%s) for: %s", ", ".join(targets),
                 ", ".join(self._states()))

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(POLL_S):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - never let alerts kill the thread
                log.exception("notify poll failed")

    # ---- the watcher -------------------------------------------------

    def _jobs(self) -> list:
        out = []
        for bundle in self.bundles.values():
            mgr = getattr(bundle, "jobs", None)
            if mgr is None:
                continue
            with mgr.lock:
                out.extend(mgr.jobs.values())
        return out

    def poll(self) -> list:
        """One pass. Returns the messages sent (for tests)."""
        sent = []
        live = set()
        states = self._states()
        min_s = float(_cfg(self.config, "notify_finished_min_s", 30) or 0)
        for job in self._jobs():
            snap = snapshot(job)
            jid = snap["job_id"]
            live.add(jid)
            state, key = event_for(snap)
            if self._seen.get(jid) == key:
                continue
            self._seen[jid] = key
            # The first pass only records where things stand: a daemon
            # restart must not re-announce every job it rehydrated.
            if not self._primed or not state or state not in states:
                continue
            # The host's own alerts must not carry a guest's sessions.
            if snap["account"] != "main":
                continue
            if (state == STATE_TURN_FINISHED
                    and self._now() - snap["started_at"] < min_s):
                continue        # a turn that short was being watched
            msg = self.message(state, snap)
            self._send(msg)
            sent.append(msg)
        self._primed = True
        for jid in [j for j in self._seen if j not in live]:
            del self._seen[jid]
        return sent

    # ---- the message -------------------------------------------------

    def message(self, state: str, snap: dict) -> dict:
        provider = snap["provider"]
        who = _PROVIDER_LABEL.get(provider, provider.capitalize() or "Agent")
        folder = os.path.basename(snap["cwd"].rstrip("/")) or snap["cwd"]
        host = platform.node().split(".")[0]
        if state == STATE_NEEDS_ANSWER:
            what = ("wants permission" if snap["gate_kind"] == "permission"
                    else "has a question")
        elif state == STATE_FAILED:
            what = "failed"
        else:
            what = "finished"
        title = "%s %s" % (who, what)
        where = " on ".join(x for x in (folder, host) if x)
        lines = [where] if where else []
        if _cfg(self.config, "notify_detail", False):
            name = ""
            if self.focus is not None and snap["session_id"]:
                try:
                    name = self.focus.title(snap["session_id"])
                except Exception:  # noqa: BLE001
                    name = ""
            if name:
                lines.insert(0, name)
            extra = snap["gate_detail"] if state == STATE_NEEDS_ANSWER else (
                snap["error"] if state == STATE_FAILED else "")
            if extra:
                lines.append(" ".join(str(extra).split())[:200])
        return {
            "state": state,
            "title": title,
            "message": "\n".join(lines) or title,
            "provider": provider,
            "session_id": snap["session_id"],
            "job_id": snap["job_id"],
            "host": host,
            "cwd_name": folder,
            "kind": snap["gate_kind"],
        }

    # ---- delivery ----------------------------------------------------

    def _post(self, url: str, payload: dict, token: str = ""):
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "User-Agent": "agentremoted"}
        if token:
            headers["Authorization"] = "Bearer %s" % token
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            resp.read()

    def _ntfy_payload(self, msg: dict) -> tuple:
        """(server_url, json) for ntfy's JSON publish.

        `notify_ntfy_url` is the topic URL people copy from the ntfy app
        (https://ntfy.sh/my-topic). Publishing as JSON to the server root
        keeps non-ASCII titles intact, which HTTP headers would not.
        """
        parts = urlsplit(self.ntfy_url)
        path = parts.path.rstrip("/")
        base, _, topic = path.rpartition("/")
        server = urlunsplit((parts.scheme, parts.netloc, base or "/", "", ""))
        priority, tag = _NTFY_STYLE.get(msg["state"], (3, "bell"))
        payload = {
            "topic": topic,
            "title": msg["title"],
            "message": msg["message"],
            "priority": priority,
            "tags": [tag],
        }
        click = str(_cfg(self.config, "notify_click_url", "") or "").strip()
        if click:
            payload["click"] = click
        return server, payload

    def _send_http(self, msg: dict):
        errors = []
        if self.ntfy_url:
            try:
                server, payload = self._ntfy_payload(msg)
                self._post(server, payload, str(
                    _cfg(self.config, "notify_ntfy_token", "") or ""))
            except (OSError, ValueError, urllib.error.URLError) as e:
                errors.append("ntfy: %s" % e)
        if self.webhook_url:
            try:
                self._post(self.webhook_url, msg)
            except (OSError, ValueError, urllib.error.URLError) as e:
                errors.append("webhook: %s" % e)
        if errors:
            # An unreachable push server during a long outage would otherwise
            # write one warning per state change.
            now = time.time()
            if now - self._last_error_log > 60:
                self._last_error_log = now
                log.warning("notification not delivered (%s)",
                            "; ".join(errors))
        return errors

    def send_test(self) -> list:
        """Post a sample message now. Returns a list of delivery errors."""
        msg = {
            "state": STATE_TURN_FINISHED,
            "title": "Agent Remote test",
            "message": "Notifications from %s are working."
                       % platform.node().split(".")[0],
            "provider": "", "session_id": "", "job_id": "",
            "host": platform.node().split(".")[0], "cwd_name": "", "kind": "",
        }
        self._last_error_log = 0.0
        return self._send_http(msg) or []
