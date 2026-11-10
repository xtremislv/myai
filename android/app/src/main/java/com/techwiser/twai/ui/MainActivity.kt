package com.techwiser.twai.ui

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Chat
import androidx.compose.material.icons.automirrored.outlined.Chat
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.VerifiedUser
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material.icons.outlined.Lock
import androidx.compose.material.icons.outlined.Notifications
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material.icons.outlined.VerifiedUser
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.IntentCompat
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.AuthState
import com.techwiser.twai.ui.alerts.AlertsScreen
import com.techwiser.twai.ui.chat.ChatListScreen
import com.techwiser.twai.ui.chat.ChatScreen
import com.techwiser.twai.ui.files.FilesScreen
import com.techwiser.twai.ui.login.ChangePasswordScreen
import com.techwiser.twai.ui.login.LoginScreen
import com.techwiser.twai.ui.settings.SettingsScreen
import com.techwiser.twai.ui.theme.TwTheme
import com.techwiser.twai.ui.work.WorkScreen
import com.techwiser.twai.ui.work.workTabs
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** Something to open once signed in: a deep link (twai://...) or content shared from another app. */
data class Arrival(val link: String? = null, val text: String? = null, val file: Uri? = null)

class MainActivity : FragmentActivity() {
    private val arrivals = MutableStateFlow<Arrival?>(null)
    private val locked = MutableStateFlow(false)
    private var stoppedAt = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val app = TwApp.from(this)
        if (savedInstanceState == null) {
            locked.value = app.settings.appLock.value
            arrivals.value = arrivalOf(intent)
        }
        setContent {
            val theme by app.settings.theme.collectAsStateWithLifecycle()
            TwTheme(theme) {
                Root(arrivals.asStateFlow(), { arrivals.value = null }, locked.asStateFlow()) { unlock() }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        arrivalOf(intent)?.let { arrivals.value = it }
    }

    override fun onStop() {
        super.onStop()
        stoppedAt = SystemClock.elapsedRealtime()
    }

    override fun onStart() {
        super.onStart()
        val app = TwApp.from(this)
        // Re-lock after 30 seconds away.
        if (app.settings.appLock.value && stoppedAt > 0 && SystemClock.elapsedRealtime() - stoppedAt > 30_000) {
            locked.value = true
        }
    }

    private fun unlock() {
        Security.confirm(this, "Unlock TW-ai") { ok -> if (ok) locked.value = false }
    }

    private fun arrivalOf(intent: Intent?): Arrival? {
        intent ?: return null
        return when (intent.action) {
            Intent.ACTION_SEND -> {
                val text = listOfNotNull(intent.getStringExtra(Intent.EXTRA_SUBJECT), intent.getStringExtra(Intent.EXTRA_TEXT))
                    .joinToString("\n").ifBlank { null }
                val file = IntentCompat.getParcelableExtra(intent, Intent.EXTRA_STREAM, Uri::class.java)
                Arrival(link = "twai://chat/new", text = text, file = file)
            }
            Intent.ACTION_VIEW -> intent.data?.takeIf { it.scheme == "twai" }?.let { Arrival(link = it.toString()) }
            else -> null
        }
    }
}

@Composable
private fun Root(arrivals: StateFlow<Arrival?>, consumed: () -> Unit, lockedFlow: StateFlow<Boolean>, unlock: () -> Unit) {
    val app = TwApp.from(LocalContext.current)
    val auth by app.api.auth.collectAsStateWithLifecycle()
    val locked by lockedFlow.collectAsStateWithLifecycle()
    when (auth) {
        AuthState.SIGNED_OUT -> LoginScreen()
        AuthState.MUST_CHANGE_PASSWORD -> ChangePasswordScreen()
        AuthState.SIGNED_IN -> if (locked) LockScreen(unlock) else Main(arrivals, consumed)
    }
}

@Composable
private fun LockScreen(unlock: () -> Unit) {
    LaunchedEffect(Unit) { unlock() }
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center,
            modifier = Modifier.fillMaxSize().padding(32.dp)) {
            Icon(Icons.Outlined.Lock, contentDescription = null, modifier = Modifier.size(48.dp))
            Spacer(Modifier.height(16.dp))
            Text("TW-ai is locked", style = MaterialTheme.typography.titleLarge)
            Spacer(Modifier.height(16.dp))
            Button(onClick = unlock) { Text("Unlock") }
        }
    }
}

private data class Dest(val route: String, val label: String, val icon: ImageVector, val selected: ImageVector)

private object Routes {
    const val CHATS = "chats"
    const val CHAT = "chat/{thread}"
    const val ALERTS = "alerts?tab={tab}"
    const val FILES = "files"
    const val WORK = "work"
    const val SETTINGS = "settings"
    fun chat(thread: String) = "chat/${Uri.encode(thread)}"
    fun alerts(tab: Int = 0) = "alerts?tab=$tab"
}

@Composable
private fun Main(arrivals: StateFlow<Arrival?>, consumed: () -> Unit) {
    val context = LocalContext.current
    val app = TwApp.from(context)
    val nav = rememberNavController()
    val arrival by arrivals.collectAsStateWithLifecycle()
    var unread by remember { mutableIntStateOf(0) }
    var showWork by remember { mutableStateOf(workTabs(app.settings.isAdmin, app.settings.departments).isNotEmpty()) }
    var shared by remember { mutableStateOf<Pair<String, Arrival>?>(null) }

    // Ask once for permission to post notifications (Android 13+).
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    LaunchedEffect(Unit) {
        if (Build.VERSION.SDK_INT >= 33 && !com.techwiser.twai.notify.Notifier.canPost(context)) {
            permission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
    LifecycleResumeEffect(Unit) {
        val job = app.scope.launchCatching {
            val me = app.api.me()
            app.settings.isAdmin = me.is_admin
            app.settings.departments = me.departments.toSet()
            showWork = workTabs(me.is_admin, me.departments.toSet()).isNotEmpty()
            unread = app.api.unreadCount()
        }
        onPauseOrDispose { job.cancel() }
    }
    LaunchedEffect(arrival) {
        val a = arrival ?: return@LaunchedEffect
        consumed()
        val uri = a.link?.let(Uri::parse) ?: return@LaunchedEffect
        when (uri.host) {
            "chat" -> {
                val id = uri.pathSegments.firstOrNull()
                val thread = if (id == null || id == "new") newThreadId() else id
                if (a.text != null || a.file != null) shared = thread to a
                nav.navigate(Routes.chat(thread)) { launchSingleTop = true }
            }
            "alerts" -> nav.go(Routes.alerts(0))
            "reminders" -> nav.go(Routes.alerts(1))
            "files" -> nav.go(Routes.FILES)
            "work" -> nav.go(Routes.WORK)
            else -> nav.go(Routes.CHATS)
        }
    }

    val dests = buildList {
        add(Dest(Routes.CHATS, "Chats", Icons.AutoMirrored.Outlined.Chat, Icons.AutoMirrored.Filled.Chat))
        add(Dest(Routes.ALERTS, "Alerts", Icons.Outlined.Notifications, Icons.Filled.Notifications))
        add(Dest(Routes.FILES, "Files", Icons.Outlined.Folder, Icons.Filled.Folder))
        if (showWork) add(Dest(Routes.WORK, "Work", Icons.Outlined.VerifiedUser, Icons.Filled.VerifiedUser))
        add(Dest(Routes.SETTINGS, "Settings", Icons.Outlined.Settings, Icons.Filled.Settings))
    }
    val entry by nav.currentBackStackEntryAsState()
    val route = entry?.destination?.route
    val inChat = route == Routes.CHAT

    Scaffold(
        bottomBar = {
            AnimatedVisibility(!inChat) {
                NavigationBar {
                    dests.forEach { d ->
                        val selected = route == d.route
                        NavigationBarItem(
                            selected = selected,
                            onClick = { nav.go(if (d.route == Routes.ALERTS) Routes.alerts(0) else d.route) },
                            icon = {
                                if (d.route == Routes.ALERTS && unread > 0) {
                                    BadgedBox(badge = { Badge { Text(if (unread > 99) "99+" else "$unread") } }) {
                                        Icon(if (selected) d.selected else d.icon, contentDescription = d.label)
                                    }
                                } else Icon(if (selected) d.selected else d.icon, contentDescription = d.label)
                            },
                            label = { Text(d.label) },
                        )
                    }
                }
            }
        },
        contentWindowInsets = androidx.compose.foundation.layout.WindowInsets(0, 0, 0, 0),
    ) { padding ->
        NavHost(nav, startDestination = Routes.CHATS, modifier = Modifier.padding(padding).consumeWindowInsets(padding)) {
            composable(Routes.CHATS) {
                ChatListScreen(onOpen = { nav.navigate(Routes.chat(it)) }, onNew = { nav.navigate(Routes.chat(newThreadId())) })
            }
            composable(Routes.CHAT, arguments = listOf(navArgument("thread") { type = NavType.StringType })) { back ->
                val thread = back.arguments?.getString("thread").orEmpty()
                val payload = shared?.takeIf { it.first == thread }?.second
                ChatScreen(thread, onBack = { if (!nav.popBackStack()) nav.go(Routes.CHATS) },
                    draft = payload?.text, sharedFile = payload?.file)
            }
            composable(Routes.ALERTS, arguments = listOf(navArgument("tab") { type = NavType.IntType; defaultValue = 0 })) { back ->
                LaunchedEffect(Unit) { unread = 0 }
                AlertsScreen(startTab = back.arguments?.getInt("tab") ?: 0)
            }
            composable(Routes.FILES) { FilesScreen() }
            composable(Routes.WORK) { WorkScreen() }
            composable(Routes.SETTINGS) { SettingsScreen() }
        }
    }
}

/** Top-level tabs: one copy of each on the back stack, state kept. */
private fun NavHostController.go(route: String) {
    navigate(route) {
        popUpTo(graph.startDestinationId) { saveState = true }
        launchSingleTop = true
        restoreState = true
    }
}

private fun CoroutineScope.launchCatching(block: suspend () -> Unit) =
    launch(Dispatchers.Main) { runCatching { block() } }
