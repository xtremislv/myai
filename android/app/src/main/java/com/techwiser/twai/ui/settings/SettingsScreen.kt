package com.techwiser.twai.ui.settings

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings as AndroidSettings
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.Logout
import androidx.compose.material.icons.automirrored.outlined.OpenInNew
import androidx.compose.material.icons.outlined.DarkMode
import androidx.compose.material.icons.outlined.Fingerprint
import androidx.compose.material.icons.outlined.Key
import androidx.compose.material.icons.outlined.Mail
import androidx.compose.material.icons.outlined.Notifications
import androidx.compose.material.icons.outlined.Person
import androidx.compose.material.icons.outlined.Smartphone
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import com.techwiser.twai.BuildConfig
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.AppConfig
import com.techwiser.twai.data.DeviceOut
import com.techwiser.twai.data.Prefs
import com.techwiser.twai.data.Profile
import com.techwiser.twai.data.ThemeMode
import com.techwiser.twai.notify.Notifier
import com.techwiser.twai.notify.Push
import com.techwiser.twai.ui.ErrorBanner
import com.techwiser.twai.ui.Security
import com.techwiser.twai.ui.appViewModel
import com.techwiser.twai.ui.chat.departmentLabel
import com.techwiser.twai.ui.friendly
import com.techwiser.twai.ui.login.RecoveryCodeDialog
import com.techwiser.twai.ui.relative
import com.techwiser.twai.ui.toast
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class SettingsUi(
    val profile: Profile? = null,
    val prefs: Prefs? = null,
    val devices: List<DeviceOut> = emptyList(),
    val config: AppConfig? = null,
    val error: String? = null,
)

class SettingsViewModel(private val app: TwApp) : ViewModel() {
    private val _ui = MutableStateFlow(SettingsUi())
    val ui = _ui.asStateFlow()

    fun refresh() = viewModelScope.launch {
        try {
            val profile = app.api.me()
            app.settings.isAdmin = profile.is_admin
            app.settings.departments = profile.departments.toSet()
            val prefs = runCatching { app.api.prefs() }.getOrNull()
            val devices = runCatching { app.api.devices() }.getOrDefault(emptyList())
            val config = runCatching { app.api.appConfig() }.getOrNull()
            _ui.update { SettingsUi(profile, prefs, devices, config) }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _ui.update { it.copy(error = friendly(e)) }
        }
    }

    fun savePrefs(p: Prefs) = viewModelScope.launch {
        runCatching { app.api.savePrefs(p) }
            .onSuccess { saved -> _ui.update { it.copy(prefs = saved, error = null) } }
            .onFailure { e -> _ui.update { it.copy(error = friendly(e)) } }
    }

    fun signOutDevice(id: String) = viewModelScope.launch {
        runCatching { app.api.signOutDevice(id) }.onFailure { e -> _ui.update { it.copy(error = friendly(e)) } }
        refresh()
    }

    fun signOut() = viewModelScope.launch { app.api.signOut() }

    suspend fun newRecoveryCode(password: String): String = app.api.newRecoveryCode(password).recovery_code
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen() {
    val context = LocalContext.current
    val app = TwApp.from(context)
    val vm = appViewModel { SettingsViewModel(it) }
    val ui by vm.ui.collectAsStateWithLifecycle()
    val theme by app.settings.theme.collectAsStateWithLifecycle()
    val lock by app.settings.appLock.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    var askPassword by remember { mutableStateOf(false) }
    var code by remember { mutableStateOf<String?>(null) }
    var editMail by remember { mutableStateOf(false) }
    var confirmSignOut by remember { mutableStateOf(false) }
    LifecycleResumeEffect(Unit) {
        vm.refresh()
        onPauseOrDispose { }
    }

    Scaffold(topBar = { TopAppBar(title = { Text("Settings") }) }) { padding ->
        Column(Modifier.padding(padding).fillMaxSize().verticalScroll(rememberScrollState())) {
            ui.error?.let { ErrorBanner(it, Modifier.padding(12.dp)) { vm.refresh() } }

            Section("Account")
            val p = ui.profile
            ListItem(
                leadingContent = { Icon(Icons.Outlined.Person, null) },
                headlineContent = { Text(p?.username ?: app.settings.username) },
                supportingContent = {
                    val parts = buildList {
                        if (p?.is_admin == true) add("Admin")
                        p?.departments?.forEach { add(departmentLabel(it) ?: it) }
                    }
                    Text(if (parts.isEmpty()) "No department" else parts.joinToString(" · "))
                },
            )
            ListItem(
                leadingContent = { Icon(Icons.Outlined.Key, null) },
                headlineContent = { Text("New recovery code") },
                supportingContent = { Text("Replaces your old code. Needs your password.") },
                modifier = Modifier.clickable { askPassword = true },
            )

            Section("Notifications")
            val allowed = Notifier.canPost(context)
            ListItem(
                leadingContent = { Icon(Icons.Outlined.Notifications, null) },
                headlineContent = { Text(if (allowed) "Notifications are on" else "Notifications are off") },
                supportingContent = {
                    val how = when {
                        ui.config?.push_enabled == true && Push.available(context) -> "Instant (push)"
                        !BuildConfig.HAS_FIREBASE -> "Checked every 15 minutes (this build has no Firebase)"
                        ui.config?.push_enabled == false -> "Checked every 15 minutes (push isn't set up on the server)"
                        else -> "Checked every 15 minutes"
                    }
                    Text("$how. Tap to choose sounds and which kinds you get.")
                },
                modifier = Modifier.clickable {
                    val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        Intent(AndroidSettings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(AndroidSettings.EXTRA_APP_PACKAGE, context.packageName)
                    } else {
                        Intent(AndroidSettings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}"))
                    }
                    context.startActivity(intent)
                },
            )
            ui.prefs?.let { prefs ->
                ListItem(
                    leadingContent = { Icon(Icons.Outlined.Mail, null) },
                    headlineContent = { Text("Email and morning digest") },
                    supportingContent = {
                        Text(buildString {
                            append(if (prefs.email.isBlank()) "No email address" else prefs.email)
                            append(" · ")
                            append(prefs.digest_hour?.let { "digest at %02d:00".format(it) } ?: "no digest")
                            if (prefs.email_reminders) append(" · reminders by email")
                        })
                    },
                    modifier = Modifier.clickable { editMail = true },
                )
            }

            Section("Security")
            ListItem(
                leadingContent = { Icon(Icons.Outlined.Fingerprint, null) },
                headlineContent = { Text("App lock") },
                supportingContent = {
                    Text(if (Security.available(context)) "Ask for fingerprint, face or screen lock when opening the app"
                    else "Set a screen lock on this phone to use this")
                },
                trailingContent = {
                    Switch(checked = lock, enabled = Security.available(context), onCheckedChange = { on ->
                        Security.confirm(context, if (on) "Turn on app lock" else "Turn off app lock") { ok ->
                            if (ok) app.settings.setAppLock(on)
                        }
                    })
                },
            )

            Section("Appearance")
            ListItem(leadingContent = { Icon(Icons.Outlined.DarkMode, null) }, headlineContent = { Text("Theme") })
            ThemeMode.entries.forEach { mode ->
                ListItem(
                    headlineContent = {
                        Text(when (mode) { ThemeMode.SYSTEM -> "Follow the system"; ThemeMode.LIGHT -> "Light"; ThemeMode.DARK -> "Dark" })
                    },
                    trailingContent = { RadioButton(selected = theme == mode, onClick = { app.settings.setTheme(mode) }) },
                    modifier = Modifier.clickable { app.settings.setTheme(mode) },
                )
            }

            Section("Signed-in phones")
            ui.devices.forEach { d ->
                val mine = d.id == app.api.deviceId
                ListItem(
                    leadingContent = { Icon(Icons.Outlined.Smartphone, null) },
                    headlineContent = { Text(d.name + if (mine) " (this phone)" else "") },
                    supportingContent = {
                        Text(listOfNotNull(d.last_used_at?.let { "used ${relative(it)}" }, if (d.push) "push on" else null).joinToString(" · "))
                    },
                    trailingContent = {
                        if (!mine) TextButton(onClick = { vm.signOutDevice(d.id) }) { Text("Sign out") }
                    },
                )
            }

            Section("More")
            ListItem(
                leadingContent = { Icon(Icons.AutoMirrored.Outlined.OpenInNew, null) },
                headlineContent = { Text("Open the web app") },
                supportingContent = { Text("Mailbox sign-in, saved details, templates and admin pages") },
                modifier = Modifier.clickable {
                    runCatching { CustomTabsIntent.Builder().build().launchUrl(context, Uri.parse(app.api.webUrl())) }
                        .onFailure { toast(context, "No browser found.") }
                },
            )
            ListItem(
                leadingContent = { Icon(Icons.AutoMirrored.Outlined.Logout, null, tint = MaterialTheme.colorScheme.error) },
                headlineContent = { Text("Sign out of this phone", color = MaterialTheme.colorScheme.error) },
                modifier = Modifier.clickable { confirmSignOut = true },
            )
            Text("TW-ai ${BuildConfig.VERSION_NAME} · ${app.settings.server}",
                style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(16.dp))
        }
    }

    if (askPassword) {
        var pw by remember { mutableStateOf("") }
        var err by remember { mutableStateOf<String?>(null) }
        AlertDialog(
            onDismissRequest = { askPassword = false },
            title = { Text("New recovery code") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(value = pw, onValueChange = { pw = it }, label = { Text("Your password") },
                        singleLine = true, visualTransformation = PasswordVisualTransformation())
                    err?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    scope.launch {
                        try {
                            code = vm.newRecoveryCode(pw)
                            askPassword = false
                        } catch (e: Exception) {
                            err = friendly(e)
                        }
                    }
                }) { Text("Create") }
            },
            dismissButton = { TextButton(onClick = { askPassword = false }) { Text("Cancel") } },
        )
    }
    code?.let { RecoveryCodeDialog(it) { code = null } }

    if (editMail) ui.prefs?.let { prefs -> MailPrefsDialog(prefs, onDismiss = { editMail = false }) { vm.savePrefs(it); editMail = false } }

    if (confirmSignOut) {
        AlertDialog(
            onDismissRequest = { confirmSignOut = false },
            title = { Text("Sign out?") },
            text = { Text("This phone stops getting reminders and replies until you sign in again. Your chats stay on the server.") },
            confirmButton = { TextButton(onClick = { confirmSignOut = false; vm.signOut() }) { Text("Sign out") } },
            dismissButton = { TextButton(onClick = { confirmSignOut = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun Section(title: String) {
    HorizontalDivider(Modifier.padding(top = 8.dp))
    Text(title, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.secondary,
        modifier = Modifier.padding(start = 16.dp, top = 12.dp, bottom = 4.dp))
}

@Composable
private fun MailPrefsDialog(prefs: Prefs, onDismiss: () -> Unit, onSave: (Prefs) -> Unit) {
    var email by remember { mutableStateOf(prefs.email) }
    var hour by remember { mutableStateOf(prefs.digest_hour?.toString().orEmpty()) }
    var digestEmail by remember { mutableStateOf(prefs.digest_email) }
    var reminders by remember { mutableStateOf(prefs.email_reminders) }
    var zone by remember { mutableStateOf(prefs.timezone.ifBlank { java.util.TimeZone.getDefault().id }) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Email and digest") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(value = email, onValueChange = { email = it }, label = { Text("Email address") }, singleLine = true)
                OutlinedTextField(value = hour, onValueChange = { hour = it.filter(Char::isDigit).take(2) },
                    label = { Text("Morning digest hour (0-23, empty = off)") }, singleLine = true)
                OutlinedTextField(value = zone, onValueChange = { zone = it }, label = { Text("Time zone") }, singleLine = true)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Switch(checked = digestEmail, onCheckedChange = { digestEmail = it })
                    Text("  Email me the digest too")
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Switch(checked = reminders, onCheckedChange = { reminders = it })
                    Text("  Email me every reminder")
                }
                Text("Connect your mailbox (Google, Microsoft or app password) in the web app.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        },
        confirmButton = {
            TextButton(onClick = {
                onSave(prefs.copy(email = email.trim(), digest_hour = hour.toIntOrNull()?.takeIf { it in 0..23 },
                    digest_email = digestEmail, email_reminders = reminders, timezone = zone.trim()))
            }) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
