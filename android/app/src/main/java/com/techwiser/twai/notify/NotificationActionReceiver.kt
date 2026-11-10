package com.techwiser.twai.notify

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.RemoteInput
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.ReminderIn
import kotlinx.coroutines.launch

/** Buttons on notifications: mark read, snooze a reminder, reply to the assistant. */
class NotificationActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val app = TwApp.from(context)
        val id = intent.getStringExtra(EXTRA_ID)
        when (intent.action) {
            MARK_READ -> {
                if (id == null) return
                NotificationManagerCompat.from(context).cancel(Notifier.serverId(id))
                val pending = goAsync()
                app.scope.launch {
                    try {
                        runCatching { app.api.markRead(id) }
                    } finally {
                        pending.finish()
                    }
                }
            }
            SNOOZE -> {
                if (id == null) return
                val text = intent.getStringExtra(EXTRA_TEXT).orEmpty().ifBlank { "Reminder" }
                NotificationManagerCompat.from(context).cancel(Notifier.serverId(id))
                val pending = goAsync()
                app.scope.launch {
                    try {
                        runCatching {
                            app.api.addReminder(ReminderIn("in 10 minutes", text.take(500)))
                            app.api.markRead(id)
                        }
                    } finally {
                        pending.finish()
                    }
                }
            }
            REPLY -> {
                val thread = intent.getStringExtra(EXTRA_THREAD) ?: return
                val text = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(Notifier.KEY_REPLY)
                    ?.toString()?.trim().orEmpty()
                if (text.isEmpty()) return
                Notifier.chatWorking(context, thread, text)
                ReplyWorker.start(context, thread, text)
            }
        }
    }

    companion object {
        const val MARK_READ = "com.techwiser.twai.MARK_READ"
        const val SNOOZE = "com.techwiser.twai.SNOOZE"
        const val REPLY = "com.techwiser.twai.REPLY"
        const val EXTRA_ID = "id"
        const val EXTRA_TEXT = "text"
        const val EXTRA_THREAD = "thread"
    }
}

/** Sends a reply typed in the notification shade, then shows the answer there. */
class ReplyWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val thread = inputData.getString("thread") ?: return Result.success()
        val text = inputData.getString("text") ?: return Result.success()
        val api = TwApp.from(applicationContext).api
        val out = try {
            api.chat(thread, text)
        } catch (e: Exception) {
            Notifier.chatReply(applicationContext, thread, "Couldn't send: ${e.message ?: "network error"}", asked = text)
            return Result.success()
        }
        when {
            out.confirm != null -> Notifier.confirmNeeded(applicationContext, thread, out.confirm.title)
            out.browser != null -> Notifier.confirmNeeded(applicationContext, thread, "The assistant needs you in the live browser.")
            out.outcome == "flagged_nsfw" -> Notifier.chatReply(applicationContext, thread, "That message was blocked.", asked = text)
            else -> Notifier.chatReply(applicationContext, thread, out.reply.ifBlank { "Done." }, asked = text)
        }
        return Result.success()
    }

    companion object {
        fun start(context: Context, thread: String, text: String) {
            val request = OneTimeWorkRequestBuilder<ReplyWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setInputData(Data.Builder().putString("thread", thread).putString("text", text).build())
                .build()
            WorkManager.getInstance(context).enqueue(request)
        }
    }
}
