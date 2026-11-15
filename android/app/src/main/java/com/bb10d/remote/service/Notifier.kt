package com.bb10d.remote.service

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.bb10d.remote.MainActivity
import com.bb10d.remote.R
import com.bb10d.remote.RemoteApp
import com.bb10d.remote.data.SessionRef

/**
 * Notifications are the reason this app is worth using over SSH: a turn can
 * take twenty minutes, and the phone should say so instead of being polled.
 *
 * Three kinds, deliberately distinct:
 *  - one ongoing, silent summary while turns run (also the foreground-service
 *    notification that keeps the status streams connected);
 *  - a high-priority alert when the agent is *blocked* on the user;
 *  - a normal alert when a turn finishes.
 */
object Notifier {

    private const val ID_ONGOING = 1
    private const val ID_BLOCKED_BASE = 1000
    private const val ID_DONE_BASE = 2000

    fun canPost(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED

    fun ongoing(context: Context, running: Int, detail: String): Notification =
        NotificationCompat.Builder(context, RemoteApp.CHANNEL_PROGRESS)
            .setSmallIcon(R.drawable.ic_stat_agent)
            .setContentTitle(
                if (running == 1) "1 turn running" else "$running turns running",
            )
            .setContentText(detail)
            .setOngoing(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(openApp(context, null))
            .build()

    fun blocked(
        context: Context,
        ref: SessionRef,
        title: String,
        what: String,
        vibrate: Boolean,
        permission: PermissionAsk? = null,
    ) {
        if (!canPost(context)) return
        // A permission names its tool and command: that is what is being
        // decided, so it leads; the prompt that started the turn follows.
        val body = permission?.summary?.takeIf { it.isNotBlank() }
            ?.let { "$it\n\n$title" } ?: title
        val builder = NotificationCompat.Builder(context, RemoteApp.CHANNEL_ALERTS)
            .setSmallIcon(R.drawable.ic_stat_agent)
            .setContentTitle(what)
            .setContentText(permission?.summary?.takeIf { it.isNotBlank() } ?: title)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            // The channel's own vibration is a user-facing OS setting; this
            // only silences what the app itself asks for.
            .setSilent(!vibrate)
            .setContentIntent(openApp(context, ref))
        if (permission != null && permission.requestId.isNotBlank()) {
            // Answer without opening the app. Deny first: the button nearest
            // the edge should not be the one that lets a command run.
            builder.addAction(0, "Deny", answerIntent(context, ref, permission, allow = false))
            builder.addAction(0, "Allow", answerIntent(context, ref, permission, allow = true))
        }
        manager(context)?.notify(blockedId(ref), builder.build())
    }

    /** What a permission notification is asking, and how to answer it. */
    data class PermissionAsk(
        val jobId: String,
        val requestId: String,
        val summary: String,
    )

    /** The answer could not be delivered: say so, and offer the app. */
    fun answerFailed(context: Context, ref: SessionRef, reason: String) {
        if (!canPost(context)) return
        val notification = NotificationCompat.Builder(context, RemoteApp.CHANNEL_ALERTS)
            .setSmallIcon(R.drawable.ic_stat_agent)
            .setContentTitle("Answer not sent")
            .setContentText(reason.ifBlank { "Open the session to answer." })
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(openApp(context, ref))
            .build()
        manager(context)?.notify(blockedId(ref), notification)
    }

    private fun blockedId(ref: SessionRef) = ID_BLOCKED_BASE + ref.key.hashCode().and(0xFF)

    private fun answerIntent(
        context: Context,
        ref: SessionRef,
        ask: PermissionAsk,
        allow: Boolean,
    ): PendingIntent {
        val intent = Intent(context, PermissionActionReceiver::class.java).apply {
            action = PermissionActionReceiver.ACTION
            putExtra(PermissionActionReceiver.EXTRA_REF, ref.key)
            putExtra(PermissionActionReceiver.EXTRA_JOB, ask.jobId)
            putExtra(PermissionActionReceiver.EXTRA_REQUEST, ask.requestId)
            putExtra(PermissionActionReceiver.EXTRA_ALLOW, allow)
        }
        // One PendingIntent per (session, answer): the request code separates
        // Allow from Deny, UPDATE_CURRENT refreshes the extras for a new ask.
        val code = ref.key.hashCode() * 2 + (if (allow) 1 else 0)
        return PendingIntent.getBroadcast(
            context,
            code,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    fun clearBlocked(context: Context, ref: SessionRef) {
        manager(context)?.cancel(blockedId(ref))
    }

    fun finished(
        context: Context,
        ref: SessionRef,
        title: String,
        status: String,
        error: String,
    ) {
        if (!canPost(context)) return
        val ok = status == "done"
        val notification = NotificationCompat.Builder(context, RemoteApp.CHANNEL_ALERTS)
            .setSmallIcon(R.drawable.ic_stat_agent)
            .setContentTitle(
                when (status) {
                    "done" -> "Turn finished"
                    "stopped" -> "Turn stopped"
                    "error" -> "Turn failed"
                    // Unknown / empty / still-running blips: never call this "failed".
                    else -> "Turn finished"
                },
            )
            .setContentText(if (ok || error.isBlank()) title else error)
            .setStyle(
                NotificationCompat.BigTextStyle()
                    .bigText(if (ok || error.isBlank()) title else "$title\n\n$error"),
            )
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(openApp(context, ref))
            .build()
        manager(context)?.notify(ID_DONE_BASE + ref.key.hashCode().and(0xFF), notification)
    }

    fun ongoingId() = ID_ONGOING

    private fun manager(context: Context): NotificationManager? =
        context.getSystemService(NotificationManager::class.java)

    private fun openApp(context: Context, ref: SessionRef?): PendingIntent {
        val intent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            if (ref != null) putExtra(MainActivity.EXTRA_OPEN_SESSION, ref.key)
        }
        return PendingIntent.getActivity(
            context,
            ref?.key?.hashCode() ?: 0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }
}
