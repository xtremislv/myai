package com.techwiser.twai.notify

import android.content.Context
import android.util.Log
import com.google.firebase.FirebaseApp
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.techwiser.twai.BuildConfig
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.AuthState
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Firebase Cloud Messaging, when the app was built with a google-services.json. The server only
 * sends content-free wake-ups ({"type": "notification" | "chat_done" | "approval" | ...}); the app
 * then fetches what happened itself (app/push.py on the server).
 */
object Push {
    private const val TAG = "TwPush"

    fun available(context: Context): Boolean =
        BuildConfig.HAS_FIREBASE && runCatching { FirebaseApp.getApps(context).isNotEmpty() }.getOrDefault(false)

    suspend fun token(context: Context): String? {
        if (!available(context)) return null
        return withTimeoutOrNull(10_000) {
            suspendCancellableCoroutine { cont ->
                FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
                    cont.resumeWith(Result.success(if (task.isSuccessful) task.result else null))
                }
            }
        }
    }

    /** Send this install's push token to the server (after sign-in, and when Firebase renews it). */
    fun register(context: Context, known: String? = null) {
        val app = TwApp.from(context)
        app.scope.launch {
            val token = known ?: token(context) ?: return@launch
            runCatching { app.api.setPushToken(token) }
                .onFailure { Log.w(TAG, "couldn't register push token: ${it.message}") }
        }
    }
}

class PushService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        if (TwApp.from(this).api.auth.value == AuthState.SIGNED_IN) Push.register(this, token)
    }

    override fun onMessageReceived(message: RemoteMessage) {
        // Runs on a background thread with a short time budget: fetch now, fall back to a job.
        val data = message.data
        val done = runBlocking {
            withTimeoutOrNull(15_000) {
                Sync.handle(applicationContext, data)
                true
            } ?: false
        }
        if (!done) SyncWorker.runOnce(applicationContext, data)
    }
}
