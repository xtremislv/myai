package com.techwiser.twai.ui.login

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import androidx.compose.ui.unit.em
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.outlined.Lock
import com.techwiser.twai.ui.theme.LocalExtra
import com.techwiser.twai.ui.theme.EyebrowStyle
import com.techwiser.twai.ui.theme.ButtonShape
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Visibility
import androidx.compose.material.icons.outlined.VisibilityOff
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.Settings
import com.techwiser.twai.notify.Push
import com.techwiser.twai.ui.friendly
import com.techwiser.twai.ui.shareText
import com.techwiser.twai.ui.theme.Mono
import kotlinx.coroutines.launch

@Composable
private fun PasswordField(value: String, onValue: (String) -> Unit, label: String, last: Boolean = false, onDone: () -> Unit = {}) {
    var show by remember { mutableStateOf(false) }
    OutlinedTextField(
        value = value, onValueChange = onValue, label = { Text(label) }, singleLine = true,
        visualTransformation = if (show) VisualTransformation.None else PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = if (last) ImeAction.Done else ImeAction.Next),
        keyboardActions = KeyboardActions(onDone = { onDone() }),
        trailingIcon = {
            IconButton(onClick = { show = !show }) {
                Icon(if (show) Icons.Outlined.VisibilityOff else Icons.Outlined.Visibility, if (show) "Hide" else "Show")
            }
        },
        modifier = Modifier.fillMaxWidth(),
    )
}

/** The web app's sign-in: a raised 14 dp card on the page, brand mark on top. */
@Composable
private fun Page(content: @Composable () -> Unit) {
    val extra = LocalExtra.current
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            Modifier.safeDrawingPadding().imePadding().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 40.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Surface(
                shape = RoundedCornerShape(14.dp),
                color = MaterialTheme.colorScheme.surface,
                border = BorderStroke(1.dp, extra.line),
                shadowElevation = 8.dp,
                modifier = Modifier.widthIn(max = 440.dp).fillMaxWidth(),
            ) {
                Column(Modifier.padding(horizontal = 28.dp, vertical = 32.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    content()
                }
            }
        }
    }
}

/** The web app's brand: a dark "TW" tile, "TW-ai" and "BY TW LABS". */
@Composable
fun BrandMark(modifier: Modifier = Modifier) {
    val extra = LocalExtra.current
    Row(modifier, verticalAlignment = Alignment.CenterVertically) {
        Box(
            Modifier.size(44.dp).clip(RoundedCornerShape(10.dp)).background(extra.inkStrong),
            contentAlignment = Alignment.Center,
        ) {
            Text("TW", style = Mono.copy(fontWeight = FontWeight.Bold, fontSize = 14.sp), color = MaterialTheme.colorScheme.background)
        }
        Spacer(Modifier.width(14.dp))
        Column {
            Text("TW-ai", style = MaterialTheme.typography.titleSmall, color = extra.inkStrong)
            Text("BY TW LABS", style = EyebrowStyle.copy(fontSize = 9.5.sp, letterSpacing = 0.14.em), color = extra.ink3)
        }
    }
}

@Composable
fun LoginScreen() {
    val context = LocalContext.current
    val app = TwApp.from(context)
    val scope = rememberCoroutineScope()
    var recovering by rememberSaveable { mutableStateOf(false) }
    var server by rememberSaveable { mutableStateOf(app.settings.server) }
    var showServer by rememberSaveable { mutableStateOf(false) }
    var username by rememberSaveable { mutableStateOf(app.settings.username) }
    var password by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    var newPassword by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var newCode by remember { mutableStateOf<String?>(null) }

    fun submit() {
        if (busy) return
        busy = true
        error = null
        app.settings.server = server
        scope.launch {
            try {
                val push = Push.token(context)
                if (recovering) {
                    if (newPassword.length < 12) throw IllegalArgumentException("Choose a password of at least 12 characters.")
                    val made = app.api.recover(username, code, newPassword, push, publish = false).recovery_code
                    if (made.isNullOrBlank()) { app.api.publishAuth() } else { newCode = made }
                } else {
                    app.api.login(username, password, push)
                }
            } catch (e: Exception) {
                error = friendly(e)
            } finally {
                busy = false
            }
        }
    }

    Page {
        BrandMark(Modifier.padding(bottom = 22.dp))
        Text(if (recovering) "Reset your password" else "Welcome back", style = MaterialTheme.typography.headlineLarge,
            color = LocalExtra.current.inkStrong)
        Text(if (recovering) "Set a new password with your recovery code." else "Sign in to your private workspace",
            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = username, onValueChange = { username = it }, label = { Text("Username") }, singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii, imeAction = ImeAction.Next),
            modifier = Modifier.fillMaxWidth(),
        )
        if (recovering) {
            OutlinedTextField(value = code, onValueChange = { code = it }, label = { Text("Recovery code") }, singleLine = true,
                textStyle = Mono, modifier = Modifier.fillMaxWidth())
            PasswordField(newPassword, { newPassword = it }, "New password (12+ characters)", last = true, onDone = ::submit)
        } else {
            PasswordField(password, { password = it }, "Password", last = true, onDone = ::submit)
        }
        error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium) }
        Button(onClick = ::submit, enabled = !busy && username.isNotBlank() &&
            (if (recovering) code.isNotBlank() && newPassword.isNotBlank() else password.isNotEmpty()),
            shape = ButtonShape, modifier = Modifier.fillMaxWidth().heightIn(min = 46.dp).padding(top = 6.dp)) {
            if (busy) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
            else {
                Text(if (recovering) "Set password" else "Sign in", fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.width(8.dp))
                Icon(Icons.AutoMirrored.Filled.ArrowForward, contentDescription = null, modifier = Modifier.size(18.dp))
            }
        }
        TextButton(onClick = { recovering = !recovering; error = null }) {
            Text(if (recovering) "Back to sign in" else "Forgot your password?")
        }
        TextButton(onClick = { showServer = !showServer }) { Text("Server: ${Settings.normalizeServer(server)}") }
        if (showServer) {
            OutlinedTextField(value = server, onValueChange = { server = it }, label = { Text("Server address") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth())
        }
        HorizontalDivider(color = LocalExtra.current.line, modifier = Modifier.padding(top = 4.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Outlined.Lock, contentDescription = null, tint = LocalExtra.current.ink3, modifier = Modifier.size(15.dp))
            Spacer(Modifier.width(8.dp))
            Text("Private chats are encrypted with a key only you can unlock.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }

    newCode?.let { RecoveryCodeDialog(it) { newCode = null; app.api.publishAuth() } }
}

/** Shown right after a first password change: the admin set the old one, so chats get their own key now. */
@Composable
fun ChangePasswordScreen() {
    val context = LocalContext.current
    val app = TwApp.from(context)
    val scope = rememberCoroutineScope()
    var old by remember { mutableStateOf("") }
    var new by remember { mutableStateOf("") }
    var again by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var code by remember { mutableStateOf<String?>(null) }

    fun submit() {
        error = when {
            new.length < 12 -> "Use at least 12 characters."
            new != again -> "The two new passwords don't match."
            new == old -> "Choose a password different from the one your admin gave you."
            else -> null
        }
        if (error != null || busy) return
        busy = true
        scope.launch {
            try {
                // Keep the app on this screen until the code is acknowledged.
                val made = app.api.changePassword(old, new, publish = false).recovery_code
                if (made.isNullOrBlank()) { app.api.publishAuth() } else { code = made }
            } catch (e: Exception) {
                error = friendly(e)
            } finally {
                busy = false
            }
        }
    }

    Page {
        BrandMark(Modifier.padding(bottom = 22.dp))
        Text("Choose your own password", style = MaterialTheme.typography.headlineMedium)
        Text("Your admin set your first password, so private chat stays off until you replace it. " +
            "Only your new password will unlock your private chats.",
            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        PasswordField(old, { old = it }, "Password from your admin")
        PasswordField(new, { new = it }, "New password (12+ characters)")
        PasswordField(again, { again = it }, "New password again", last = true, onDone = ::submit)
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button(shape = ButtonShape, onClick = ::submit, enabled = !busy, modifier = Modifier.fillMaxWidth()) {
            if (busy) CircularProgressIndicator(Modifier.height(18.dp), strokeWidth = 2.dp) else Text("Save password")
        }
        TextButton(onClick = { scope.launch { app.api.signOut() } }) { Text("Sign out") }
    }

    code?.let { c -> RecoveryCodeDialog(c) { code = null; app.api.publishAuth() } }
}

@Composable
fun RecoveryCodeDialog(code: String, onDone: () -> Unit) {
    val clipboard = LocalClipboardManager.current
    val context = LocalContext.current
    AlertDialog(
        onDismissRequest = {},
        title = { Text("Save your recovery code") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("It's shown only once. With it you can set a new password and keep your private chats if you forget yours. " +
                    "Anyone holding it can read your private chats, so keep it somewhere safe (a password manager).")
                Text(code, style = Mono.copy(fontSize = MaterialTheme.typography.titleMedium.fontSize))
                TextButton(onClick = { clipboard.setText(AnnotatedString(code)) }) { Text("Copy") }
                TextButton(onClick = { shareText(context, code) }) { Text("Save to password manager…") }
            }
        },
        confirmButton = { Button(shape = ButtonShape, onClick = onDone) { Text("I've saved it") } },
    )
}
