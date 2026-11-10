package com.techwiser.twai.ui.chat

import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.AttachmentOut
import com.techwiser.twai.data.BrowserHandover
import com.techwiser.twai.data.ChatOut
import com.techwiser.twai.data.ChatRequestOut
import com.techwiser.twai.data.ConfirmOut
import com.techwiser.twai.data.Dashboard
import com.techwiser.twai.data.MessageOut
import com.techwiser.twai.data.StreamEvent
import com.techwiser.twai.notify.FollowRunWorker
import com.techwiser.twai.notify.Notifier
import com.techwiser.twai.ui.friendly
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class UiMessage(
    val key: Long,
    val role: String,
    val text: String,
    val department: String? = null,
    val dashboards: List<Dashboard> = emptyList(),
)

data class ChatUi(
    val loading: Boolean = true,
    val messages: List<UiMessage> = emptyList(),
    val streaming: String? = null,
    val steps: List<String> = emptyList(),
    val busy: Boolean = false,
    val pending: ConfirmOut? = null,
    val browser: BrowserHandover? = null,
    val requests: List<ChatRequestOut> = emptyList(),
    val attachments: List<AttachmentOut> = emptyList(),
    val uploading: Boolean = false,
    val error: String? = null,
    val notice: String? = null,
    val mode: String = "auto",
)

private val DEPARTMENTS = mapOf(
    "account" to "Accounts", "hr" to "HR", "logistics" to "Logistics", "researcher" to "Research",
    "superuser" to "Superuser", "admin" to "Admin",
)

fun departmentLabel(d: String?): String? = d?.let { DEPARTMENTS[it] ?: it.replaceFirstChar { c -> c.uppercase() } }

class ChatViewModel(private val app: TwApp, val thread: String) : ViewModel() {
    private val api = app.api
    private val _ui = MutableStateFlow(ChatUi())
    val ui: StateFlow<ChatUi> = _ui.asStateFlow()

    private var nextKey = 0L
    private var runId: String? = null
    private var job: Job? = null
    private var requestsPoll: Job? = null
    private var lastAsked: String? = null

    init {
        load()
    }

    private fun MessageOut.toUi() = UiMessage(nextKey++, role, content, handled_by, dashboards.mapNotNull(api::dashboard))

    fun load() {
        viewModelScope.launch {
            try {
                val history = api.messages(thread)
                val pending = runCatching { api.pending(thread) }.getOrNull()
                _ui.update {
                    it.copy(loading = false, messages = history.map { m -> m.toUi() }, pending = pending, error = null)
                }
                val running = runCatching { api.running(thread) }.getOrNull()
                if (running != null && job?.isActive != true) {
                    runId = running.id
                    _ui.update { it.copy(busy = true, steps = running.steps, streaming = null) }
                    job = viewModelScope.launch { follow() }
                }
                refreshSide()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _ui.update { it.copy(loading = false, error = friendly(e)) }
            }
        }
    }

    private suspend fun refreshSide() {
        val attachments = runCatching { api.attachments(thread) }.getOrDefault(_ui.value.attachments)
        val requests = runCatching { api.requests(thread) }.getOrDefault(_ui.value.requests)
        _ui.update { it.copy(attachments = attachments, requests = requests) }
        if (requests.any { it.state in OPEN_STATES } && requestsPoll?.isActive != true) {
            requestsPoll = viewModelScope.launch {
                while (true) {
                    delay(10_000)
                    val now = runCatching { api.requests(thread) }.getOrNull() ?: continue
                    val before = _ui.value.requests
                    _ui.update { it.copy(requests = now) }
                    if (now.none { it.state in OPEN_STATES }) {
                        if (now != before) reloadMessages()
                        break
                    }
                }
            }
        }
    }

    private suspend fun reloadMessages() {
        val history = runCatching { api.messages(thread) }.getOrNull() ?: return
        val pending = runCatching { api.pending(thread) }.getOrNull()
        _ui.update { it.copy(messages = history.map { m -> m.toUi() }, pending = pending) }
    }

    fun setMode(mode: String) = _ui.update { it.copy(mode = mode) }

    fun dismissError() = _ui.update { it.copy(error = null, notice = null) }

    fun send(text: String) {
        val message = text.trim()
        if (message.isEmpty() || _ui.value.busy) return
        lastAsked = message
        _ui.update {
            it.copy(
                messages = it.messages + UiMessage(nextKey++, "user", message),
                busy = true, streaming = "", steps = emptyList(), error = null, notice = null,
                pending = null, browser = null,
            )
        }
        job = viewModelScope.launch { run(api.streamChat(thread, message, _ui.value.mode)) }
    }

    fun confirm(approve: Boolean, asWork: Boolean = false) {
        if (_ui.value.busy) return
        _ui.update { it.copy(busy = true, streaming = "", steps = emptyList(), pending = null, error = null) }
        job = viewModelScope.launch { run(api.streamConfirm(thread, approve, asWork)) }
    }

    private suspend fun run(stream: Flow<StreamEvent>) {
        runId = null
        try {
            stream.collect { event ->
                when (event) {
                    is StreamEvent.Run -> runId = event.id
                    is StreamEvent.Step -> _ui.update { it.copy(steps = it.steps + event.text) }
                    is StreamEvent.Delta -> _ui.update { it.copy(streaming = (it.streaming ?: "") + event.text) }
                    is StreamEvent.Done -> finish(event.out)
                    is StreamEvent.Failed -> _ui.update {
                        it.copy(busy = false, streaming = null, steps = emptyList(), error = event.detail)
                    }
                }
            }
            if (_ui.value.busy) follow()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // The connection dropped: the server carries on with the reply. Follow it.
            if (_ui.value.busy) follow()
        }
    }

    private fun finish(out: ChatOut) {
        val dashboards = out.dashboards.mapNotNull(api::dashboard)
        _ui.update { state ->
            val added = if (out.reply.isNotBlank() || dashboards.isNotEmpty()) {
                listOf(UiMessage(nextKey++, "assistant", out.reply, out.department.takeIf { out.route == "work" }, dashboards))
            } else emptyList()
            state.copy(
                busy = false, streaming = null, steps = emptyList(),
                messages = state.messages + added,
                pending = out.confirm,
                browser = out.browser,
                notice = when (out.outcome) {
                    "flagged_nsfw" -> "This message was blocked and reported (${out.reason.ifBlank { "explicit or illegal content" }})."
                    "denied_auth" -> out.reason.ifBlank { null }
                    else -> null
                },
            )
        }
        Notifier.cancelChat(app, thread)
        viewModelScope.launch { refreshSide() }
    }

    /** Follow a run the stream lost: poll its status, then load the saved reply. */
    private suspend fun follow() {
        val asked = lastAsked
        var id = runId
        var tries = 0
        while (id == null && tries < 5) {
            delay(2_000)
            id = runCatching { api.running(thread) }.getOrNull()?.id
            tries++
        }
        val run = id
        if (run == null) {
            // Not running any more: it finished, or never started. Show what the server has.
            reloadMessages()
            val last = _ui.value.messages.lastOrNull()
            _ui.update {
                it.copy(busy = false, streaming = null, steps = emptyList(),
                    error = if (last?.role == "user" && last.text == asked && it.pending == null)
                        "The reply was lost (the server may have restarted). Send it again." else null)
            }
            return
        }
        runId = run
        _ui.update { it.copy(streaming = null) }
        while (true) {
            delay(3_000)
            val state = try {
                api.runStatus(run)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                continue
            }
            if (state == "running") {
                runCatching { api.running(thread) }.getOrNull()?.let { live ->
                    _ui.update { it.copy(steps = live.steps) }
                }
                continue
            }
            reloadMessages()
            _ui.update {
                it.copy(
                    busy = false, steps = emptyList(), streaming = null,
                    error = when (state) {
                        "failed" -> "That request failed on the server. Try again."
                        "unknown" -> "The server restarted while answering. Send it again."
                        else -> null
                    },
                )
            }
            refreshSide()
            return
        }
    }

    /** The app went to the background mid-reply: a worker follows it and notifies. */
    fun onBackground() {
        val run = runId
        if (_ui.value.busy && run != null) FollowRunWorker.start(app, thread, run)
    }

    fun onForeground() {
        FollowRunWorker.stop(app, thread)
        Notifier.cancelChat(app, thread)
        if (!_ui.value.busy && !_ui.value.loading) viewModelScope.launch { reloadMessages(); refreshSide() }
    }

    fun upload(uri: Uri) {
        viewModelScope.launch {
            _ui.update { it.copy(uploading = true, error = null) }
            try {
                val picked = withContext(Dispatchers.IO) { api.readPicked(uri, 10L * 1024 * 1024) }
                val out = api.uploadAttachment(thread, picked)
                if (out.outcome == "flagged_nsfw") {
                    _ui.update { it.copy(notice = "That file was rejected and reported (${out.reason}).") }
                }
                _ui.update { it.copy(attachments = runCatching { api.attachments(thread) }.getOrDefault(it.attachments)) }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _ui.update { it.copy(error = friendly(e)) }
            } finally {
                _ui.update { it.copy(uploading = false) }
            }
        }
    }

    fun removeAttachment(id: String) {
        viewModelScope.launch {
            runCatching { api.deleteAttachment(id) }.onFailure { e -> _ui.update { it.copy(error = friendly(e)) } }
            _ui.update { it.copy(attachments = it.attachments.filterNot { a -> a.id == id }) }
        }
    }

    suspend fun downloadAttachment(a: AttachmentOut) = api.download("/v1/chat/attachments/${Uri.encode(a.id)}", a.name)

    fun closeBrowser() = _ui.update { it.copy(browser = null) }

    fun submitSecret(value: String, onDone: (String?) -> Unit) {
        val handover = _ui.value.browser ?: return
        val ask = handover.secret ?: return
        viewModelScope.launch {
            try {
                api.browserSecret(handover.scope, ask, value)
                _ui.update { it.copy(browser = null) }
                onDone(null)
                send("I've entered it, please carry on.")
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                onDone(friendly(e))
            }
        }
    }

    fun browserDone() {
        _ui.update { it.copy(browser = null) }
        send("Done, please carry on.")
    }

    override fun onCleared() {
        // Leaving the chat mid-reply: keep following in the background.
        onBackground()
    }

    companion object {
        val OPEN_STATES = setOf("pending", "queued", "approved", "running", "processing")
    }
}
