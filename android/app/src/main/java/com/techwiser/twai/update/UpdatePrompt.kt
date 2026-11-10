package com.techwiser.twai.update

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.techwiser.twai.BuildConfig
import kotlinx.coroutines.launch

/**
 * Shown over every screen: checks for a new app whenever TW-ai comes to the front and offers it.
 * A required update (the server's min_version_code) can't be put off.
 */
@Composable
fun UpdatePrompt() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val state by Updater.state.collectAsStateWithLifecycle()
    var dismissed by remember { mutableIntStateOf(Updater.dismissedVersion) }
    LifecycleResumeEffect(Unit) {
        dismissed = Updater.dismissedVersion
        scope.launch { Updater.check(context) }
        onPauseOrDispose { }
    }

    when (val s = state) {
        is UpdateState.Available -> if (s.required || s.release.version_code != dismissed) {
            AlertDialog(
                onDismissRequest = { if (!s.required) { Updater.later(s.release); dismissed = s.release.version_code } },
                properties = DialogProperties(dismissOnBackPress = !s.required, dismissOnClickOutside = !s.required),
                title = { Text(if (s.required) "Update needed" else "Update available") },
                text = {
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(
                            if (s.required) "This version of TW-ai no longer works with the server. Install version ${s.release.version_name} to go on."
                            else "TW-ai ${s.release.version_name} is ready (you have ${BuildConfig.VERSION_NAME})."
                        )
                        if (s.release.notes.isNotBlank()) {
                            Text(s.release.notes, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Text("%.1f MB. Android may ask you to allow TW-ai to install updates; that's needed once.".format(s.release.size / 1048576.0),
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                },
                confirmButton = {
                    TextButton(onClick = { scope.launch { Updater.install(context, s.release, interactive = true) } }) { Text("Update now") }
                },
                dismissButton = if (s.required) null else {
                    { TextButton(onClick = { Updater.later(s.release); dismissed = s.release.version_code }) { Text("Later") } }
                },
            )
        }
        is UpdateState.Downloading -> AlertDialog(
            onDismissRequest = {},
            properties = DialogProperties(dismissOnBackPress = false, dismissOnClickOutside = false),
            title = { Text("Downloading ${s.release.version_name}") },
            text = { LinearProgressIndicator(progress = { s.progress.coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth()) },
            confirmButton = {},
        )
        is UpdateState.Installing -> if (Updater.appInForeground()) AlertDialog(
            onDismissRequest = {},
            properties = DialogProperties(dismissOnBackPress = false, dismissOnClickOutside = false),
            title = { Text("Installing ${s.release.version_name}") },
            text = { Text("TW-ai restarts when it's done.") },
            confirmButton = {},
        )
        is UpdateState.Failed -> if (s.release.version_code != dismissed) AlertDialog(
            onDismissRequest = { Updater.later(s.release); dismissed = s.release.version_code },
            title = { Text("Update didn't install") },
            text = { Text(s.message) },
            confirmButton = {
                TextButton(onClick = { scope.launch { Updater.install(context, s.release, interactive = true) } }) { Text("Try again") }
            },
            dismissButton = { TextButton(onClick = { Updater.later(s.release); dismissed = s.release.version_code }) { Text("Later") } },
        )
        UpdateState.Idle -> Unit
    }
}
