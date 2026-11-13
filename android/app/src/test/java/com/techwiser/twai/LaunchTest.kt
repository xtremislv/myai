package com.techwiser.twai

import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import com.techwiser.twai.ui.MainActivity
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import android.os.Looper

/** Starts the app as a phone would, signed out, and lets it settle: any crash fails the test. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class LaunchTest {
    @Test
    fun appStartsSignedOut() {
        ApplicationProvider.getApplicationContext<TwApp>()
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            repeat(20) { shadowOf(Looper.getMainLooper()).idle(); Thread.sleep(100) }
            scenario.onActivity { activity -> check(!activity.isFinishing) { "the activity finished" } }
        }
    }
}
