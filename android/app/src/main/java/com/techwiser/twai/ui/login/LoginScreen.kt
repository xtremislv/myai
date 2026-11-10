package com.techwiser.twai.ui.login

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

@Composable
private fun Page(content: @Composable () -> Unit) {
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            Modifier.safeDrawingPadding().imePadding().verticalScroll(rememberScrollState()).padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Column(Modifier.widthIn(max = 440.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) { content() }
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
        Spacer(Modifier.height(32.dp))
        Text("TW-ai", style = MaterialTheme.typography.headlineLarge)
        Text(if (recovering) "Set a new password with your recovery code." else "Sign in to your assistant.",
            style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(8.dp))
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
            modifier = Modifier.fillMaxWidth()) {
            if (busy) CircularProgressIndicator(Modifier.height(18.dp), strokeWidth = 2.dp)
            else Text(if (recovering) "Set password" else "Sign in")
        }
        TextButton(onClick = { recovering = !recovering; error = null }) {
            Text(if (recovering) "Back to sign in" else "Forgot your password?")
        }
        TextButton(onClick = { showServer = !showServer }) { Text("Server: ${Settings.normalizeServer(server)}") }
        if (showServer) {
            OutlinedTextField(value = server, onValueChange = { server = it }, label = { Text("Server address") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth())
        }
        Text("Your private chats are encrypted with a key only your password (or recovery code) unlocks. " +
            "This phone keeps its sign-in in the Android Keystore.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
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
        Spacer(Modifier.height(32.dp))
        Text("Choose your own password", style = MaterialTheme.typography.headlineMedium)
        Text("Your admin set your first password, so private chat stays off until you replace it. " +
            "Only your new password will unlock your private chats.",
            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        PasswordField(old, { old = it }, "Password from your admin")
        PasswordField(new, { new = it }, "New password (12+ characters)")
        PasswordField(again, { again = it }, "New password again", last = true, onDone = ::submit)
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button(onClick = ::submit, enabled = !busy, modifier = Modifier.fillMaxWidth()) {
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
        confirmButton = { Button(onClick = onDone) { Text("I've saved it") } },
    )
}
