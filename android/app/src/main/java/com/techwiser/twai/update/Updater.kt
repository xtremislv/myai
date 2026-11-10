package com.techwiser.twai.update

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.IntentCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ProcessLifecycleOwner
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.techwiser.twai.BuildConfig
import com.techwiser.twai.R
import com.techwiser.twai.TwApp
import com.techwiser.twai.data.AndroidRelease
import com.techwiser.twai.notify.Notifier
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/** Where the self-update is at, for the in-app prompt. */
sealed interface UpdateState {
    data object Idle : UpdateState
    data class Available(val release: AndroidRelease, val required: Boolean) : UpdateState
    data class Downloading(val release: AndroidRelease, val progress: Float) : UpdateState
    data class Installing(val release: AndroidRelease) : UpdateState
    data class Failed(val release: AndroidRelease, val message: String) : UpdateState
}

/**
 * Keeps the app in step with the server: the server publishes an APK (scripts/publish_android.sh),
 * and /v1/app/config names its version. A newer version is downloaded, its SHA-256 checked against
 * what the server said, and installed with PackageInstaller. Android asks the person to confirm,
 * except on Android 12+ once this app installed itself (then updates in the background are silent).
 * Updates must be signed with the same key as the installed app, or Android refuses them.
 */
object Updater {
    private const val TAG = "TwUpdate"
    const val CHANNEL = "app_updates"
    private const val NOTE_ID = 0x7570
    private const val ACTION_STATUS = "com.techwiser.twai.UPDATE_STATUS"
    private const val PREFS = "tw_updates"
    private const val K_NOTIFIED = "notified_version"
    private const val K_UPDATED_FROM = "updated_from"

    private val _state = MutableStateFlow<UpdateState>(UpdateState.Idle)
    val state: StateFlow<UpdateState> = _state.asStateFlow()

    /** The person tapped "Later" on this version (asked again on the next start). */
    @Volatile var dismissedVersion = 0
        private set

    private val busy = Mutex()

    fun createChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(CHANNEL, context.getString(R.string.channel_app_updates), NotificationManager.IMPORTANCE_DEFAULT)
        channel.description = context.getString(R.string.channel_app_updates_desc)
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    /** Asks the server for its app. Returns the newer release, if there is one. Never throws. */
    suspend fun check(context: Context): AndroidRelease? {
        val release = try {
            TwApp.from(context).api.appConfig().android
        } catch (e: Exception) {
            Log.w(TAG, "update check failed: ${e.message}")
            return null
        }
        if (release == null || release.version_code <= BuildConfig.VERSION_CODE) {
            if (_state.value is UpdateState.Available) _state.value = UpdateState.Idle
            return null
        }
        val current = _state.value
        if (current is UpdateState.Idle || current is UpdateState.Available || current is UpdateState.Failed) {
            _state.value = UpdateState.Available(release, required = BuildConfig.VERSION_CODE < release.min_version_code)
        }
        return release
    }

    fun later(release: AndroidRelease) {
        dismissedVersion = release.version_code
    }

    /** Shows the prompt again (from a notification or Settings). */
    fun prompt() {
        dismissedVersion = 0
    }

    /** Downloads, checks and installs [release]. [interactive]: the person is looking at the app. */
    suspend fun install(context: Context, release: AndroidRelease, interactive: Boolean) {
        if (!busy.tryLock()) return
        try {
            val apk = download(context, release)
            _state.value = UpdateState.Installing(release)
            commit(context, apk, release, interactive)
        } catch (e: Exception) {
            Log.w(TAG, "update failed: ${e.message}")
            _state.value = UpdateState.Failed(release, e.message ?: "The update couldn't be downloaded.")
            if (!interactive) notifyReady(context, release)
        } finally {
            busy.unlock()
        }
    }

    private suspend fun download(context: Context, release: AndroidRelease): File = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        dir.listFiles()?.filter { it.name != "${release.version_code}.apk" }?.forEach { it.delete() }
        val apk = File(dir, "${release.version_code}.apk")
        if (apk.exists() && sha256(apk) == release.sha256.lowercase()) return@withContext apk
        _state.value = UpdateState.Downloading(release, 0f)
        val part = File(dir, "${release.version_code}.part")
        TwApp.from(context).api.downloadUpdate(release.url, part) { done ->
            _state.value = UpdateState.Downloading(release, if (release.size > 0) done.toFloat() / release.size else 0f)
        }
        if (sha256(part) != release.sha256.lowercase()) {
            part.delete()
            throw IllegalStateException("The downloaded update didn't match its checksum. Try again.")
        }
        if (!part.renameTo(apk)) throw IllegalStateException("Couldn't save the update.")
        apk
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buffer)
                if (n < 0) break
                digest.update(buffer, 0, n)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private suspend fun commit(context: Context, apk: File, release: AndroidRelease, interactive: Boolean) = withContext(Dispatchers.IO) {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
            setSize(apk.length())
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
            }
        }
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            apk.inputStream().use { input ->
                session.openWrite("twai.apk", 0, apk.length()).use { out ->
                    input.copyTo(out)
                    session.fsync(out)
                }
            }
            val intent = Intent(context, UpdateReceiver::class.java).setAction(ACTION_STATUS)
                .putExtra(EXTRA_VERSION, release.version_code)
                .putExtra(EXTRA_INTERACTIVE, interactive)
            // Mutable: the installer adds the status and, if needed, the confirm screen to it.
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0)
            session.commit(PendingIntent.getBroadcast(context, id, intent, flags).intentSender)
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putInt(K_UPDATED_FROM, BuildConfig.VERSION_CODE).apply()
    }

    internal fun onStatus(context: Context, intent: Intent) {
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        val version = intent.getIntExtra(EXTRA_VERSION, 0)
        val release = (_state.value as? UpdateState.Installing)?.release
        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm = IntentCompat.getParcelableExtra(intent, Intent.EXTRA_INTENT, Intent::class.java)
                if (confirm != null && inForeground()) {
                    context.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    release?.let { _state.value = UpdateState.Available(it, BuildConfig.VERSION_CODE < it.min_version_code) }
                } else {
                    // Android won't let a background app open the confirm screen: ask with a notification.
                    release?.let { notifyReady(context, it) } ?: notifyReady(context, version, null)
                    release?.let { _state.value = UpdateState.Available(it, BuildConfig.VERSION_CODE < it.min_version_code) }
                }
            }
            PackageInstaller.STATUS_SUCCESS -> Unit  // Android restarts the app as the new version.
            else -> {
                val message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
                Log.w(TAG, "install failed: $status $message")
                val text = when (status) {
                    PackageInstaller.STATUS_FAILURE_ABORTED -> "The update was cancelled."
                    PackageInstaller.STATUS_FAILURE_CONFLICT, PackageInstaller.STATUS_FAILURE_INCOMPATIBLE ->
                        "This update is signed differently from the installed app. Uninstall TW-ai and install it again from the website."
                    PackageInstaller.STATUS_FAILURE_STORAGE -> "Not enough space on the phone for the update."
                    else -> "The update couldn't be installed${message?.let { ": $it" } ?: "."}"
                }
                release?.let { _state.value = UpdateState.Failed(it, text) }
            }
        }
    }

    private fun inForeground(): Boolean =
        ProcessLifecycleOwner.get().lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)

    fun appInForeground(): Boolean = inForeground()

    private fun notifyReady(context: Context, release: AndroidRelease) =
        notifyReady(context, release.version_code, release.version_name)

    /** "Update available: tap to install", once per version. */
    private fun notifyReady(context: Context, version: Int, name: String?) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (prefs.getInt(K_NOTIFIED, 0) == version) return
        val notification = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_notify)
            .setColor(0xFF2563EB.toInt())
            .setAutoCancel(true)
            .setContentTitle("TW-ai update ready")
            .setContentText(if (name != null) "Version $name is ready. Tap to install it." else "Tap to install the new version.")
            .setContentIntent(Notifier.open(context, "twai://update"))
            .build()
        if (Notifier.canPost(context)) {
            try {
                NotificationManagerCompat.from(context).notify(NOTE_ID, notification)
                prefs.edit().putInt(K_NOTIFIED, version).apply()
            } catch (e: SecurityException) {
                // Notifications turned off between the check and the post.
            }
        }
    }

    /** After Android replaced the app: say so once, and clear the downloaded file. */
    internal fun onReplaced(context: Context) {
        File(context.cacheDir, "updates").deleteRecursively()
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val from = prefs.getInt(K_UPDATED_FROM, 0)
        prefs.edit().remove(K_UPDATED_FROM).apply()
        NotificationManagerCompat.from(context).cancel(NOTE_ID)
        if (from == 0 || from == BuildConfig.VERSION_CODE || !Notifier.canPost(context)) return
        val notification = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_notify)
            .setColor(0xFF2563EB.toInt())
            .setAutoCancel(true)
            .setContentTitle("TW-ai updated")
            .setContentText("You're on version ${BuildConfig.VERSION_NAME}.")
            .setContentIntent(Notifier.open(context, "twai://chat"))
            .build()
        try {
            NotificationManagerCompat.from(context).notify(NOTE_ID, notification)
        } catch (e: SecurityException) {
            // Notifications are off.
        }
    }

    internal const val EXTRA_VERSION = "version"
    internal const val EXTRA_INTERACTIVE = "interactive"
}

/** The installer's answer for an update, and "this app was just replaced by a newer version". */
class UpdateReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_MY_PACKAGE_REPLACED -> Updater.onReplaced(context)
            else -> Updater.onStatus(context, intent)
        }
    }
}

/**
 * Checks for a new app every few hours, signed in or not. A newer version is downloaded in the
 * background; while the app isn't open it is installed straight away where Android allows that
 * silently, otherwise the person gets a notification to install it.
 */
class UpdateWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val release = Updater.check(applicationContext) ?: return Result.success()
        if (Updater.appInForeground()) return Result.success()  // the in-app prompt asks instead
        Updater.install(applicationContext, release, interactive = false)
        return Result.success()
    }

    companion object {
        private const val NAME = "tw-app-update"

        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<UpdateWorker>(6, TimeUnit.HOURS)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(NAME, ExistingPeriodicWorkPolicy.KEEP, request)
        }
    }
}
