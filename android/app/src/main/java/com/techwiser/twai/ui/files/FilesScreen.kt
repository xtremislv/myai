package com.techwiser.twai.ui.files

import com.techwiser.twai.ui.theme.TwTitle
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Delete
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material.icons.outlined.FolderOpen
import androidx.compose.material.icons.outlined.MoreVert
import androidx.compose.material.icons.outlined.RestoreFromTrash
import androidx.compose.material.icons.outlined.Share
import androidx.compose.material.icons.outlined.TableChart
import androidx.compose.material.icons.outlined.PictureAsPdf
import androidx.compose.material.icons.outlined.Upload
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.FileOut
import com.techwiser.twai.ui.EmptyState
import com.techwiser.twai.ui.ErrorBanner
import com.techwiser.twai.ui.appViewModel
import com.techwiser.twai.ui.bytes
import com.techwiser.twai.ui.friendly
import com.techwiser.twai.ui.openFile
import com.techwiser.twai.ui.relative
import com.techwiser.twai.ui.shareFile
import com.techwiser.twai.ui.toast
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

data class FilesUi(
    val loading: Boolean = true,
    val trash: Boolean = false,
    val files: List<FileOut> = emptyList(),
    val used: Long = 0,
    val quota: Long = 0,
    val busy: Boolean = false,
    val error: String? = null,
)

class FilesViewModel(private val app: TwApp) : ViewModel() {
    private val _ui = MutableStateFlow(FilesUi())
    val ui = _ui.asStateFlow()

    private fun guarded(block: suspend () -> Unit) = viewModelScope.launch {
        try {
            block()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _ui.update { it.copy(loading = false, busy = false, error = friendly(e)) }
        }
    }

    fun refresh(trash: Boolean = _ui.value.trash) = guarded {
        _ui.update { it.copy(loading = true, trash = trash, error = null) }
        val out = app.api.files(trash)
        _ui.update { it.copy(loading = false, files = out.files.sortedBy { f -> f.path.lowercase() }, used = out.used, quota = out.quota) }
    }

    fun upload(uri: Uri, folder: String) = guarded {
        _ui.update { it.copy(busy = true, error = null) }
        val picked = withContext(Dispatchers.IO) { app.api.readPicked(uri, 10L * 1024 * 1024) }
        app.api.uploadFile(folder.trim().trim('/'), picked)
        _ui.update { it.copy(busy = false) }
        refresh(false)
    }

    fun delete(f: FileOut) = guarded {
        app.api.deleteFile(f.id)
        refresh()
    }

    fun restore(f: FileOut) = guarded {
        app.api.restoreFile(f.id)
        refresh()
    }

    suspend fun download(f: FileOut): File = app.api.download("/v1/files/${Uri.encode(f.id)}", f.path)
}

private fun iconFor(f: FileOut): ImageVector = when {
    f.path.endsWith(".pdf", true) -> Icons.Outlined.PictureAsPdf
    f.path.endsWith(".csv", true) || f.path.endsWith(".xlsx", true) -> Icons.Outlined.TableChart
    else -> Icons.Outlined.Description
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FilesScreen() {
    val context = LocalContext.current
    val vm = appViewModel { FilesViewModel(it) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    var pendingUpload by remember { mutableStateOf<Uri?>(null) }
    var folder by rememberSaveable { mutableStateOf("") }
    val pick = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri -> pendingUpload = uri }
    LifecycleResumeEffect(Unit) {
        vm.refresh()
        onPauseOrDispose { }
    }

    fun withFile(f: FileOut, then: (File) -> Unit) {
        scope.launch {
            runCatching { vm.download(f) }.onSuccess(then).onFailure { toast(context, friendly(it)) }
        }
    }

    Scaffold(
        topBar = { TopAppBar(title = { TwTitle("Personal · Files", "My files") }) },
        floatingActionButton = {
            if (!ui.trash) ExtendedFloatingActionButton(
                onClick = { pick.launch(arrayOf("*/*")) },
                icon = { Icon(Icons.Outlined.Upload, null) }, text = { Text("Upload") },
            )
        },
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            TabRow(selectedTabIndex = if (ui.trash) 1 else 0) {
                Tab(selected = !ui.trash, onClick = { vm.refresh(false) }, text = { Text("Files") })
                Tab(selected = ui.trash, onClick = { vm.refresh(true) }, text = { Text("Trash") })
            }
            if (ui.quota > 0) {
                Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
                    LinearProgressIndicator(progress = { (ui.used.toFloat() / ui.quota).coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth())
                    Text("${bytes(ui.used)} of ${bytes(ui.quota)} · encrypted with your key",
                        style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 4.dp))
                }
            }
            if (ui.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            ui.error?.let { ErrorBanner(it, Modifier.padding(12.dp)) { vm.refresh() } }
            PullToRefreshBox(isRefreshing = ui.loading, onRefresh = { vm.refresh() }, modifier = Modifier.fillMaxSize()) {
                if (!ui.loading && ui.files.isEmpty()) {
                    EmptyState(Icons.Outlined.FolderOpen, if (ui.trash) "Trash is empty" else "No files yet",
                        if (ui.trash) "Deleted files stay here for 7 days." else
                            "Upload files, or ask the assistant to write one: \"save a packing list to notes/trip.md\".")
                } else {
                    LazyColumn(Modifier.fillMaxSize()) {
                        items(ui.files, key = { it.id }) { f ->
                            var menu by remember { mutableStateOf(false) }
                            ListItem(
                                leadingContent = { Icon(iconFor(f), null) },
                                headlineContent = { Text(f.path.substringAfterLast('/'), maxLines = 1, overflow = TextOverflow.Ellipsis) },
                                supportingContent = {
                                    val dir = f.path.substringBeforeLast('/', "")
                                    Text(listOfNotNull(dir.ifBlank { null }, bytes(f.size),
                                        if (f.versions > 1) "${f.versions} versions" else null, relative(f.updated_at))
                                        .joinToString(" · "))
                                },
                                trailingContent = {
                                    if (ui.trash) {
                                        IconButton(onClick = { vm.restore(f) }) { Icon(Icons.Outlined.RestoreFromTrash, "Restore") }
                                    } else Box {
                                        IconButton(onClick = { menu = true }) { Icon(Icons.Outlined.MoreVert, "More") }
                                        DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                                            DropdownMenuItem(text = { Text("Share") }, leadingIcon = { Icon(Icons.Outlined.Share, null) },
                                                onClick = { menu = false; withFile(f) { shareFile(context, it) } })
                                            DropdownMenuItem(text = { Text("Delete") }, leadingIcon = { Icon(Icons.Outlined.Delete, null) },
                                                onClick = { menu = false; vm.delete(f) })
                                        }
                                    }
                                },
                                modifier = Modifier.clickable(enabled = !ui.trash) { withFile(f) { openFile(context, it) } },
                            )
                            HorizontalDivider()
                        }
                    }
                }
            }
        }
    }

    pendingUpload?.let { uri ->
        AlertDialog(
            onDismissRequest = { pendingUpload = null },
            title = { Text("Upload to…") },
            text = {
                OutlinedTextField(value = folder, onValueChange = { folder = it }, singleLine = true,
                    label = { Text("Folder (optional)") }, placeholder = { Text("notes") })
            },
            confirmButton = { TextButton(onClick = { vm.upload(uri, folder); pendingUpload = null }) { Text("Upload") } },
            dismissButton = { TextButton(onClick = { pendingUpload = null }) { Text("Cancel") } },
        )
    }
}
