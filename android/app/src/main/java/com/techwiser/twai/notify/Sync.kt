package com.techwiser.twai.notify

import android.content.Context
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.AuthState
import com.techwiser.twai.data.SignedOutException
import kotlinx.coroutines.delay
import java.util.concurrent.TimeUnit

/** Turns what's new on the server into phone notifications. Never throws. */
object Sync {
    private const val TAG = "TwSync"

    suspend fun handle(context: Context, data: Map<String, String>) {
        val app = TwApp.from(context)
        if (app.api.auth.value != AuthState.SIGNED_IN) return
        try {
            when (data["type"]) {
                "chat_done", "chat_failed" -> chatFinished(context, data["thread"].orEmpty(), data["type"] == "chat_done")
                "approval" -> approvals(context)
                "approval_decided" -> Notifier.decision(context, data["thread"].orEmpty(), data["state"].orEmpty())
                else -> notifications(context)  // "notification", or a periodic check
            }
        } catch (e: SignedOutException) {
            Notifier.signedOut(context)
        } catch (e: Exception) {
            Log.w(TAG, "sync failed: ${e.message}")
        }
    }

    /** The periodic check: unread notifications, and (for approvers) waiting approvals. */
    suspend fun checkAll(context: Context) {
        handle(context, mapOf("type" to "notification"))
        if (TwApp.from(context).settings.isAdmin) handle(context, mapOf("type" to "approval"))
    }

    private suspend fun notifications(context: Context) {
        val app = TwApp.from(context)
        val unread = app.api.notifications(unreadOnly = true)
        val shown = app.settings.shownNotifications
        val fresh = unread.filter { it.id !in shown }.sortedBy { it.created_at }
        fresh.takeLast(8).forEach { Notifier.serverNotification(context, it) }
        if (fresh.isNotEmpty()) app.settings.shownNotifications = shown + fresh.map { it.id }
        // Read elsewhere (the web app): take it off the phone too.
        val unreadIds = unread.map { it.id }.toSet()
        shown.filter { it !in unreadIds }.forEach { Notifier.cancelServer(context, it) }
    }

    private suspend fun approvals(context: Context) {
        val app = TwApp.from(context)
        val waiting = try {
            app.api.approvals()
        } catch (e: com.techwiser.twai.data.ApiException) {
            if (e.code == 403) return else throw e
        }
        val shown = app.settings.shownApprovals
        val fresh = waiting.filter { it.id !in shown }
        fresh.forEach { Notifier.approval(context, it) }
        if (fresh.isNotEmpty()) app.settings.shownApprovals = shown + fresh.map { it.id }
    }

    suspend fun chatFinished(context: Context, thread: String, ok: Boolean) {
        val app = TwApp.from(context)
        if (thread.isBlank() || app.settings.openThread == thread) return
        if (!ok) {
            Notifier.chatReply(context, thread, "That request failed on the server. Open the chat to try again.")
            return
        }
        val pending = runCatching { app.api.pending(thread) }.getOrNull()
        if (pending != null) {
            Notifier.confirmNeeded(context, thread, pending.title)
            return
        }
        val last = app.api.messages(thread).lastOrNull { it.role == "assistant" } ?: return
        Notifier.chatReply(context, thread, last.content)
    }
}

/** Periodic (every 15 minutes) and one-off checks, when push isn't available or a push ran out of time. */
class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val data = inputData.keyValueMap.mapValues { it.value.toString() }
        if (data.isEmpty()) Sync.checkAll(applicationContext) else Sync.handle(applicationContext, data)
        return Result.success()
    }

    companion object {
        private const val PERIODIC = "tw-sync"
        private val online = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
                .setConstraints(online)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES)
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, request)
        }

        fun runOnce(context: Context, data: Map<String, String>) {
            val request = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(online)
                .setInputData(workDataOf(*data.map { it.key to it.value }.toTypedArray()))
                .build()
            WorkManager.getInstance(context).enqueue(request)
        }

        fun cancel(context: Context) {
            WorkManager.getInstance(context).cancelAllWork()
        }
    }
}

/**
 * Follows a chat reply the app lost sight of (the app went to the background, or the line dropped)
 * and notifies when it is ready. Covers phones without push; with push the server's wake-up and
 * this land on the same notification.
 */
class FollowRunWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val thread = inputData.getString(K_THREAD) ?: return Result.success()
        val run = inputData.getString(K_RUN) ?: return Result.success()
        val api = TwApp.from(applicationContext).api
        val until = System.currentTimeMillis() + 9 * 60_000L
        while (System.currentTimeMillis() < until) {
            delay(5_000)
            val state = try {
                api.runStatus(run)
            } catch (e: SignedOutException) {
                return Result.success()
            } catch (e: Exception) {
                continue
            }
            when (state) {
                "running" -> continue
                "done" -> Sync.chatFinished(applicationContext, thread, true)
                "failed" -> Sync.chatFinished(applicationContext, thread, false)
            }
            return Result.success()
        }
        return Result.success()
    }

    companion object {
        private const val K_THREAD = "thread"
        private const val K_RUN = "run"

        fun start(context: Context, thread: String, runId: String) {
            val request = OneTimeWorkRequestBuilder<FollowRunWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setInputData(Data.Builder().putString(K_THREAD, thread).putString(K_RUN, runId).build())
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork("follow-$thread", ExistingWorkPolicy.REPLACE, request)
        }

        fun stop(context: Context, thread: String) {
            WorkManager.getInstance(context).cancelUniqueWork("follow-$thread")
        }
    }
}
