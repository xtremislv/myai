package com.techwiser.twai

import android.app.Application
import android.content.Context
import com.techwiser.twai.data.Api
import com.techwiser.twai.data.AuthState
import com.techwiser.twai.data.SecureStore
import com.techwiser.twai.data.Settings
import com.techwiser.twai.notify.Notifier
import com.techwiser.twai.notify.Push
import com.techwiser.twai.notify.SyncWorker
import com.techwiser.twai.update.UpdateWorker
import com.techwiser.twai.update.Updater
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/** Holds the app's single instances (no DI framework: the graph is tiny). */
class TwApp : Application() {
    lateinit var settings: Settings
        private set
    lateinit var store: SecureStore
        private set
    lateinit var api: Api
        private set

    /** For work that must outlive a screen (e.g. registering the push token). */
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        store = SecureStore(this)
        api = Api(this, settings, store)
        Notifier.createChannels(this)
        Updater.createChannel(this)
        UpdateWorker.schedule(this)  // signed in or not: an old app should still update

        scope.launch {
            api.auth.collect { state ->
                when (state) {
                    AuthState.SIGNED_IN -> {
                        SyncWorker.schedule(this@TwApp)
                        Push.register(this@TwApp)
                    }
                    AuthState.SIGNED_OUT -> {
                        SyncWorker.cancel(this@TwApp)
                        Notifier.cancelAll(this@TwApp)
                        UpdateWorker.schedule(this@TwApp)  // SyncWorker.cancel stops all work
                    }
                    AuthState.MUST_CHANGE_PASSWORD -> Unit
                }
            }
        }
    }

    companion object {
        fun from(context: Context): TwApp = context.applicationContext as TwApp
    }
}
