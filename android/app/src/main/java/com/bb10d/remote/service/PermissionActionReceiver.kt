package com.bb10d.remote.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.bb10d.remote.RemoteApp
import com.bb10d.remote.data.SessionRef
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Allow / Deny pressed on a "needs permission" notification.
 *
 * The answer goes straight to the daemon, so a command can be approved from
 * the lock screen without opening the app. If it cannot be delivered (no
 * network, the request was already answered elsewhere) the notification is
 * replaced by one that says so and opens the session: a tap that silently did
 * nothing would leave the turn blocked with nobody aware of it.
 */
class PermissionActionReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION) return
        val ref = SessionRef.parse(intent.getStringExtra(EXTRA_REF).orEmpty()) ?: return
        val jobId = intent.getStringExtra(EXTRA_JOB).orEmpty()
        val requestId = intent.getStringExtra(EXTRA_REQUEST).orEmpty()
        val allow = intent.getBooleanExtra(EXTRA_ALLOW, false)
        if (jobId.isBlank() || requestId.isBlank()) return

        val app = context.applicationContext
        val pending = goAsync()
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
            try {
                val client = RemoteApp.get(app).repository.client(ref.profileId)
                val error = if (client == null) {
                    "This daemon is no longer set up on the phone."
                } else {
                    // A receiver has about ten seconds before the system
                    // gives up on it.
                    withTimeoutOrNull(8_000) {
                        runCatching { client.answerPermission(jobId, requestId, allow) }
                            .exceptionOrNull()
                            ?.let { it.message ?: "Could not reach the daemon." }
                            ?: ""
                    } ?: "The daemon did not answer in time."
                }
                if (error.isEmpty()) {
                    Notifier.clearBlocked(app, ref)
                } else {
                    Notifier.answerFailed(app, ref, error)
                }
            } finally {
                pending.finish()
            }
        }
    }

    companion object {
        const val ACTION = "com.bb10d.remote.action.ANSWER_PERMISSION"
        const val EXTRA_REF = "ref"
        const val EXTRA_JOB = "job"
        const val EXTRA_REQUEST = "request"
        const val EXTRA_ALLOW = "allow"
    }
}
