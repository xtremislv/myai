package com.techwiser.twai.ui.work

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.VerifiedUser
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.ScrollableTabRow
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.ApprovalOut
import com.techwiser.twai.data.ReportOut
import com.techwiser.twai.data.RequestOut
import com.techwiser.twai.data.TaskOut
import com.techwiser.twai.ui.EmptyState
import com.techwiser.twai.ui.ErrorBanner
import com.techwiser.twai.ui.Security
import com.techwiser.twai.ui.appViewModel
import com.techwiser.twai.ui.chat.Markdown
import com.techwiser.twai.ui.chat.departmentLabel
import com.techwiser.twai.ui.friendly
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonPrimitive

enum class WorkTab(val label: String) { APPROVALS("Approvals"), INBOX("Accounts inbox"), REPORTS("Reports") }

data class WorkUi(
    val tabs: List<WorkTab> = emptyList(),
    val loading: Boolean = true,
    val approvals: List<ApprovalOut> = emptyList(),
    val inbox: List<TaskOut> = emptyList(),
    val reports: List<ReportOut> = emptyList(),
    val working: String? = null,
    val result: RequestOut? = null,
    val error: String? = null,
)

/** Which work pages this person has: the server checks every call again. */
fun workTabs(isAdmin: Boolean, departments: Set<String>): List<WorkTab> = buildList {
    if (isAdmin) add(WorkTab.APPROVALS)
    if ("account" in departments) add(WorkTab.INBOX)
    if (isAdmin || "hr" in departments || "account" in departments) add(WorkTab.REPORTS)
}

class WorkViewModel(private val app: TwApp) : ViewModel() {
    private val _ui = MutableStateFlow(WorkUi(tabs = workTabs(app.settings.isAdmin, app.settings.departments)))
    val ui = _ui.asStateFlow()

    fun refresh() = viewModelScope.launch {
        _ui.update { it.copy(loading = true, error = null) }
        try {
            val tabs = _ui.value.tabs
            val approvals = if (WorkTab.APPROVALS in tabs) app.api.approvals() else emptyList()
            val inbox = if (WorkTab.INBOX in tabs) app.api.inbox("account") else emptyList()
            val reports = if (WorkTab.REPORTS in tabs) app.api.reports() else emptyList()
            app.settings.shownApprovals = app.settings.shownApprovals + approvals.map { it.id }
            _ui.update { it.copy(loading = false, approvals = approvals, inbox = inbox, reports = reports) }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _ui.update { it.copy(loading = false, error = friendly(e)) }
        }
    }

    private fun act(id: String, block: suspend () -> RequestOut) = viewModelScope.launch {
        _ui.update { it.copy(working = id, error = null) }
        try {
            val out = block()
            _ui.update { it.copy(working = null, result = out) }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _ui.update { it.copy(working = null, error = friendly(e)) }
        }
        refresh()
    }

    fun decide(a: ApprovalOut, approve: Boolean, reason: String) = act(a.id) { app.api.decide(a.id, approve, reason) }

    fun process(t: TaskOut) = act(t.id) { app.api.processTask(t.id) }

    fun clearResult() = _ui.update { it.copy(result = null) }
}

private fun argText(v: kotlinx.serialization.json.JsonElement): String = (v as? JsonPrimitive)?.content ?: v.toString()

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WorkScreen() {
    val context = LocalContext.current
    val vm = appViewModel { WorkViewModel(it) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    var tab by rememberSaveable { mutableStateOf(0) }
    var denying by remember { mutableStateOf<ApprovalOut?>(null) }
    LifecycleResumeEffect(Unit) {
        vm.refresh()
        onPauseOrDispose { }
    }
    Scaffold(topBar = { TopAppBar(title = { Text("Work") }) }) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            if (ui.tabs.isEmpty()) {
                EmptyState(Icons.Outlined.VerifiedUser, "Nothing to review",
                    "Approvals, the Accounts inbox and reports appear here for the people who handle them.")
                return@Column
            }
            val current = ui.tabs.getOrElse(tab) { ui.tabs.first() }
            ScrollableTabRow(selectedTabIndex = ui.tabs.indexOf(current), edgePadding = 12.dp) {
                ui.tabs.forEachIndexed { i, t ->
                    val count = when (t) {
                        WorkTab.APPROVALS -> ui.approvals.size
                        WorkTab.INBOX -> ui.inbox.size
                        WorkTab.REPORTS -> ui.reports.size
                    }
                    Tab(selected = t == current, onClick = { tab = i }, text = { Text(if (count > 0) "${t.label} ($count)" else t.label) })
                }
            }
            ui.error?.let { ErrorBanner(it, Modifier.padding(12.dp)) { vm.refresh() } }
            PullToRefreshBox(isRefreshing = ui.loading, onRefresh = { vm.refresh() }, modifier = Modifier.fillMaxSize()) {
                LazyColumn(
                    Modifier.fillMaxSize(),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    when (current) {
                        WorkTab.APPROVALS -> {
                            if (ui.approvals.isEmpty() && !ui.loading) item { Empty("No approvals waiting") }
                            items(ui.approvals, key = { it.id }) { a ->
                                Card {
                                    Text("${a.tool.replace('_', ' ')} · ${departmentLabel(a.department)}",
                                        style = MaterialTheme.typography.titleMedium)
                                    Text("Asked by ${a.subject_name.ifBlank { a.subject_id }}", style = MaterialTheme.typography.labelMedium,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                                    if (a.message.isNotBlank()) Text(a.message, style = MaterialTheme.typography.bodyMedium)
                                    a.tool_args.forEach { (k, v) ->
                                        Row {
                                            Text("$k: ", style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.SemiBold)
                                            Text(argText(v), style = MaterialTheme.typography.bodySmall)
                                        }
                                    }
                                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        Button(enabled = ui.working == null, onClick = {
                                            Security.confirm(context, "Approve ${a.tool.replace('_', ' ')}?",
                                                "This runs the action for ${a.subject_name}.") { ok -> if (ok) vm.decide(a, true, "") }
                                        }) { Text(if (ui.working == a.id) "Working…" else "Approve") }
                                        OutlinedButton(enabled = ui.working == null, onClick = { denying = a }) { Text("Deny") }
                                    }
                                }
                            }
                        }
                        WorkTab.INBOX -> {
                            if (ui.inbox.isEmpty() && !ui.loading) item { Empty("The Accounts inbox is empty") }
                            items(ui.inbox, key = { it.id }) { t ->
                                Card {
                                    Text("${t.kind.replaceFirstChar { it.uppercase() }} from ${t.requester_name}",
                                        style = MaterialTheme.typography.titleMedium)
                                    Text(t.message, style = MaterialTheme.typography.bodyMedium)
                                    Button(enabled = ui.working == null, onClick = { vm.process(t) }) {
                                        Text(if (ui.working == t.id) "Working…" else "Process")
                                    }
                                }
                            }
                        }
                        WorkTab.REPORTS -> {
                            if (ui.reports.isEmpty() && !ui.loading) item { Empty("No open reports") }
                            items(ui.reports, key = { it.id }) { r ->
                                Card {
                                    Text(if (r.kind == "nsfw") "Flagged message" else r.kind.replace('_', ' ')
                                        .replaceFirstChar { it.uppercase() }, style = MaterialTheme.typography.titleMedium)
                                    Text("From ${r.subject_name}", style = MaterialTheme.typography.labelMedium,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                                    Text(r.message, style = MaterialTheme.typography.bodyMedium)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    denying?.let { a ->
        var reason by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { denying = null },
            title = { Text("Deny ${a.tool.replace('_', ' ')}?") },
            text = {
                OutlinedTextField(value = reason, onValueChange = { reason = it }, label = { Text("Reason (optional)") })
            },
            confirmButton = { TextButton(onClick = { vm.decide(a, false, reason); denying = null }) { Text("Deny") } },
            dismissButton = { TextButton(onClick = { denying = null }) { Text("Cancel") } },
        )
    }

    ui.result?.let { r ->
        AlertDialog(
            onDismissRequest = { vm.clearResult() },
            title = { Text(resultTitle(r.outcome)) },
            text = { Markdown(r.reply ?: r.reason.ifBlank { "Done." }) },
            confirmButton = { TextButton(onClick = { vm.clearResult() }) { Text("OK") } },
        )
    }
}

private fun resultTitle(outcome: String) = when (outcome) {
    "ok" -> "Done"
    "pending_approval" -> "Needs another approval"
    "running" -> "Still running"
    "error" -> "It didn't work"
    else -> outcome.replace('_', ' ').replaceFirstChar { it.uppercase() }
}

@Composable
private fun Card(content: @Composable androidx.compose.foundation.layout.ColumnScope.() -> Unit) {
    OutlinedCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp), content = content)
    }
}

@Composable
private fun Empty(text: String) {
    Column(Modifier.fillMaxWidth().padding(32.dp)) {
        Text(text, style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
