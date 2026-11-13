package com.techwiser.twai

import android.os.Looper
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import com.techwiser.twai.data.AuthState
import com.techwiser.twai.ui.MainActivity
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import kotlin.concurrent.thread
import java.security.Key
import java.security.KeyStoreSpi
import java.security.Provider
import java.security.SecureRandom
import java.security.cert.Certificate
import java.security.spec.AlgorithmParameterSpec
import java.util.Collections
import java.util.Date
import java.util.Enumeration
import javax.crypto.KeyGeneratorSpi
import javax.crypto.SecretKey
import javax.crypto.spec.SecretKeySpec

/**
 * Signs in against a fake server and opens the main screen, the path a phone takes right after
 * "Sign in" (Android 13 and 14 framework code via Robolectric). Any crash fails the test.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33, 34])
class SignedInTest {
    private lateinit var server: ServerSocket
    private val seen = Collections.synchronizedList(mutableListOf<String>())
    private val address get() = "http://127.0.0.1:${server.localPort}"

    @Before
    fun setUp() {
        FakeKeyStore.install()
        server = ServerSocket(0, 50, InetAddress.getLoopbackAddress())
        thread(isDaemon = true) {
            while (!server.isClosed) {
                val sock = runCatching { server.accept() }.getOrNull() ?: break
                thread(isDaemon = true) { sock.use { answer(it) } }
            }
        }
    }

    @After
    fun tearDown() = server.close()

    private fun answer(sock: Socket) {
        val input = sock.getInputStream().bufferedReader()
        val request = input.readLine() ?: return
        var length = 0
        while (true) {
            val line = input.readLine() ?: break
            if (line.isEmpty()) break
            if (line.lowercase().startsWith("content-length:")) length = line.substringAfter(':').trim().toInt()
        }
        repeat(length) { input.read() }
        val parts = request.split(" ")
        val path = parts[1].substringBefore('?')
        seen += "${parts[0]} $path"
        val body = when {
            path.startsWith("/v1/auth/device/") ->
                """{"access_token":"a","expires_in":3600,"refresh_token":"r","device_id":"d1"}"""
            path == "/v1/auth/me" -> """{"id":"u1","username":"tester","is_admin":true,"departments":["hr"],"chat_enabled":true}"""
            path == "/v1/notifications/count" -> """{"unread":2}"""
            path == "/v1/app/config" -> """{"api_version":1}"""
            path == "/v1/chat/threads" -> """[{"thread_id":"chat-1","title":"Hello","updated_at":"2026-11-13T05:00:00Z"}]"""
            path == "/v1/files" -> """{"files":[],"used":0,"quota":0}"""
            path == "/v1/me/prefs" -> "{}"
            else -> "[]"
        }.toByteArray()
        val out = sock.getOutputStream()
        out.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
        out.write(body)
        out.flush()
    }

    private fun settle(times: Int = 30) =
        repeat(times) { shadowOf(Looper.getMainLooper()).idle(); Thread.sleep(100) }

    @Test
    fun signingInOpensTheMainScreen() {
        val app = ApplicationProvider.getApplicationContext<TwApp>()
        app.settings.server = address
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            settle(10)
            runBlocking { app.api.login("tester", "password", null) }  // what the Sign in button does
            check(app.api.auth.value == AuthState.SIGNED_IN)
            settle()
            scenario.onActivity { check(!it.isFinishing) { "the activity finished" } }
        }
        check(seen.any { it == "GET /v1/auth/me" }) { "the main screen never loaded; requests: $seen" }
    }

    @Test
    fun aSignedInPhoneOpensTheApp() {
        val app = ApplicationProvider.getApplicationContext<TwApp>()
        app.settings.server = address
        runBlocking { app.api.login("tester", "password", null) }
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            settle()
            scenario.onActivity { check(!it.isFinishing) { "the activity finished" } }
        }
        check(seen.any { it == "GET /v1/auth/me" }) { "the main screen never loaded; requests: $seen" }
    }

    @Test
    @Config(sdk = [35], qualifiers = "night")
    fun darkModeOnAndroid15() = aSignedInPhoneOpensTheApp()

    @Test
    @Config(sdk = [34], qualifiers = "night-xxhdpi")
    fun darkModeSignIn() = signingInOpensTheMainScreen()
}

/** Robolectric has no Android Keystore: an in-memory one with the same name, for SecureStore. */
private object FakeKeyStore {
    private val keys = mutableMapOf<String, Key>()

    fun install() {
        keys.clear()
        if (java.security.Security.getProvider("AndroidKeyStore") != null) return
        java.security.Security.addProvider(object : Provider("AndroidKeyStore", 1.0, "test keystore") {
            init {
                put("KeyStore.AndroidKeyStore", Store::class.java.name)
                put("KeyGenerator.AES", Gen::class.java.name)
            }
        })
    }

    class Gen : KeyGeneratorSpi() {
        private var alias = "k"
        override fun engineInit(random: SecureRandom?) = Unit
        override fun engineInit(params: AlgorithmParameterSpec?, random: SecureRandom?) {
            alias = params?.javaClass?.getMethod("getKeystoreAlias")?.invoke(params) as? String ?: "k"
        }
        override fun engineInit(keysize: Int, random: SecureRandom?) = Unit
        override fun engineGenerateKey(): SecretKey =
            SecretKeySpec(ByteArray(32).also { SecureRandom().nextBytes(it) }, "AES").also { keys[alias] = it }
    }

    class Store : KeyStoreSpi() {
        override fun engineGetKey(alias: String, password: CharArray?): Key? = keys[alias]
        override fun engineGetEntry(alias: String, protection: java.security.KeyStore.ProtectionParameter?): java.security.KeyStore.Entry? =
            (keys[alias] as? SecretKey)?.let { java.security.KeyStore.SecretKeyEntry(it) }
        override fun engineGetCertificateChain(alias: String?): Array<Certificate>? = null
        override fun engineGetCertificate(alias: String?): Certificate? = null
        override fun engineGetCreationDate(alias: String?): Date = Date()
        override fun engineSetKeyEntry(alias: String, key: Key, password: CharArray?, chain: Array<out Certificate>?) { keys[alias] = key }
        override fun engineSetKeyEntry(alias: String?, key: ByteArray?, chain: Array<out Certificate>?) = Unit
        override fun engineSetCertificateEntry(alias: String?, cert: Certificate?) = Unit
        override fun engineDeleteEntry(alias: String) { keys.remove(alias) }
        override fun engineAliases(): Enumeration<String> = Collections.enumeration(keys.keys.toList())
        override fun engineContainsAlias(alias: String) = keys.containsKey(alias)
        override fun engineSize() = keys.size
        override fun engineIsKeyEntry(alias: String) = keys.containsKey(alias)
        override fun engineIsCertificateEntry(alias: String?) = false
        override fun engineGetCertificateAlias(cert: Certificate?): String? = null
        override fun engineStore(stream: OutputStream?, password: CharArray?) = Unit
        override fun engineLoad(stream: InputStream?, password: CharArray?) = Unit
    }
}
