package com.techwiser.twai.ui.chat

import com.techwiser.twai.ui.theme.TwTitle
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.Chat
import androidx.compose.material.icons.outlined.Add
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.ThreadOut
import com.techwiser.twai.ui.EmptyState
import com.techwiser.twai.ui.ErrorBanner
import com.techwiser.twai.ui.appViewModel
import com.techwiser.twai.ui.friendly
import com.techwiser.twai.ui.relative
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class ChatListUi(
    val loading: Boolean = true,
    val threads: List<ThreadOut> = emptyList(),
    val running: Set<String> = emptySet(),
    val error: String? = null,
)

class ChatListViewModel(private val app: TwApp) : ViewModel() {
    private val _ui = MutableStateFlow(ChatListUi())
    val ui = _ui.asStateFlow()

    fun refresh() {
        viewModelScope.launch {
            _ui.update { it.copy(loading = true) }
            try {
                val threads = app.api.threads()
                val running = runCatching { app.api.runningThreads().toSet() }.getOrDefault(emptySet())
                _ui.update { ChatListUi(loading = false, threads = threads, running = running) }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _ui.update { it.copy(loading = false, error = friendly(e)) }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatListScreen(onOpen: (String) -> Unit, onNew: () -> Unit) {
    val vm = appViewModel { ChatListViewModel(it) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    var query by remember { mutableStateOf("") }
    LifecycleResumeEffect(Unit) {
        vm.refresh()
        onPauseOrDispose { }
    }
    Scaffold(
        topBar = { TopAppBar(title = { TwTitle("Personal · Chats", "Chats") }) },
        floatingActionButton = {
            ExtendedFloatingActionButton(onClick = onNew, icon = { Icon(Icons.Outlined.Add, null) }, text = { Text("New chat") })
        },
    ) { padding ->
        PullToRefreshBox(isRefreshing = ui.loading && ui.threads.isNotEmpty(), onRefresh = { vm.refresh() },
            modifier = Modifier.padding(padding).fillMaxSize()) {
            Column(Modifier.fillMaxSize()) {
                ui.error?.let { ErrorBanner(it, Modifier.padding(12.dp)) { vm.refresh() } }
                if (ui.threads.size > 6) {
                    OutlinedTextField(
                        value = query, onValueChange = { query = it }, singleLine = true,
                        leadingIcon = { Icon(Icons.Outlined.Search, null) },
                        placeholder = { Text("Search chats") },
                        modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                    )
                }
                val shown = ui.threads.filter { query.isBlank() || it.title.contains(query.trim(), ignoreCase = true) }
                if (!ui.loading && ui.threads.isEmpty() && ui.error == null) {
                    EmptyState(Icons.AutoMirrored.Outlined.Chat, "No chats yet",
                        "Ask anything: private questions stay private, work goes to your department.")
                } else {
                    LazyColumn(Modifier.fillMaxSize()) {
                        items(shown, key = { it.thread_id }) { t ->
                            ListItem(
                                headlineContent = { Text(t.title.ifBlank { "Untitled" }, maxLines = 2, overflow = TextOverflow.Ellipsis) },
                                supportingContent = { Text(relative(t.updated_at), style = MaterialTheme.typography.labelSmall) },
                                trailingContent = {
                                    if (t.thread_id in ui.running) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                                },
                                modifier = Modifier.clickable { onOpen(t.thread_id) },
                            )
                            HorizontalDivider()
                        }
                    }
                }
            }
        }
    }
}
