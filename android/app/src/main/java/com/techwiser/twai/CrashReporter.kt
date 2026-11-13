package com.techwiser.twai

import android.content.Context
import android.os.Build
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Saves a crash's stack trace and sends it to the server (POST /v1/app/crash) on the next start,
 * where it lands in the gateway log for the developer. Only the exception and its stack frames
 * are kept: messages are cut short, and nothing from chats, mail or files is added.
 */
object CrashReporter {
    private const val FILE = "last_crash.txt"

    @Serializable
    private data class CrashIn(val version_code: Int, val version_name: String, val android: Int, val device: String, val trace: String)

    fun install(context: Context) {
        val app = context.applicationContext
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            runCatching { File(app.filesDir, FILE).writeText(describe(thread, error)) }
            previous?.uncaughtException(thread, error)
        }
    }

    private fun describe(thread: Thread, error: Throwable): String = buildString {
        append("thread ").append(thread.name).append('\n')
        var e: Throwable? = error
        var depth = 0
        while (e != null && depth < 6) {
            if (depth > 0) append("Caused by: ")
            append(e.javaClass.name)
            e.message?.let { append(": ").append(it.take(300)) }
            append('\n')
            e.stackTrace.take(40).forEach { append("\tat ").append(it).append('\n') }
            e = e.cause.takeIf { it !== e }
            depth++
        }
    }.take(15000)

    /** Sends a saved crash, if any, in the background. */
    fun sendPending(context: Context, server: String) {
        val file = File(context.filesDir, FILE)
        if (!file.exists()) return
        Thread {
            runCatching {
                val trace = file.readText().ifBlank { return@runCatching }
                val body = Json.encodeToString(CrashIn.serializer(), CrashIn(
                    BuildConfig.VERSION_CODE, BuildConfig.VERSION_NAME, Build.VERSION.SDK_INT,
                    "${Build.MANUFACTURER} ${Build.MODEL}".take(120), trace,
                ))
                val client = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS).build()
                val request = Request.Builder().url(server.trimEnd('/') + "/v1/app/crash")
                    .post(body.toRequestBody("application/json".toMediaType())).build()
                client.newCall(request).execute().use { if (it.isSuccessful || it.code == 422) file.delete() }
            }
        }.apply { isDaemon = true; name = "crash-report" }.start()
    }
}
