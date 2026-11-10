package com.techwiser.twai.notify

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.Person
import androidx.core.app.RemoteInput
import androidx.core.content.ContextCompat
import com.techwiser.twai.R
import com.techwiser.twai.data.ApprovalOut
import com.techwiser.twai.data.NotificationOut
import com.techwiser.twai.ui.MainActivity

/** Android notifications: channels, and one builder per kind of news. */
object Notifier {
    const val CH_REMINDERS = "reminders"
    const val CH_CHAT = "chat"
    const val CH_APPROVALS = "approvals"
    const val CH_UPDATES = "updates"
    const val CH_GENERAL = "general"

    const val KEY_REPLY = "reply_text"
    private const val GROUP_UPDATES = "tw.updates"

    fun createChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java)
        fun channel(id: String, name: Int, desc: Int?, importance: Int) =
            NotificationChannel(id, context.getString(name), importance).apply {
                if (desc != null) description = context.getString(desc)
            }
        manager.createNotificationChannels(listOf(
            channel(CH_REMINDERS, R.string.channel_reminders, R.string.channel_reminders_desc, NotificationManager.IMPORTANCE_HIGH),
            channel(CH_CHAT, R.string.channel_chat, R.string.channel_chat_desc, NotificationManager.IMPORTANCE_HIGH),
            channel(CH_APPROVALS, R.string.channel_approvals, R.string.channel_approvals_desc, NotificationManager.IMPORTANCE_HIGH),
            channel(CH_UPDATES, R.string.channel_updates, R.string.channel_updates_desc, NotificationManager.IMPORTANCE_DEFAULT),
            channel(CH_GENERAL, R.string.channel_general, null, NotificationManager.IMPORTANCE_LOW),
        ))
    }

    fun canPost(context: Context): Boolean =
        Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    private fun post(context: Context, id: Int, notification: android.app.Notification) {
        if (!canPost(context)) return
        try {
            NotificationManagerCompat.from(context).notify(id, notification)
        } catch (e: SecurityException) {
            // Permission revoked between the check and the post.
        }
    }

    /** Opens the app at a deep link (twai://...), each link with its own PendingIntent. */
    fun open(context: Context, link: String): PendingIntent {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(link), context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        return PendingIntent.getActivity(context, link.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    private fun action(context: Context, action: String, code: Int, extras: Intent.() -> Unit, mutable: Boolean = false): PendingIntent {
        val intent = Intent(context, NotificationActionReceiver::class.java).setAction(action).apply(extras)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (mutable) PendingIntent.FLAG_MUTABLE else PendingIntent.FLAG_IMMUTABLE)
        return PendingIntent.getBroadcast(context, code, intent, flags)
    }

    private fun base(context: Context, channel: String) = NotificationCompat.Builder(context, channel)
        .setSmallIcon(R.drawable.ic_stat_notify)
        .setColor(0xFF2563EB.toInt())
        .setAutoCancel(true)

    // ---- the server's notifications (the bell): reminders, digests, jobs, shares, alerts ----
    fun serverId(id: String): Int = ("n:$id").hashCode()

    fun serverNotification(context: Context, n: NotificationOut) {
        val id = serverId(n.id)
        val isReminder = n.kind == "reminder"
        val builder = base(context, if (isReminder) CH_REMINDERS else CH_UPDATES)
            .setContentTitle(if (isReminder) n.title else n.title.ifBlank { "Update" })
            .setContentText(if (isReminder) n.body else n.body.lineSequence().firstOrNull().orEmpty())
            .setStyle(NotificationCompat.BigTextStyle().bigText(n.body.take(4000)))
            .setContentIntent(open(context, "twai://alerts"))
            .setCategory(if (isReminder) NotificationCompat.CATEGORY_REMINDER else NotificationCompat.CATEGORY_STATUS)
            .setPriority(if (isReminder) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
            .addAction(0, "Mark read", action(context, NotificationActionReceiver.MARK_READ, id, {
                putExtra(NotificationActionReceiver.EXTRA_ID, n.id)
            }))
        if (isReminder) {
            builder.setSubText(kindLabel(n.kind))
            builder.addAction(0, "Snooze 10 min", action(context, NotificationActionReceiver.SNOOZE, id + 1, {
                putExtra(NotificationActionReceiver.EXTRA_ID, n.id)
                putExtra(NotificationActionReceiver.EXTRA_TEXT, n.title)
            }))
        } else {
            builder.setGroup(GROUP_UPDATES).setSubText(kindLabel(n.kind))
        }
        post(context, id, builder.build())
    }

    private fun kindLabel(kind: String) = when (kind) {
        "reminder" -> "Reminder"
        "digest" -> "Mail digest"
        "job" -> "Background job"
        "share" -> "Shared with you"
        "alert" -> "Server alert"
        "mail" -> "Mail"
        else -> kind.replaceFirstChar { it.uppercase() }
    }

    fun cancelServer(context: Context, id: String) {
        NotificationManagerCompat.from(context).cancel(serverId(id))
    }

    // ---- chat ----
    fun chatId(thread: String): Int = ("c:$thread").hashCode()

    private val assistant = Person.Builder().setName("TW-ai").setBot(true).setKey("assistant").build()
    private val me = Person.Builder().setName("You").setKey("me").build()

    /** "Your reply is ready", with a Reply box that answers from the notification shade. */
    fun chatReply(context: Context, thread: String, text: String, asked: String? = null) {
        val id = chatId(thread)
        val style = NotificationCompat.MessagingStyle(me)
        if (asked != null) style.addMessage(asked, System.currentTimeMillis() - 1, me)
        style.addMessage(plain(text).take(2000), System.currentTimeMillis(), assistant)
        val reply = RemoteInput.Builder(KEY_REPLY).setLabel("Reply").build()
        val replyAction = NotificationCompat.Action.Builder(0, "Reply",
            action(context, NotificationActionReceiver.REPLY, id, {
                putExtra(NotificationActionReceiver.EXTRA_THREAD, thread)
            }, mutable = true))
            .addRemoteInput(reply)
            .setAllowGeneratedReplies(true)
            .setSemanticAction(NotificationCompat.Action.SEMANTIC_ACTION_REPLY)
            .build()
        val n = base(context, CH_CHAT)
            .setStyle(style)
            .setContentTitle("TW-ai")
            .setContentText(plain(text).take(200))
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setContentIntent(open(context, "twai://chat/${Uri.encode(thread)}"))
            .addAction(replyAction)
            .build()
        post(context, id, n)
    }

    fun cancelChat(context: Context, thread: String) {
        NotificationManagerCompat.from(context).cancel(chatId(thread))
    }

    /** Shown while a reply from the notification shade is being written. */
    fun chatWorking(context: Context, thread: String, asked: String) {
        val style = NotificationCompat.MessagingStyle(me).addMessage(asked, System.currentTimeMillis(), me)
        val n = base(context, CH_CHAT)
            .setStyle(style)
            .setContentTitle("TW-ai")
            .setContentText("Working on it…")
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setProgress(0, 0, true)
            .setContentIntent(open(context, "twai://chat/${Uri.encode(thread)}"))
            .build()
        post(context, chatId(thread), n)
    }

    fun confirmNeeded(context: Context, thread: String, title: String) {
        val n = base(context, CH_CHAT)
            .setContentTitle("Needs your go-ahead")
            .setContentText(title)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(open(context, "twai://chat/${Uri.encode(thread)}"))
            .addAction(0, "Review", open(context, "twai://chat/${Uri.encode(thread)}"))
            .build()
        post(context, chatId(thread), n)
    }

    // ---- work ----
    fun approval(context: Context, a: ApprovalOut) {
        val who = a.subject_name.ifBlank { "Someone" }
        val n = base(context, CH_APPROVALS)
            .setContentTitle("Approval needed: ${a.tool.replace('_', ' ')}")
            .setContentText("$who · ${a.department}")
            .setStyle(NotificationCompat.BigTextStyle().bigText("$who (${a.department}) asked: ${a.message.take(600)}"))
            .setCategory(NotificationCompat.CATEGORY_EVENT)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(open(context, "twai://work"))
            // Approving runs payments, orders and mail: only inside the app (behind the app lock).
            .addAction(0, "Review", open(context, "twai://work"))
            .build()
        post(context, ("a:${a.id}").hashCode(), n)
    }

    fun decision(context: Context, thread: String, state: String) {
        val text = when (state) {
            "approved" -> "Your request was approved. The result will appear in the chat."
            "denied" -> "Your request was declined by the approver."
            else -> "Your request was updated: $state."
        }
        val link = if (thread.isNotBlank()) "twai://chat/${Uri.encode(thread)}" else "twai://chats"
        val n = base(context, CH_APPROVALS)
            .setContentTitle(if (state == "approved") "Approved" else if (state == "denied") "Declined" else "Request update")
            .setContentText(text)
            .setContentIntent(open(context, link))
            .build()
        post(context, ("d:$thread:$state").hashCode(), n)
    }

    fun signedOut(context: Context) {
        val n = base(context, CH_GENERAL)
            .setContentTitle("Signed out")
            .setContentText("Sign in again to keep getting reminders and replies.")
            .setContentIntent(open(context, "twai://chats"))
            .build()
        post(context, 1, n)
    }

    fun cancelAll(context: Context) {
        NotificationManagerCompat.from(context).cancelAll()
    }

    /** Markdown to plain text, for notification previews. */
    fun plain(markdown: String): String = markdown
        .replace(Regex("```[\\s\\S]*?```"), "[code]")
        .replace(Regex("!?\\[([^\\]]*)]\\([^)]*\\)"), "$1")
        .replace(Regex("[*_`#>]+"), "")
        .replace(Regex("\\n{3,}"), "\n\n")
        .trim()
}
