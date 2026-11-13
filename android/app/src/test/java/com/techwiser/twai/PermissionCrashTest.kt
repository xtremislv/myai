package com.techwiser.twai

import androidx.fragment.app.FragmentActivity
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner

/**
 * 1.0.70 crashed right after sign-in on Android 13+ ("Can only use lower 16 bits for requestCode"):
 * biometric 1.1.0 pulled in fragment 1.2.5, whose FragmentActivity validates permission request codes
 * that activity 1.9 makes larger than 16 bits. A current fragment (libs.versions.toml) has no such check.
 * Robolectric doesn't run the real permission flow, so this calls the check that threw.
 */
@RunWith(RobolectricTestRunner::class)
class PermissionCrashTest {
    @Test
    fun fragmentAcceptsActivityResultRequestCodes() {
        val activity = Robolectric.buildActivity(FragmentActivity::class.java).create().get()
        // activity 1.9 hands out codes like this one; fragment 1.2.5 threw on anything above 0xFFFF.
        activity.validateRequestPermissionsRequestCode(0x2_0000 + 7)
    }
}
