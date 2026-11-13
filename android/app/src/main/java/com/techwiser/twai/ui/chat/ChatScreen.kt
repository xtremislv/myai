package com.techwiser.twai.ui.chat

import com.techwiser.twai.ui.theme.ButtonShape
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.height
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material.icons.filled.ArrowUpward
import androidx.compose.ui.graphics.Color
import android.app.Activity
import android.content.Intent
import android.graphics.BitmapFactory
import android.speech.RecognizerIntent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.outlined.AttachFile
import androidx.compose.material.icons.outlined.Close
import androidx.compose.material.icons.outlined.ContentCopy
import androidx.compose.material.icons.outlined.Delete
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material.icons.outlined.Lock
import androidx.compose.material.icons.outlined.Mic
import androidx.compose.material.icons.outlined.Public
import androidx.compose.material.icons.outlined.Share
import androidx.compose.material.icons.outlined.TaskAlt
import androidx.compose.material.icons.outlined.Tune
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.Api
import com.techwiser.twai.data.ChatRequestOut
import com.techwiser.twai.data.ConfirmOut
import com.techwiser.twai.ui.ErrorBanner
import com.techwiser.twai.ui.appViewModel
import com.techwiser.twai.ui.bytes
import com.techwiser.twai.ui.friendly
import com.techwiser.twai.ui.openFile
import com.techwiser.twai.ui.shareText
import com.techwiser.twai.ui.theme.LocalExtra
import com.techwiser.twai.ui.toast
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private val MODES = listOf(
    "auto" to "Automatic",
    "single" to "Quick (one agent)",
    "agents" to "Specialists",
    "team" to "Team (plan, research, write)",
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(thread: String, onBack: () -> Unit, draft: String? = null, sharedFile: android.net.Uri? = null) {
    val context = LocalContext.current
    val app = TwApp.from(context)
    val vm = appViewModel(key = "chat:$thread") { ChatViewModel(it, thread) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    var input by rememberSaveable(thread) { mutableStateOf(draft.orEmpty()) }
    var showAttachments by remember { mutableStateOf(false) }
    var modeMenu by remember { mutableStateOf(false) }
    val list = rememberLazyListState()

    LifecycleResumeEffect(thread) {
        app.settings.openThread = thread
        vm.onForeground()
        onPauseOrDispose {
            if (app.settings.openThread == thread) app.settings.openThread = null
            vm.onBackground()
        }
    }
    LaunchedEffect(sharedFile) { if (sharedFile != null) vm.upload(sharedFile) }

    val pick = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri -> if (uri != null) vm.upload(uri) }
    val speech = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val heard = result.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()
            if (!heard.isNullOrBlank()) input = (input.trimEnd() + " " + heard).trim()
        }
    }

    // Keep the newest content in view.
    val itemCount = ui.messages.size + (if (ui.busy) 1 else 0) + (if (ui.pending != null) 1 else 0) +
        (if (ui.browser != null) 1 else 0) + (if (ui.requests.isNotEmpty()) 1 else 0)
    LaunchedEffect(itemCount, ui.streaming?.length?.div(200), ui.steps.size) {
        if (itemCount > 0) list.animateScrollToItem(itemCount - 1)
    }

    val title = ui.messages.firstOrNull { it.role == "user" }?.text?.take(60) ?: "New chat"

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                navigationIcon = {
                    IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
                },
                actions = {
                    IconButton(onClick = { showAttachments = true }) {
                        BadgedBox(badge = { if (ui.attachments.isNotEmpty()) Badge { Text("${ui.attachments.size}") } }) {
                            Icon(Icons.Outlined.AttachFile, contentDescription = "Attachments")
                        }
                    }
                    Box {
                        IconButton(onClick = { modeMenu = true }) { Icon(Icons.Outlined.Tune, contentDescription = "How to answer") }
                        DropdownMenu(expanded = modeMenu, onDismissRequest = { modeMenu = false }) {
                            MODES.forEach { (value, label) ->
                                DropdownMenuItem(
                                    text = { Text(label) },
                                    leadingIcon = { if (ui.mode == value) Icon(Icons.Filled.Check, contentDescription = null) },
                                    onClick = { vm.setMode(value); modeMenu = false },
                                )
                            }
                        }
                    }
                },
            )
        },
        bottomBar = {
            Composer(
                text = input,
                onText = { input = it },
                busy = ui.busy,
                uploading = ui.uploading,
                onSend = { vm.send(input); input = "" },
                onAttach = { pick.launch(arrayOf("*/*")) },
                onMic = {
                    val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                        .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                        .putExtra(RecognizerIntent.EXTRA_PROMPT, "Ask TW-ai")
                    runCatching { speech.launch(intent) }.onFailure { toast(context, "Voice input isn't available on this phone.") }
                },
            )
        },
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            ui.error?.let { ErrorBanner(it, Modifier.padding(12.dp, 8.dp), actionLabel = "Dismiss") { vm.dismissError() } }
            ui.notice?.let { ErrorBanner(it, Modifier.padding(12.dp, 8.dp), actionLabel = "Dismiss") { vm.dismissError() } }
            if (ui.loading) LinearProgressIndicator(Modifier.fillMaxWidth())
            if (!ui.loading && ui.messages.isEmpty() && !ui.busy) {
                Starter(Modifier.weight(1f)) { input = it }
            } else {
                LazyColumn(
                    state = list,
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 12.dp, vertical = 12.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    items(ui.messages, key = { it.key }) { m -> MessageBubble(m) }
                    if (ui.busy) item(key = "live") { LiveReply(ui.steps, ui.streaming) }
                    if (ui.requests.isNotEmpty()) item(key = "requests") { RequestsCard(ui.requests) }
                    ui.pending?.let { p ->
                        item(key = "confirm") { ConfirmCard(p, enabled = !ui.busy, onDecide = vm::confirm) }
                    }
                    ui.browser?.let { b ->
                        item(key = "browser") {
                            if (b.secret != null) {
                                SecretBox(b.secret.what, b.secret.site, onSubmit = vm::submitSecret, onCancel = vm::closeBrowser)
                            } else {
                                LiveBrowser(app.api, b.scope, b.reason, onDone = vm::browserDone, onClose = vm::closeBrowser)
                            }
                        }
                    }
                }
            }
        }
    }

    if (showAttachments) {
        val scope = rememberCoroutineScope()
        ModalBottomSheet(onDismissRequest = { showAttachments = false }) {
            Column(Modifier.padding(bottom = 24.dp)) {
                Text("Files in this chat", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(16.dp, 4.dp))
                Text("Readable by the assistant for 3 days, then deleted. Up to 10 MB each.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(horizontal = 16.dp))
                ui.attachments.forEach { a ->
                    ListItem(
                        headlineContent = { Text(a.name, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                        supportingContent = { Text(bytes(a.size)) },
                        leadingContent = { Icon(Icons.Outlined.Description, contentDescription = null) },
                        trailingContent = {
                            IconButton(onClick = { vm.removeAttachment(a.id) }) { Icon(Icons.Outlined.Delete, contentDescription = "Delete") }
                        },
                        modifier = Modifier.combinedClickableCompat {
                            scope.launch {
                                runCatching { vm.downloadAttachment(a) }
                                    .onSuccess { openFile(context, it) }
                                    .onFailure { toast(context, friendly(it)) }
                            }
                        },
                    )
                }
                TextButton(onClick = { pick.launch(arrayOf("*/*")) }, modifier = Modifier.padding(horizontal = 8.dp)) {
                    Icon(Icons.Outlined.AttachFile, contentDescription = null)
                    Spacer(Modifier.width(6.dp))
                    Text("Attach a file")
                }
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
private fun Modifier.combinedClickableCompat(onClick: () -> Unit): Modifier = this.combinedClickable(onClick = onClick)

@Composable
private fun Starter(modifier: Modifier, onPick: (String) -> Unit) {
    val ideas = listOf(
        "What's new in my mail?",
        "Remind me tomorrow at 9 to call the bank",
        "Summarize the attached PDF",
        "Compare earbuds under ₹1500 on Flipkart",
        "Research the latest Android flagship launches",
    )
    Column(modifier.fillMaxWidth().padding(24.dp), verticalArrangement = Arrangement.Center) {
        Text("How can I help?", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.size(6.dp))
        Text("Private by default: only you can read this chat. Work requests go to your department.",
            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.size(16.dp))
        ideas.forEach { idea ->
            AssistChip(onClick = { onPick(idea) }, label = { Text(idea) }, modifier = Modifier.padding(vertical = 2.dp))
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun MessageBubble(m: UiMessage) {
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    val haptics = LocalHapticFeedback.current
    var menu by remember { mutableStateOf(false) }
    val mine = m.role == "user"
    Column(Modifier.fillMaxWidth(), horizontalAlignment = if (mine) Alignment.End else Alignment.Start) {
        Box {
            val extra = LocalExtra.current
            Surface(
                // Web: .msg.user is a --mine bubble (12/12/4/12); .msg.assistant a raised card with a
                // bronze rule on the left (4/12/12/4).
                color = if (mine) extra.mine else MaterialTheme.colorScheme.surface,
                shape = if (mine) RoundedCornerShape(12.dp, 12.dp, 4.dp, 12.dp) else RoundedCornerShape(4.dp, 12.dp, 12.dp, 4.dp),
                border = BorderStroke(1.dp, extra.line),
                shadowElevation = if (mine) 0.dp else 1.dp,
                modifier = Modifier
                    .then(if (mine) Modifier.widthIn(max = 320.dp) else Modifier.fillMaxWidth())
                    .combinedClickable(onClick = {}, onLongClick = {
                        haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                        menu = true
                    }),
            ) {
                if (mine) {
                    Text(m.text, style = MaterialTheme.typography.bodyMedium, color = extra.inkStrong,
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 10.dp))
                } else {
                    Row(Modifier.height(IntrinsicSize.Min)) {
                        Box(Modifier.width(2.dp).fillMaxHeight().background(extra.bronze))
                        Column(Modifier.padding(horizontal = 18.dp, vertical = 14.dp)) {
                            SelectionContainer { Markdown(m.text) }
                        }
                    }
                }
            }
            DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                DropdownMenuItem(text = { Text("Copy") }, leadingIcon = { Icon(Icons.Outlined.ContentCopy, null) }, onClick = {
                    clipboard.setText(AnnotatedString(m.text)); menu = false
                })
                DropdownMenuItem(text = { Text("Share") }, leadingIcon = { Icon(Icons.Outlined.Share, null) }, onClick = {
                    shareText(context, m.text); menu = false
                })
            }
        }
        departmentLabel(m.department)?.let { d ->
            Text("Handled by $d", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.secondary,
                modifier = Modifier.padding(top = 4.dp, start = 4.dp))
        }
        m.dashboards.forEach { d -> DashboardView(d, Modifier.padding(top = 8.dp)) }
    }
}

@Composable
private fun LiveReply(steps: List<String>, streaming: String?) {
    val extra = LocalExtra.current
    Surface(color = MaterialTheme.colorScheme.surface, shape = RoundedCornerShape(4.dp, 12.dp, 12.dp, 4.dp),
        border = BorderStroke(1.dp, extra.line), shadowElevation = 1.dp,
        modifier = Modifier.fillMaxWidth().animateContentSize()) {
      Row(Modifier.height(IntrinsicSize.Min)) {
        Box(Modifier.width(2.dp).fillMaxHeight().background(extra.bronze))
        Column(Modifier.padding(horizontal = 18.dp, vertical = 14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            steps.takeLast(6).forEachIndexed { i, s ->
                val last = i == steps.takeLast(6).lastIndex && streaming.isNullOrEmpty()
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (last) CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp)
                    else Icon(Icons.Filled.Check, contentDescription = null, modifier = Modifier.size(14.dp),
                        tint = LocalExtra.current.good)
                    Spacer(Modifier.width(8.dp))
                    Text(s, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            if (!streaming.isNullOrEmpty()) {
                Markdown(streaming)
            } else if (steps.isEmpty()) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(8.dp))
                    Text("Thinking…", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
      }
    }
}

@Composable
private fun RequestsCard(requests: List<ChatRequestOut>) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Outlined.TaskAlt, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(6.dp))
                Text("Work from this chat", style = MaterialTheme.typography.titleSmall)
            }
            requests.forEach { r ->
                Row {
                    Text("${departmentLabel(r.department)} · ${r.label.replace('_', ' ')}",
                        style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
                    Text(r.state, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold)
                }
                r.result?.takeIf { it.isNotBlank() }?.let {
                    Text(it.take(300), style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSecondaryContainer)
                }
            }
        }
    }
}

@Composable
private fun ConfirmCard(p: ConfirmOut, enabled: Boolean, onDecide: (Boolean, Boolean) -> Unit) {
    OutlinedCardBox {
        Text(p.title, style = MaterialTheme.typography.titleMedium)
        p.details.forEach { (k, v) ->
            Column {
                Text(k.replace('_', ' ').replaceFirstChar { it.uppercase() }, style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                SelectionContainer { Text(v, style = MaterialTheme.typography.bodyMedium) }
            }
        }
        if (p.purchase) {
            Text("Is this for personal use or for work? Work orders go to the Accounts team, who pay with the company's details.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Button(shape = ButtonShape, onClick = { onDecide(true, false) }, enabled = enabled, modifier = Modifier.fillMaxWidth()) { Text("Personal: place it") }
            OutlinedButton(shape = ButtonShape, onClick = { onDecide(false, true) }, enabled = enabled, modifier = Modifier.fillMaxWidth()) { Text("Work: send to Accounts") }
            TextButton(onClick = { onDecide(false, false) }, enabled = enabled, modifier = Modifier.fillMaxWidth()) { Text("Cancel") }
        } else {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(shape = ButtonShape, onClick = { onDecide(true, false) }, enabled = enabled) {
                    Text(if (p.tool == "send_mail") "Send" else "Yes, go ahead")
                }
                OutlinedButton(shape = ButtonShape, onClick = { onDecide(false, false) }, enabled = enabled) { Text("No") }
            }
        }
    }
}

@Composable
private fun OutlinedCardBox(content: @Composable androidx.compose.foundation.layout.ColumnScope.() -> Unit) {
    androidx.compose.material3.OutlinedCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp), content = content)
    }
}

@Composable
private fun SecretBox(what: String, site: String, onSubmit: (String, (String?) -> Unit) -> Unit, onCancel: () -> Unit) {
    var value by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var sending by remember { mutableStateOf(false) }
    OutlinedCardBox {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Outlined.Lock, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text(what.ifBlank { "Secure entry" }, style = MaterialTheme.typography.titleMedium)
        }
        Text("Typed straight into the page on $site. It isn't saved, shown to the assistant, or kept in this chat.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        OutlinedTextField(
            value = value, onValueChange = { value = it }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
            modifier = Modifier.fillMaxWidth(),
        )
        error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(enabled = value.isNotEmpty() && !sending, onClick = {
                sending = true
                onSubmit(value) { e -> sending = false; error = e; if (e == null) value = "" }
            }) { Text("Enter") }
            TextButton(onClick = onCancel) { Text("Cancel") }
        }
    }
}

/** The live browser the agent handed over: tap to click, type, keys; "Done" lets the agent carry on. */
@Composable
private fun LiveBrowser(api: Api, scope: String, reason: String, onDone: () -> Unit, onClose: () -> Unit) {
    var frame by remember { mutableStateOf<ImageBitmap?>(null) }
    var size by remember { mutableStateOf(IntSize.Zero) }
    var typing by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var tick by remember { mutableStateOf(0) }
    val co = rememberCoroutineScope()
    LaunchedEffect(scope, tick) {
        while (true) {
            runCatching { api.browserScreen(scope) }
                .onSuccess { bytes -> BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.let { frame = it.asImageBitmap() }; error = null }
                .onFailure { error = friendly(it) }
            delay(1_500)
        }
    }
    fun act(action: String, x: Int = 0, y: Int = 0, text: String = "") = co.launch {
        runCatching { api.browserAct(scope, action, x, y, text) }.onFailure { error = friendly(it) }
        tick++
    }
    OutlinedCardBox {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Outlined.Public, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Live browser", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            IconButton(onClick = onClose) { Icon(Icons.Outlined.Close, contentDescription = "Close") }
        }
        if (reason.isNotBlank()) Text(reason, style = MaterialTheme.typography.bodyMedium)
        error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
        val img = frame
        if (img == null) {
            Box(Modifier.fillMaxWidth().heightIn(min = 160.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
        } else {
            Image(
                bitmap = img, contentDescription = "The page",
                contentScale = ContentScale.FillWidth,
                modifier = Modifier.fillMaxWidth()
                    .onSizeChanged { size = it }
                    .pointerInput(img) {
                        detectTapGestures { pos ->
                            if (size.width > 0) {
                                val scale = img.width.toFloat() / size.width
                                act("click", (pos.x * scale).toInt(), (pos.y * scale).toInt())
                            }
                        }
                    },
            )
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(value = typing, onValueChange = { typing = it }, singleLine = true,
                placeholder = { Text("Type into the page") }, modifier = Modifier.weight(1f))
            IconButton(enabled = typing.isNotEmpty(), onClick = { act("type", text = typing); typing = "" }) {
                Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Type")
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            listOf("Enter", "Tab", "Backspace").forEach { k ->
                TextButton(onClick = { act("key", text = k) }) { Text(k) }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            TextButton(onClick = { act("scroll", y = -600) }) { Text("Up") }
            TextButton(onClick = { act("scroll", y = 600) }) { Text("Down") }
            TextButton(onClick = { act("back") }) { Text("Back") }
        }
        Button(shape = ButtonShape, onClick = onDone, modifier = Modifier.fillMaxWidth()) { Text("Done, carry on") }
    }
}

@Composable
private fun Composer(
    text: String,
    onText: (String) -> Unit,
    busy: Boolean,
    uploading: Boolean,
    onSend: () -> Unit,
    onAttach: () -> Unit,
    onMic: () -> Unit,
) {
    // Web: .composer, a raised card with a line and a soft shadow, the send button a dark 8 dp square.
    val extra = LocalExtra.current
    Column(Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.background).navigationBarsPadding().imePadding()) {
        AnimatedVisibility(uploading) { LinearProgressIndicator(Modifier.fillMaxWidth()) }
        Surface(
            shape = RoundedCornerShape(12.dp),
            color = MaterialTheme.colorScheme.surface,
            border = BorderStroke(1.dp, extra.line),
            shadowElevation = 4.dp,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
        ) {
            Column(Modifier.padding(start = 16.dp, end = 10.dp, top = 6.dp, bottom = 8.dp)) {
                TextField(
                    value = text,
                    onValueChange = onText,
                    placeholder = { Text("Ask anything", color = extra.ink3) },
                    maxLines = 6,
                    textStyle = MaterialTheme.typography.bodyLarge,
                    colors = TextFieldDefaults.colors(
                        focusedContainerColor = Color.Transparent, unfocusedContainerColor = Color.Transparent,
                        disabledContainerColor = Color.Transparent,
                        focusedIndicatorColor = Color.Transparent, unfocusedIndicatorColor = Color.Transparent,
                        disabledIndicatorColor = Color.Transparent,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    IconButton(onClick = onAttach) { Icon(Icons.Outlined.AttachFile, contentDescription = "Attach a file", tint = extra.ink2) }
                    IconButton(onClick = onMic) { Icon(Icons.Outlined.Mic, contentDescription = "Speak", tint = extra.ink2) }
                    Spacer(Modifier.weight(1f))
                    FilledIconButton(
                        onClick = onSend,
                        enabled = text.isNotBlank() && !busy,
                        shape = RoundedCornerShape(8.dp),
                        colors = IconButtonDefaults.filledIconButtonColors(
                            disabledContainerColor = MaterialTheme.colorScheme.surfaceVariant,
                        ),
                    ) {
                        if (busy) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                        else Icon(Icons.Filled.ArrowUpward, contentDescription = "Send")
                    }
                }
            }
        }
    }
}
