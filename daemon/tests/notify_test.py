#!/usr/bin/env python3
"""notify.Notifier: one alert per state change, none for what was already
there at start, none for guests, chained turns or short finished turns; and
the ntfy / webhook requests it actually sends."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentremoted import notify  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("ok" if cond else "FAIL", name,
                           (" — " + str(detail)) if detail and not cond else ""))
    if not cond:
        failures.append(name)


class Cfg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Job:
    def __init__(self, jid, **kw):
        self.id = jid
        self.lock = threading.Lock()
        self.session_id = "sess-" + jid
        self.new_session_id = ""
        self.provider = "claude"
        self.cwd = "/home/u/projects/desk"
        self.account = "main"
        self.status = "running"
        self.watch = False
        self.next_job_id = ""
        self.started_at = 0.0
        self.error = ""
        self.pending_permission = None
        self.pending_question = None
        self.__dict__.update(kw)


class Mgr:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}


class Bundle:
    def __init__(self):
        self.jobs = Mgr()


class Focus:
    def title(self, key):
        return "Fix the login flow" if key == "sess-a" else ""


bundle = Bundle()
sent = []
clock = [1000.0]
cfg = Cfg(notify_ntfy_url="https://ntfy.example/topic-x", notify_detail=False)
n = notify.Notifier(cfg, {"claude": bundle}, focus=Focus(),
                    send=sent.append, now=lambda: clock[0])

old = Job("old", status="done")
blocked_before = Job("pre", pending_question={"request_id": 1, "questions": []})
bundle.jobs.jobs = {"old": old, "pre": blocked_before}
n.poll()
check("first pass announces nothing that was already there", sent == [], sent)

a = Job("a", started_at=1000.0)
bundle.jobs.jobs["a"] = a
n.poll()
check("a running turn is silent", sent == [])

a.pending_permission = {"request_id": 7, "tool_name": "Bash",
                        "detail": "npm test"}
n.poll()
check("permission gate alerts once", len(sent) == 1
      and sent[0]["state"] == "needs_answer"
      and sent[0]["title"] == "Claude wants permission", sent)
check("default message names only folder and host, not the command",
      "npm test" not in sent[0]["message"] and "desk" in sent[0]["message"],
      sent[0]["message"])
n.poll()
check("the same gate does not repeat", len(sent) == 1)

a.pending_permission = None
n.poll()
check("answering it is silent", len(sent) == 1)
a.pending_question = {"request_id": 2,
                      "questions": [{"question": "Which approach?"}]}
n.poll()
check("a later question alerts again", len(sent) == 2
      and sent[1]["title"] == "Claude has a question", sent)

a.pending_question = None
a.status = "done"
clock[0] = 1010.0
n.poll()
check("a turn shorter than notify_finished_min_s finishes silently",
      len(sent) == 2, sent)

b = Job("b", started_at=1010.0, provider="copilot")
bundle.jobs.jobs["b"] = b
n.poll()
b.status = "done"
clock[0] = 1100.0
n.poll()
check("a long turn alerts when it finishes", len(sent) == 3
      and sent[2]["state"] == "turn_finished"
      and sent[2]["title"] == "Copilot finished", sent)

c = Job("c", started_at=1100.0)
bundle.jobs.jobs["c"] = c
n.poll()
c.status = "done"
c.next_job_id = "d"
clock[0] = 1300.0
n.poll()
check("a turn with a queued follow-up is not 'finished'", len(sent) == 3)

d = Job("d", started_at=1300.0)
bundle.jobs.jobs["d"] = d
n.poll()
d.status = "error"
d.error = "rate limited"
n.poll()
check("a failed turn alerts", len(sent) == 4 and sent[3]["state"] == "failed")

e = Job("e", started_at=1300.0)
bundle.jobs.jobs["e"] = e
n.poll()
e.status = "stopped"
n.poll()
check("a turn stopped by hand is silent", len(sent) == 4)

g = Job("g", account="alice", started_at=1300.0)
bundle.jobs.jobs["g"] = g
n.poll()
g.pending_question = {"request_id": 1, "questions": []}
n.poll()
check("a guest's session never reaches the host's alerts", len(sent) == 4)

del bundle.jobs.jobs["a"]
n.poll()
check("evicted jobs are forgotten", "a" not in n._seen)

# notify_on narrows the states; notify_detail adds title and the ask.
sent2 = []
n2 = notify.Notifier(
    Cfg(notify_webhook_url="http://x", notify_on=["needs_answer"],
        notify_detail=True),
    {"claude": bundle}, focus=Focus(), send=sent2.append, now=lambda: 5000.0)
n2.poll()
a2 = Job("a", started_at=0.0)
bundle.jobs.jobs["a"] = a2
n2.poll()
a2.pending_permission = {"request_id": 1, "tool_name": "Bash",
                         "detail": "rm -rf build"}
n2.poll()
a2.pending_permission = None
a2.status = "done"
n2.poll()
check("notify_on filters states", [m["state"] for m in sent2] == ["needs_answer"],
      sent2)
check("notify_detail adds the title and the ask",
      "Fix the login flow" in sent2[0]["message"]
      and "rm -rf build" in sent2[0]["message"], sent2[0]["message"])

check("disabled without a target",
      not notify.Notifier(Cfg(), {}).enabled)

# Real HTTP: what ntfy and the webhook receive.
got = []


class Sink(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        got.append((self.path, self.headers.get("Authorization"),
                    json.loads(body.decode("utf-8"))))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Sink)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_port
n3 = notify.Notifier(
    Cfg(notify_ntfy_url=base + "/my-topic", notify_ntfy_token="tk_1",
        notify_webhook_url=base + "/hook", notify_click_url="https://app/x"),
    {}, focus=None)
errors = n3._send_http({"state": "needs_answer", "title": "Claude 有问题",
                        "message": "desk on host", "provider": "claude",
                        "session_id": "s", "job_id": "j", "host": "host",
                        "cwd_name": "desk", "kind": "question"})
srv.shutdown()
check("both targets delivered", errors == [] and len(got) == 2, (errors, got))
ntfy = next((g for g in got if g[0] == "/"), None)
check("ntfy: JSON publish to the server root with the topic",
      ntfy is not None and ntfy[2].get("topic") == "my-topic"
      and ntfy[2].get("title") == "Claude 有问题"
      and ntfy[2].get("priority") == 4
      and ntfy[2].get("click") == "https://app/x", ntfy)
check("ntfy: bearer token sent", ntfy is not None and ntfy[1] == "Bearer tk_1")
hook = next((g for g in got if g[0] == "/hook"), None)
check("webhook: the event itself", hook is not None
      and hook[2].get("state") == "needs_answer"
      and hook[2].get("session_id") == "s", hook)

n4 = notify.Notifier(Cfg(notify_ntfy_url="http://127.0.0.1:9/t"), {})
check("an unreachable server is an error, not an exception",
      len(n4.send_test()) == 1)

print("FAIL" if failures else "all notify checks passed")
sys.exit(1 if failures else 0)
