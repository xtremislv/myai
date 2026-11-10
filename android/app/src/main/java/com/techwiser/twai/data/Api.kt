package com.techwiser.twai.data

import android.content.Context
import android.net.Uri
import android.os.Build
import android.provider.OpenableColumns
import kotlinx.coroutines.CancellableContinuation
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.KSerializer
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.serializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** The server said no: [code] is the HTTP status, [message] its "detail" in plain words. */
class ApiException(val code: Int, override val message: String) : Exception(message)

/** The phone's sign-in is gone (signed out here, from another device, or by the admin). */
class SignedOutException : Exception("You were signed out. Sign in again.")

enum class AuthState { SIGNED_OUT, MUST_CHANGE_PASSWORD, SIGNED_IN }

/** A file picked on the phone, read for upload. */
class PickedFile(val name: String, val mime: String, val bytes: ByteArray)

/**
 * Everything the app asks the server, with the phone's sign-in: a short-lived access token in
 * memory, renewed from the refresh token in [SecureStore] (POST /v1/auth/device/refresh).
 */
class Api(private val context: Context, private val settings: Settings, private val store: SecureStore) {

    val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        coerceInputValues = true
        encodeDefaults = true
    }

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        // A non-streamed chat reply or an approval can take minutes on the GPU.
        .readTimeout(5, TimeUnit.MINUTES)
        .writeTimeout(2, TimeUnit.MINUTES)
        .build()

    // Server-sent events: the server pings every few seconds, so a long silence means a dead line.
    private val streamHttp: OkHttpClient = http.newBuilder().readTimeout(90, TimeUnit.SECONDS).build()

    private val mutex = Mutex()

    @Volatile private var access: String? = null
    @Volatile private var accessExpiresAt = 0L

    private val _auth = MutableStateFlow(initialState())
    val auth: StateFlow<AuthState> = _auth.asStateFlow()

    val deviceId: String? get() = store.get(K_DEVICE)

    private fun initialState(): AuthState = when {
        store.get(K_REFRESH) == null -> AuthState.SIGNED_OUT
        store.get(K_MUST_CHANGE) == "1" -> AuthState.MUST_CHANGE_PASSWORD
        else -> AuthState.SIGNED_IN
    }

    private fun base(): String = settings.server

    // ---------------------------------------------------------------- sign-in

    private fun deviceName(): String =
        listOf(Build.MANUFACTURER.replaceFirstChar { it.uppercase() }, Build.MODEL).distinct().joinToString(" ").take(120)

    suspend fun login(username: String, password: String, pushToken: String?): DeviceTokenOut {
        val out = post(
            "/v1/auth/device/login",
            DeviceLoginIn(username.trim(), password, deviceName(), push_token = pushToken),
            DeviceLoginIn.serializer(), DeviceTokenOut.serializer(), auth = false,
        )
        settings.username = username.trim()
        mutex.withLock { accept(out) }
        return out
    }

    /** After the first password change: sign this phone in again so it carries the chat key. */
    suspend fun changePassword(old: String, new: String, publish: Boolean = true): TokenOut {
        val out = post("/v1/auth/change-password", ChangePasswordIn(old, new),
            ChangePasswordIn.serializer(), TokenOut.serializer())
        rebindWith(out.access_token, publish)
        return out
    }

    suspend fun recover(username: String, code: String, newPassword: String, pushToken: String?,
                        publish: Boolean = true): TokenOut {
        val out = post("/v1/auth/recover", RecoverIn(username.trim(), code.trim(), newPassword),
            RecoverIn.serializer(), TokenOut.serializer(), auth = false)
        settings.username = username.trim()
        val dev = sendRaw("POST", "/v1/devices",
            json.encodeToString(DeviceRegisterIn.serializer(), DeviceRegisterIn(deviceName(), push_token = pushToken)),
            bearer = out.access_token)
        mutex.withLock { accept(json.decodeFromString(DeviceTokenOut.serializer(), dev), publish) }
        return out
    }

    private suspend fun rebindWith(freshToken: String, publish: Boolean) {
        val body = DeviceRegisterIn(deviceName(), device_id = deviceId)
        val raw = sendRaw("POST", "/v1/devices", json.encodeToString(DeviceRegisterIn.serializer(), body), bearer = freshToken)
        mutex.withLock { accept(json.decodeFromString(DeviceTokenOut.serializer(), raw), publish) }
    }

    private fun accept(out: DeviceTokenOut, publish: Boolean = true) {
        store.put(K_DEVICE, out.device_id)
        store.put(K_REFRESH, out.refresh_token)
        store.put(K_MUST_CHANGE, if (out.must_change_password) "1" else null)
        access = out.access_token
        accessExpiresAt = System.currentTimeMillis() + out.expires_in * 1000L
        if (publish) publishAuth()
    }

    /** Let the app move on (after showing a just-created recovery code, for instance). */
    fun publishAuth() {
        _auth.value = initialState()
    }

    /** A valid access token, refreshed if it is about to expire. */
    suspend fun token(force: Boolean = false): String = mutex.withLock { tokenLocked(force) }

    private suspend fun tokenLocked(force: Boolean): String {
        val current = access
        if (!force && current != null && System.currentTimeMillis() < accessExpiresAt - 60_000) return current
        val id = store.get(K_DEVICE)
        val refresh = store.get(K_REFRESH)
        if (id == null || refresh == null) {
            forgetLocally()
            throw SignedOutException()
        }
        val raw = try {
            sendRaw("POST", "/v1/auth/device/refresh",
                json.encodeToString(RefreshIn.serializer(), RefreshIn(id, refresh)), bearer = null)
        } catch (e: ApiException) {
            if (e.code == 401 || e.code == 403) {
                forgetLocally()
                throw SignedOutException()
            }
            throw e
        }
        val out = json.decodeFromString(DeviceTokenOut.serializer(), raw)
        accept(out)
        return out.access_token
    }

    private fun forgetLocally() {
        store.put(K_DEVICE, null)
        store.put(K_REFRESH, null)
        store.put(K_MUST_CHANGE, null)
        access = null
        accessExpiresAt = 0
        settings.clearAccount()
        _auth.value = AuthState.SIGNED_OUT
    }

    /** Sign this phone out on the server (best effort) and forget it here. */
    suspend fun signOut() {
        val id = deviceId
        if (id != null) runCatching { send("DELETE", "/v1/devices/$id", null) }
        mutex.withLock { forgetLocally() }
    }

    // ---------------------------------------------------------------- plumbing

    private suspend fun Call.await(): Response = suspendCancellableCoroutine { cont: CancellableContinuation<Response> ->
        cont.invokeOnCancellation { runCatching { cancel() } }
        enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (!cont.isCancelled) cont.resumeWith(Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                cont.resumeWith(Result.success(response))
            }
        })
    }

    fun detailOf(code: Int, body: String): String {
        val detail = runCatching {
            when (val d = json.parseToJsonElement(body).jsonObject["detail"]) {
                is JsonPrimitive -> d.contentOrNull
                is JsonArray -> d.mapNotNull { (it as? JsonObject)?.get("msg")?.jsonPrimitive?.contentOrNull }
                    .joinToString("; ")
                else -> null
            }
        }.getOrNull()
        return detail?.takeIf { it.isNotBlank() } ?: when (code) {
            401 -> "Please sign in again."
            403 -> "You don't have access to that."
            404 -> "Not found."
            429 -> "The assistant is busy right now. Try again in a moment."
            in 500..599 -> "The server hit an error. Try again."
            else -> "Request failed ($code)."
        }
    }

    private suspend fun sendRaw(method: String, path: String, body: String?, bearer: String?,
                                multipart: RequestBody? = null): String {
        val requestBody = multipart ?: body?.toRequestBody(JSON_TYPE)
            ?: if (method == "POST" || method == "PUT") "".toRequestBody(JSON_TYPE) else null
        val builder = Request.Builder().url(base() + path).method(method, requestBody)
        if (bearer != null) builder.header("Authorization", "Bearer $bearer")
        http.newCall(builder.build()).await().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw ApiException(response.code, detailOf(response.code, text))
            return text
        }
    }

    /** One authenticated request; a 401 renews the token once and tries again. */
    suspend fun send(method: String, path: String, body: String?, multipart: RequestBody? = null): String {
        return try {
            sendRaw(method, path, body, token(), multipart)
        } catch (e: ApiException) {
            if (e.code != 401) throw e
            sendRaw(method, path, body, token(force = true), multipart)
        }
    }

    suspend fun <T> get(path: String, out: KSerializer<T>): T = json.decodeFromString(out, send("GET", path, null))

    suspend fun <B, T> post(path: String, body: B, inSer: KSerializer<B>, out: KSerializer<T>, auth: Boolean = true): T {
        val text = json.encodeToString(inSer, body)
        val raw = if (auth) send("POST", path, text) else sendRaw("POST", path, text, bearer = null)
        return json.decodeFromString(out, raw)
    }

    private fun q(value: String): String = java.net.URLEncoder.encode(value, "UTF-8").replace("+", "%20")

    // ---------------------------------------------------------------- app & account

    suspend fun appConfig(): AppConfig =
        json.decodeFromString(AppConfig.serializer(), sendRaw("GET", "/v1/app/config", null, bearer = null))

    suspend fun me(): Profile = get("/v1/auth/me", Profile.serializer())

    suspend fun newRecoveryCode(password: String): RecoveryCodeOut =
        post("/v1/auth/recovery-code", PasswordIn(password), PasswordIn.serializer(), RecoveryCodeOut.serializer())

    suspend fun devices(): List<DeviceOut> = get("/v1/devices", ListSerializer(DeviceOut.serializer()))

    suspend fun signOutDevice(id: String) {
        send("DELETE", "/v1/devices/$id", null)
    }

    suspend fun setPushToken(token: String?) {
        val id = deviceId ?: return
        send("PUT", "/v1/devices/$id/push", json.encodeToString(PushIn.serializer(), PushIn(token)))
    }

    suspend fun prefs(): Prefs = get("/v1/me/prefs", Prefs.serializer())

    suspend fun savePrefs(prefs: Prefs): Prefs =
        json.decodeFromString(Prefs.serializer(), send("PUT", "/v1/me/prefs", json.encodeToString(Prefs.serializer(), prefs)))

    // ---------------------------------------------------------------- chat

    suspend fun threads(): List<ThreadOut> = get("/v1/chat/threads", ListSerializer(ThreadOut.serializer()))

    suspend fun messages(thread: String): List<MessageOut> =
        get("/v1/chat/threads/${q(thread)}/messages", ListSerializer(MessageOut.serializer()))

    suspend fun pending(thread: String): ConfirmOut? =
        send("GET", "/v1/chat/threads/${q(thread)}/pending", null).let {
            if (it.isBlank() || it == "null") null else json.decodeFromString(ConfirmOut.serializer(), it)
        }

    suspend fun running(thread: String): RunningOut? =
        send("GET", "/v1/chat/threads/${q(thread)}/running", null).let {
            if (it.isBlank() || it == "null") null else json.decodeFromString(RunningOut.serializer(), it)
        }

    suspend fun runningThreads(): List<String> =
        json.decodeFromString(ListSerializer(String.serializer()), send("GET", "/v1/chat/running", null))

    suspend fun runStatus(runId: String): String = get("/v1/chat/runs/${q(runId)}", RunStatusOut.serializer()).state

    suspend fun requests(thread: String): List<ChatRequestOut> =
        get("/v1/chat/threads/${q(thread)}/requests", ListSerializer(ChatRequestOut.serializer()))

    /** Non-streamed message (used by the notification "Reply" action). */
    suspend fun chat(thread: String, message: String): ChatOut =
        post("/v1/chat", ChatIn(message, thread), ChatIn.serializer(), ChatOut.serializer())

    fun streamChat(thread: String, message: String, mode: String = "auto"): Flow<StreamEvent> =
        stream("/v1/chat/stream", json.encodeToString(ChatIn.serializer(), ChatIn(message, thread, mode)))

    fun streamConfirm(thread: String, approve: Boolean, asWork: Boolean): Flow<StreamEvent> =
        stream("/v1/chat/threads/${q(thread)}/confirm/stream",
            json.encodeToString(ConfirmIn.serializer(), ConfirmIn(approve, asWork)))

    private fun stream(path: String, body: String): Flow<StreamEvent> = callbackFlow {
        val bearer = try {
            token()
        } catch (e: Exception) {
            trySend(StreamEvent.Failed(e.message ?: "Not signed in"))
            close()
            awaitClose { }
            return@callbackFlow
        }
        val request = Request.Builder().url(base() + path)
            .header("Authorization", "Bearer $bearer")
            .header("Accept", "text/event-stream")
            .post(body.toRequestBody(JSON_TYPE))
            .build()
        val listener = object : EventSourceListener() {
            override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                val obj = runCatching { json.parseToJsonElement(data).jsonObject }.getOrNull() ?: return
                val event = when (obj["type"]?.jsonPrimitive?.contentOrNull) {
                    "run" -> StreamEvent.Run(obj["id"]?.jsonPrimitive?.contentOrNull.orEmpty())
                    "step" -> StreamEvent.Step(obj["text"]?.jsonPrimitive?.contentOrNull.orEmpty())
                    "delta" -> StreamEvent.Delta(obj["text"]?.jsonPrimitive?.contentOrNull.orEmpty())
                    "done" -> StreamEvent.Done(json.decodeFromJsonElement(ChatOut.serializer(), obj))
                    "error" -> StreamEvent.Failed(obj["detail"]?.jsonPrimitive?.contentOrNull ?: "That request failed.")
                    else -> null
                }
                if (event != null) trySend(event)
                if (event is StreamEvent.Done || event is StreamEvent.Failed) {
                    eventSource.cancel()
                    close()
                }
            }

            override fun onClosed(eventSource: EventSource) {
                close()
            }

            override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) {
                if (response != null && !response.isSuccessful) {
                    val text = runCatching { response.body?.string().orEmpty() }.getOrDefault("")
                    trySend(StreamEvent.Failed(detailOf(response.code, text)))
                    close()
                } else {
                    // The line dropped: the server carries on; the caller follows the run.
                    close(t ?: IOException("connection lost"))
                }
            }
        }
        val source = EventSources.createFactory(streamHttp).newEventSource(request, listener)
        awaitClose { source.cancel() }
    }

    suspend fun confirm(thread: String, approve: Boolean, asWork: Boolean = false): ChatOut =
        post("/v1/chat/threads/${q(thread)}/confirm", ConfirmIn(approve, asWork), ConfirmIn.serializer(), ChatOut.serializer())

    suspend fun attachments(thread: String): List<AttachmentOut> =
        get("/v1/chat/attachments?thread_id=${q(thread)}", ListSerializer(AttachmentOut.serializer()))

    suspend fun uploadAttachment(thread: String, file: PickedFile): UploadOut {
        val form = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("thread_id", thread)
            .addFormDataPart("file", file.name, file.bytes.toRequestBody(file.mime.toMediaTypeOrNull()))
            .build()
        return json.decodeFromString(UploadOut.serializer(), send("POST", "/v1/chat/attachments", null, form))
    }

    suspend fun deleteAttachment(id: String) {
        send("DELETE", "/v1/chat/attachments/${q(id)}", null)
    }

    // ---------------------------------------------------------------- live browser

    suspend fun browserScreen(scope: String): ByteArray {
        val request = Request.Builder().url(base() + "/v1/browser/${q(scope)}/screen")
            .header("Authorization", "Bearer ${token()}").build()
        http.newCall(request).await().use { response ->
            if (!response.isSuccessful) {
                throw ApiException(response.code, detailOf(response.code, response.body?.string().orEmpty()))
            }
            return response.body?.bytes() ?: ByteArray(0)
        }
    }

    suspend fun browserAct(scope: String, action: String, x: Int = 0, y: Int = 0, text: String = "", url: String = "") {
        val body = buildString {
            append("{\"action\":").append(JsonPrimitive(action)).append(",\"x\":").append(x).append(",\"y\":").append(y)
            append(",\"text\":").append(JsonPrimitive(text)).append(",\"url\":").append(JsonPrimitive(url)).append('}')
        }
        send("POST", "/v1/browser/${q(scope)}/act", body)
    }

    suspend fun browserClose(scope: String, forget: Boolean = false) {
        send("POST", "/v1/browser/${q(scope)}/close", "{\"forget\":$forget}")
    }

    suspend fun browserSecret(scope: String, ask: SecretAsk, value: String) {
        val body = "{\"ref\":${ask.ref},\"value\":${JsonPrimitive(value)},\"site\":${JsonPrimitive(ask.site)}}"
        send("POST", "/v1/browser/${q(scope)}/secret", body)
    }

    // ---------------------------------------------------------------- notifications & reminders

    suspend fun notifications(unreadOnly: Boolean = false): List<NotificationOut> =
        get("/v1/notifications?unread_only=$unreadOnly", ListSerializer(NotificationOut.serializer()))

    suspend fun unreadCount(): Int = get("/v1/notifications/count", CountOut.serializer()).unread

    suspend fun markRead(id: String? = null): Int =
        post("/v1/notifications/read", ReadIn(id), ReadIn.serializer(), CountOut.serializer()).unread

    suspend fun reminders(): List<ReminderOut> = get("/v1/reminders", ListSerializer(ReminderOut.serializer()))

    suspend fun addReminder(r: ReminderIn): ReminderOut =
        post("/v1/reminders", r, ReminderIn.serializer(), ReminderOut.serializer())

    suspend fun cancelReminder(id: String) {
        send("DELETE", "/v1/reminders/${q(id)}", null)
    }

    suspend fun digestNow(): String =
        json.parseToJsonElement(send("POST", "/v1/me/digest", null)).jsonObject["text"]?.jsonPrimitive?.contentOrNull.orEmpty()

    // ---------------------------------------------------------------- files

    suspend fun files(trash: Boolean = false): FilesOut = get("/v1/files?trash=$trash", FilesOut.serializer())

    suspend fun uploadFile(folder: String, file: PickedFile): FileOut {
        val form = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("folder", folder)
            .addFormDataPart("file", file.name, file.bytes.toRequestBody(file.mime.toMediaTypeOrNull()))
            .build()
        return json.decodeFromString(FileOut.serializer(), send("POST", "/v1/files", null, form))
    }

    suspend fun deleteFile(id: String) {
        send("DELETE", "/v1/files/${q(id)}", null)
    }

    suspend fun restoreFile(id: String): FileOut =
        json.decodeFromString(FileOut.serializer(), send("POST", "/v1/files/${q(id)}/restore", null))

    /** Downloads into the app's cache (shared with other apps only through the FileProvider). */
    suspend fun download(path: String, name: String): File {
        val request = Request.Builder().url(base() + path).header("Authorization", "Bearer ${token()}").build()
        http.newCall(request).await().use { response ->
            if (!response.isSuccessful) {
                throw ApiException(response.code, detailOf(response.code, response.body?.string().orEmpty()))
            }
            val dir = File(context.cacheDir, "downloads").apply { mkdirs() }
            val safe = name.substringAfterLast('/').replace(Regex("[^A-Za-z0-9._ -]"), "_").ifBlank { "file" }
            val out = File(dir, safe)
            response.body?.byteStream()?.use { input -> out.outputStream().use { input.copyTo(it) } }
            return out
        }
    }

    fun readPicked(uri: Uri, maxBytes: Long): PickedFile {
        val resolver = context.contentResolver
        var name = "file"
        resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) c.getString(0)?.let { name = it }
        }
        val mime = resolver.getType(uri) ?: "application/octet-stream"
        val bytes = resolver.openInputStream(uri)?.use { input ->
            val buffer = java.io.ByteArrayOutputStream()
            val chunk = ByteArray(64 * 1024)
            var total = 0L
            while (true) {
                val n = input.read(chunk)
                if (n < 0) break
                total += n
                if (total > maxBytes) throw ApiException(413, "Files are limited to ${maxBytes / (1024 * 1024)} MB.")
                buffer.write(chunk, 0, n)
            }
            buffer.toByteArray()
        } ?: throw ApiException(400, "Couldn't read that file.")
        if (bytes.isEmpty()) throw ApiException(400, "The file is empty.")
        return PickedFile(name, mime, bytes)
    }

    // ---------------------------------------------------------------- work

    suspend fun approvals(): List<ApprovalOut> = get("/v1/approvals", ListSerializer(ApprovalOut.serializer()))

    suspend fun decide(id: String, approve: Boolean, reason: String = ""): RequestOut =
        post("/v1/approvals/${q(id)}/${if (approve) "approve" else "deny"}", DecisionIn(reason),
            DecisionIn.serializer(), RequestOut.serializer())

    suspend fun inbox(department: String): List<TaskOut> =
        get("/v1/inbox?department=${q(department)}", ListSerializer(TaskOut.serializer()))

    suspend fun processTask(id: String): RequestOut =
        json.decodeFromString(RequestOut.serializer(), send("POST", "/v1/inbox/${q(id)}/process", null))

    suspend fun reports(): List<ReportOut> = get("/v1/reports", ListSerializer(ReportOut.serializer()))

    fun dashboard(obj: JsonObject): Dashboard? =
        runCatching { json.decodeFromJsonElement(Dashboard.serializer(), obj) }.getOrNull()

    fun webUrl(): String = base().toHttpUrl().newBuilder().addPathSegment("ui").addPathSegment("").build().toString()

    companion object {
        private val JSON_TYPE = "application/json".toMediaType()
        private const val K_DEVICE = "device_id"
        private const val K_REFRESH = "refresh_token"
        private const val K_MUST_CHANGE = "must_change"
    }
}
