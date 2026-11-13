package com.techwiser.twai.ui.alerts

import android.app.DatePickerDialog
import android.app.TimePickerDialog
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Alarm
import androidx.compose.material.icons.outlined.CalendarMonth
import androidx.compose.material.icons.outlined.Campaign
import androidx.compose.material.icons.outlined.Delete
import androidx.compose.material.icons.outlined.DoneAll
import androidx.compose.material.icons.outlined.Mail
import androidx.compose.material.icons.outlined.NotificationsNone
import androidx.compose.material.icons.outlined.Share
import androidx.compose.material.icons.outlined.Warning
import androidx.compose.material.icons.outlined.WorkOutline
import androidx.compose.material.icons.outlined.Add
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuAnchorType
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SuggestionChip
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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.NotificationOut
import com.techwiser.twai.data.ReminderIn
import com.techwiser.twai.data.ReminderOut
import com.techwiser.twai.notify.Notifier
import com.techwiser.twai.ui.EmptyState
import com.techwiser.twai.ui.ErrorBanner
import com.techwiser.twai.ui.appViewModel
import com.techwiser.twai.ui.friendly
import com.techwiser.twai.ui.relative
import com.techwiser.twai.ui.toast
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.util.Calendar

data class AlertsUi(
    val loading: Boolean = true,
    val notifications: List<NotificationOut> = emptyList(),
    val reminders: List<ReminderOut> = emptyList(),
    val error: String? = null,
)

class AlertsViewModel(private val app: TwApp) : ViewModel() {
    private val _ui = MutableStateFlow(AlertsUi())
    val ui = _ui.asStateFlow()

    private fun guarded(block: suspend () -> Unit) = viewModelScope.launch {
        try {
            block()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _ui.update { it.copy(loading = false, error = friendly(e)) }
        }
    }

    fun refresh() = guarded {
        _ui.update { it.copy(loading = true, error = null) }
        val n = app.api.notifications()
        val r = app.api.reminders()
        _ui.update { AlertsUi(loading = false, notifications = n, reminders = r) }
    }

    fun read(n: NotificationOut) = guarded {
        if (!n.read) {
            app.api.markRead(n.id)
            Notifier.cancelServer(app, n.id)
            _ui.update { s -> s.copy(notifications = s.notifications.map { if (it.id == n.id) it.copy(read = true) else it }) }
        }
    }

    fun readAll() = guarded {
        app.api.markRead(null)
        _ui.value.notifications.forEach { Notifier.cancelServer(app, it.id) }
        _ui.update { s -> s.copy(notifications = s.notifications.map { it.copy(read = true) }) }
    }

    fun add(r: ReminderIn, done: (String?) -> Unit) = viewModelScope.launch {
        try {
            app.api.addReminder(r)
            done(null)
            refresh()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            done(friendly(e))
        }
    }

    fun cancel(id: String) = guarded {
        app.api.cancelReminder(id)
        _ui.update { s -> s.copy(reminders = s.reminders.filterNot { it.id == id }) }
    }

    fun digest(done: (String) -> Unit) = viewModelScope.launch {
        done(runCatching { app.api.digestNow() }.fold({ "Digest ready: it's in your notifications." }, { friendly(it) }))
        refresh()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AlertsScreen(startTab: Int = 0) {
    val context = LocalContext.current
    val vm = appViewModel { AlertsViewModel(it) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    var tab by rememberSaveable { mutableStateOf(startTab) }
    var adding by remember { mutableStateOf(false) }
    LifecycleResumeEffect(Unit) {
        vm.refresh()
        onPauseOrDispose { }
    }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { com.techwiser.twai.ui.theme.TwTitle("Personal · Alerts", "Alerts") },
                actions = {
                    if (tab == 0) {
                        TextButton(onClick = { vm.digest { toast(context, it) } }) {
                            Icon(Icons.Outlined.Mail, null); Text("  Digest")
                        }
                        IconButton(onClick = { vm.readAll() }) { Icon(Icons.Outlined.DoneAll, contentDescription = "Mark all read") }
                    }
                },
            )
        },
        floatingActionButton = {
            if (tab == 1) FloatingActionButton(onClick = { adding = true }) { Icon(Icons.Outlined.Add, contentDescription = "New reminder") }
        },
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            val unread = ui.notifications.count { !it.read }
            TabRow(selectedTabIndex = tab) {
                Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text(if (unread > 0) "Notifications ($unread)" else "Notifications") })
                Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("Reminders (${ui.reminders.size})") })
            }
            ui.error?.let { ErrorBanner(it, Modifier.padding(12.dp)) { vm.refresh() } }
            PullToRefreshBox(isRefreshing = ui.loading, onRefresh = { vm.refresh() }, modifier = Modifier.fillMaxSize()) {
                if (tab == 0) NotificationList(ui.notifications) { vm.read(it) } else ReminderList(ui.reminders) { vm.cancel(it) }
            }
        }
    }
    if (adding) {
        NewReminderDialog(onDismiss = { adding = false }, onSave = { r, done -> vm.add(r, done) })
    }
}

private fun iconFor(kind: String): ImageVector = when (kind) {
    "reminder" -> Icons.Outlined.Alarm
    "digest", "mail" -> Icons.Outlined.Mail
    "job" -> Icons.Outlined.WorkOutline
    "share" -> Icons.Outlined.Share
    "alert" -> Icons.Outlined.Warning
    else -> Icons.Outlined.Campaign
}

@Composable
private fun NotificationList(items: List<NotificationOut>, onRead: (NotificationOut) -> Unit) {
    if (items.isEmpty()) {
        EmptyState(Icons.Outlined.NotificationsNone, "All caught up", "Due reminders, mail digests, finished jobs and shares land here.")
        return
    }
    var open by remember { mutableStateOf<String?>(null) }
    LazyColumn(Modifier.fillMaxSize()) {
        items(items, key = { it.id }) { n ->
            val expanded = open == n.id
            ListItem(
                leadingContent = {
                    Box {
                        Icon(iconFor(n.kind), contentDescription = n.kind)
                        if (!n.read) Box(Modifier.size(8.dp).background(MaterialTheme.colorScheme.secondary, CircleShape)
                            .align(Alignment.TopEnd))
                    }
                },
                headlineContent = {
                    Text(n.title, fontWeight = if (n.read) FontWeight.Normal else FontWeight.SemiBold,
                        maxLines = if (expanded) 5 else 1, overflow = TextOverflow.Ellipsis)
                },
                supportingContent = {
                    Column {
                        if (n.body.isNotBlank()) {
                            Text(n.body, maxLines = if (expanded) 200 else 2, overflow = TextOverflow.Ellipsis)
                        }
                        Text(relative(n.created_at), style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                },
                modifier = Modifier.clickable {
                    open = if (expanded) null else n.id
                    onRead(n)
                },
            )
            HorizontalDivider()
        }
    }
}

@Composable
private fun ReminderList(items: List<ReminderOut>, onCancel: (String) -> Unit) {
    if (items.isEmpty()) {
        EmptyState(Icons.Outlined.Alarm, "No reminders",
            "Add one here, or just ask in a chat: \"remind me tomorrow at 9 to call the bank\".")
        return
    }
    LazyColumn(Modifier.fillMaxSize()) {
        items(items, key = { it.id }) { r ->
            ListItem(
                leadingContent = { Icon(Icons.Outlined.Alarm, null) },
                headlineContent = { Text(r.text) },
                supportingContent = {
                    Text(buildString {
                        append(r.due)
                        if (r.repeat != "none") append(" · repeats ${r.repeat}")
                        if (r.email) append(" · email too")
                    })
                },
                trailingContent = {
                    IconButton(onClick = { onCancel(r.id) }) { Icon(Icons.Outlined.Delete, contentDescription = "Cancel reminder") }
                },
            )
            HorizontalDivider()
        }
    }
}

private val REPEATS = listOf("none" to "Once", "daily" to "Every day", "weekdays" to "Weekdays", "weekly" to "Every week", "monthly" to "Every month")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun NewReminderDialog(onDismiss: () -> Unit, onSave: (ReminderIn, (String?) -> Unit) -> Unit) {
    val context = LocalContext.current
    var text by remember { mutableStateOf("") }
    var whenText by remember { mutableStateOf("") }
    var repeatMode by remember { mutableStateOf("none") }
    var email by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var saving by remember { mutableStateOf(false) }
    var repeatOpen by remember { mutableStateOf(false) }

    fun pickDateTime() {
        val now = Calendar.getInstance()
        DatePickerDialog(context, { _, y, m, d ->
            TimePickerDialog(context, { _, h, min ->
                whenText = "%04d-%02d-%02d %02d:%02d".format(y, m + 1, d, h, min)
            }, now.get(Calendar.HOUR_OF_DAY), now.get(Calendar.MINUTE), android.text.format.DateFormat.is24HourFormat(context)).show()
        }, now.get(Calendar.YEAR), now.get(Calendar.MONTH), now.get(Calendar.DAY_OF_MONTH)).show()
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("New reminder") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(value = text, onValueChange = { text = it }, label = { Text("Remind me to…") },
                    modifier = Modifier.fillMaxWidth())
                OutlinedTextField(
                    value = whenText, onValueChange = { whenText = it }, singleLine = true,
                    label = { Text("When") }, placeholder = { Text("tomorrow 9am, in 2 hours, friday 17:30") },
                    trailingIcon = { IconButton(onClick = { pickDateTime() }) { Icon(Icons.Outlined.CalendarMonth, "Pick a date") } },
                    modifier = Modifier.fillMaxWidth(),
                )
                LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    items(listOf("in 1 hour", "tonight", "tomorrow 9am", "next monday")) { s ->
                        SuggestionChip(onClick = { whenText = s }, label = { Text(s) })
                    }
                }
                ExposedDropdownMenuBox(expanded = repeatOpen, onExpandedChange = { repeatOpen = it }) {
                    OutlinedTextField(
                        value = REPEATS.first { it.first == repeatMode }.second, onValueChange = {}, readOnly = true,
                        label = { Text("Repeat") },
                        trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = repeatOpen) },
                        modifier = Modifier.menuAnchor(MenuAnchorType.PrimaryNotEditable).fillMaxWidth(),
                    )
                    ExposedDropdownMenu(expanded = repeatOpen, onDismissRequest = { repeatOpen = false }) {
                        REPEATS.forEach { (value, label) ->
                            DropdownMenuItem(text = { Text(label) }, onClick = { repeatMode = value; repeatOpen = false })
                        }
                    }
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = email, onCheckedChange = { email = it })
                    Text("Email me too")
                }
                error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
            }
        },
        confirmButton = {
            TextButton(enabled = text.isNotBlank() && whenText.isNotBlank() && !saving, onClick = {
                saving = true
                onSave(ReminderIn(whenText.trim(), text.trim(), repeatMode, email)) { e ->
                    saving = false
                    if (e == null) onDismiss() else error = e
                }
            }) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
