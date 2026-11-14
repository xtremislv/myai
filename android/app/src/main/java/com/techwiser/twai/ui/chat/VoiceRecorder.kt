package com.techwiser.twai.ui.chat

import android.content.Context
import android.media.MediaRecorder
import android.os.Build
import java.io.File

/**
 * Records the chat box's voice input as AAC (16 kHz mono) into a private cache file, which the server's
 * Whisper turns into text (POST /v1/voice/transcribe). The file is deleted as soon as it has been read.
 * Same speech model as the website, so Hinglish and product names come out the same on both.
 */
class VoiceRecorder(private val context: Context) {
    private var recorder: MediaRecorder? = null
    private var file: File? = null

    val recording: Boolean get() = recorder != null

    /** Starts recording; [onLimit] runs if it reaches [MAX_MS] (then call [stop]). Throws if the mic can't be used. */
    fun start(onLimit: () -> Unit) {
        cancel()
        val out = File.createTempFile("voice-", ".m4a", context.cacheDir)
        val r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(context) else @Suppress("DEPRECATION") MediaRecorder()
        try {
            r.setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            r.setAudioSamplingRate(16000)
            r.setAudioChannels(1)
            r.setAudioEncodingBitRate(48000)
            r.setMaxDuration(MAX_MS)
            r.setOutputFile(out.absolutePath)
            r.setOnInfoListener { _, what, _ -> if (what == MediaRecorder.MEDIA_RECORDER_INFO_MAX_DURATION_REACHED) onLimit() }
            r.prepare()
            r.start()
        } catch (e: Exception) {
            r.release()
            out.delete()
            throw e
        }
        recorder = r
        file = out
    }

    /** Stops and returns the recording, or null if it was too short to hold anything. */
    fun stop(): ByteArray? {
        val r = recorder ?: return null
        val f = file
        recorder = null
        file = null
        val ok = runCatching { r.stop() }.isSuccess // stop() throws when almost nothing was recorded
        r.release()
        return try {
            if (ok && f != null && f.length() > 0) f.readBytes() else null
        } finally {
            f?.delete()
        }
    }

    fun cancel() {
        recorder?.let { runCatching { it.stop() }; it.release() }
        recorder = null
        file?.delete()
        file = null
    }

    companion object {
        const val MAX_MS = 180_000
    }
}
