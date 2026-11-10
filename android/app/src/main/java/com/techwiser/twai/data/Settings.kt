package com.techwiser.twai.data

import android.content.Context
import com.techwiser.twai.BuildConfig
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

enum class ThemeMode { SYSTEM, LIGHT, DARK }

/** Plain app settings (nothing secret: the sign-in lives in [SecureStore]). */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("tw_settings", Context.MODE_PRIVATE)

    private val _theme = MutableStateFlow(ThemeMode.entries.getOrElse(prefs.getInt(K_THEME, 0)) { ThemeMode.SYSTEM })
    val theme: StateFlow<ThemeMode> = _theme.asStateFlow()

    private val _appLock = MutableStateFlow(prefs.getBoolean(K_LOCK, false))
    val appLock: StateFlow<Boolean> = _appLock.asStateFlow()

    var server: String
        get() = prefs.getString(K_SERVER, null)?.takeIf { it.isNotBlank() } ?: BuildConfig.DEFAULT_SERVER
        set(value) {
            prefs.edit().putString(K_SERVER, normalizeServer(value)).apply()
        }

    var username: String
        get() = prefs.getString(K_USER, "") ?: ""
        set(value) = prefs.edit().putString(K_USER, value).apply()

    /** From /v1/auth/me, cached so background checks know whether to look for approvals. */
    var isAdmin: Boolean
        get() = prefs.getBoolean(K_ADMIN, false)
        set(value) = prefs.edit().putBoolean(K_ADMIN, value).apply()

    var departments: Set<String>
        get() = prefs.getStringSet(K_DEPTS, emptySet()) ?: emptySet()
        set(value) = prefs.edit().putStringSet(K_DEPTS, value).apply()

    /** Notification ids already shown as phone notifications, so each appears once. */
    var shownNotifications: Set<String>
        get() = prefs.getStringSet(K_SHOWN, emptySet()) ?: emptySet()
        set(value) = prefs.edit().putStringSet(K_SHOWN, value.toList().takeLast(300).toSet()).apply()

    /** Approval ids already announced to this approver. */
    var shownApprovals: Set<String>
        get() = prefs.getStringSet(K_APPROVALS, emptySet()) ?: emptySet()
        set(value) = prefs.edit().putStringSet(K_APPROVALS, value.toList().takeLast(300).toSet()).apply()

    /** The chat on screen right now (no "reply ready" notification for it). Not persisted. */
    @Volatile
    var openThread: String? = null

    fun setTheme(mode: ThemeMode) {
        prefs.edit().putInt(K_THEME, mode.ordinal).apply()
        _theme.value = mode
    }

    fun setAppLock(on: Boolean) {
        prefs.edit().putBoolean(K_LOCK, on).apply()
        _appLock.value = on
    }

    fun clearAccount() {
        prefs.edit().remove(K_SHOWN).remove(K_APPROVALS).remove(K_ADMIN).remove(K_DEPTS).apply()
    }

    companion object {
        private const val K_SERVER = "server"
        private const val K_USER = "username"
        private const val K_THEME = "theme"
        private const val K_LOCK = "app_lock"
        private const val K_SHOWN = "shown_notifications"
        private const val K_APPROVALS = "shown_approvals"
        private const val K_ADMIN = "is_admin"
        private const val K_DEPTS = "departments"

        fun normalizeServer(raw: String): String {
            var url = raw.trim().trimEnd('/')
            if (url.isEmpty()) return BuildConfig.DEFAULT_SERVER
            if (!url.startsWith("http://") && !url.startsWith("https://")) url = "https://$url"
            return url.removeSuffix("/ui")
        }
    }
}
