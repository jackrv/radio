package ua.radio345

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlin.math.log10
import kotlin.math.sqrt

/**
 * Безперервний MP3 по HTTP → кадри → MediaCodec → AudioTrack.
 *
 * Стандартні плеєри для нескінченного потоку чекають «запасу» й зупиняються, якщо дані йдуть
 * рівно в реальному часі. Тут граємо одразу те, що прийшло; якщо черга більша за
 * `maxQueuedSeconds` (після ривка мережі), нові кадри відкидаються — затримка не росте.
 * Сервер шле дані й у тиші, тож 6 с без даних означає обрив.
 */
class StreamPlayer(
    private val url: String,
    private val maxQueuedSeconds: Double,
    private val onEvent: (Event) -> Unit,
) {
    sealed interface Event {
        data object Playing : Event
        data class Failed(val message: String) : Event
    }

    /** Рівень того, що реально грає (0…1); викликається в головному потоці в мить звучання. */
    var onLevel: ((Double) -> Unit)? = null

    /** «Вдома»: з'єднання й декодер працюють, звук вимкнено. */
    @Volatile var muted = false
        set(value) {
            field = value
            track?.setVolume(if (value) 0f else 1f)
        }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var stopped = false
    @Volatile private var failed = false
    @Volatile private var lastDataAt = SystemClock.elapsedRealtime()
    @Volatile private var track: AudioTrack? = null
    @Volatile private var connection: HttpURLConnection? = null
    private var thread: Thread? = null

    private val watchdog = object : Runnable {
        override fun run() {
            if (stopped) return
            if (SystemClock.elapsedRealtime() - lastDataAt > 6000) fail("Немає даних від приймача")
            else main.postDelayed(this, 2000)
        }
    }

    fun start() {
        lastDataAt = SystemClock.elapsedRealtime()
        thread = Thread({ run() }, "radio345-stream").also { it.start() }
        main.postDelayed(watchdog, 2000)
    }

    fun stop() {
        if (stopped) return
        stopped = true
        main.removeCallbacksAndMessages(null)
        Thread { connection?.disconnect() }.start()   // disconnect може блокувати на мережі
        thread?.interrupt()
    }

    private fun fail(message: String) {
        if (stopped || failed) return
        failed = true
        main.post { if (!stopped) onEvent(Event.Failed(message)) }
    }

    private fun run() {
        var codec: MediaCodec? = null
        try {
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                connectTimeout = 15000
                readTimeout = 15000
                useCaches = false
                setRequestProperty("Cache-Control", "no-cache")
                setRequestProperty("User-Agent", "Radio345-Android")
            }
            connection = conn
            if (stopped) return
            val code = conn.responseCode
            if (code != 200) {
                return fail(if (code == 403) "Доступ лише з вашого облікового запису Tailscale" else "Приймач відповів $code")
            }
            val input = conn.inputStream
            val frames = Mp3Frames()
            val chunk = ByteArray(8192)
            val info = MediaCodec.BufferInfo()
            var pts = 0L
            var sampleRate = 0
            var outRate = 0
            var outChannels = 1
            var written = 0L          // кадрів PCM, відданих у AudioTrack
            var announced = false

            fun queued(): Double {
                val t = track ?: return 0.0
                if (outRate == 0) return 0.0
                val played = t.playbackHeadPosition.toLong() and 0xFFFFFFFFL
                return (written - played).coerceAtLeast(0) / outRate.toDouble()
            }

            fun drain(c: MediaCodec) {
                while (true) {
                    val index = c.dequeueOutputBuffer(info, 0)
                    if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                        val format = c.outputFormat
                        outRate = format.getInteger(MediaFormat.KEY_SAMPLE_RATE)
                        outChannels = format.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
                        track?.release()
                        track = makeTrack(outRate, outChannels)
                        written = 0
                        continue
                    }
                    if (index < 0) return
                    val buffer = c.getOutputBuffer(index)
                    if (buffer != null && info.size > 0) {
                        if (track == null) {
                            outRate = sampleRate
                            track = makeTrack(outRate, outChannels)
                        }
                        val bytes = ByteArray(info.size)
                        buffer.position(info.offset)
                        buffer.get(bytes)
                        val delayMs = (queued() * 1000).toLong()
                        track?.write(bytes, 0, bytes.size)
                        written += bytes.size / (2 * outChannels)
                        postLevel(bytes, delayMs)
                        if (!announced) {
                            announced = true
                            main.post { if (!stopped) onEvent(Event.Playing) }
                        }
                    }
                    c.releaseOutputBuffer(index, false)
                }
            }

            while (!stopped) {
                val n = input.read(chunk)
                if (n < 0) return fail("Приймач закрив з'єднання")
                lastDataAt = SystemClock.elapsedRealtime()
                for ((header, frame) in frames.push(chunk, n)) {
                    if (codec == null) {
                        sampleRate = header.sampleRate
                        outChannels = header.channels
                        codec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_MPEG).apply {
                            configure(MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_MPEG,
                                header.sampleRate, header.channels), null, null, 0)
                            start()
                        }
                    }
                    val c = codec!!
                    // Наздоганяємо ефір замість росту затримки.
                    if (queued() > maxQueuedSeconds) { drain(c); continue }
                    var index = c.dequeueInputBuffer(10_000)
                    while (index < 0 && !stopped) {
                        drain(c)
                        index = c.dequeueInputBuffer(10_000)
                    }
                    if (stopped) break
                    c.getInputBuffer(index)!!.apply { clear(); put(frame) }
                    c.queueInputBuffer(index, 0, frame.size, pts, 0)
                    pts += header.samplesPerFrame * 1_000_000L / header.sampleRate
                    drain(c)
                }
            }
        } catch (e: IOException) {
            fail("Зв'язок перервано: ${e.localizedMessage ?: e.javaClass.simpleName}")
        } catch (e: Exception) {
            fail("Аудіо недоступне: ${e.localizedMessage ?: e.javaClass.simpleName}")
        } finally {
            try { codec?.stop() } catch (_: Exception) {}
            try { codec?.release() } catch (_: Exception) {}
            val t = track
            track = null
            try { t?.pause(); t?.flush(); t?.release() } catch (_: Exception) {}
            try { connection?.disconnect() } catch (_: Exception) {}
        }
    }

    private fun makeTrack(rate: Int, channels: Int): AudioTrack {
        val mask = if (channels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val min = AudioTrack.getMinBufferSize(rate, mask, AudioFormat.ENCODING_PCM_16BIT)
        // Буфер вміщує всю дозволену чергу: запис не блокує читання мережі.
        val size = maxOf(min * 2, (rate * channels * 2 * (maxQueuedSeconds + 1)).toInt())
        return AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build())
            .setAudioFormat(AudioFormat.Builder()
                .setSampleRate(rate)
                .setChannelMask(mask)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .build())
            .setBufferSizeInBytes(size)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
            .also {
                it.setVolume(if (muted) 0f else 1f)
                it.play()
            }
    }

    private var levelSum = 0.0
    private var levelCount = 0
    private var levelAt = 0L

    /** Рівень −50…−10 дБ → 0…1, ~15 разів на секунду, з затримкою черги — у мить звучання. */
    private fun postLevel(pcm: ByteArray, delayMs: Long) {
        var i = 0
        while (i + 1 < pcm.size) {
            val v = ((pcm[i + 1].toInt() shl 8) or (pcm[i].toInt() and 0xFF)).toShort() / 32768.0
            levelSum += v * v
            levelCount++
            i += 2
        }
        val now = SystemClock.elapsedRealtime()
        if (now - levelAt < 66 || levelCount == 0) return
        levelAt = now
        val rms = sqrt(levelSum / levelCount)
        levelSum = 0.0
        levelCount = 0
        val db = 20 * log10(maxOf(rms, 1e-6))
        val level = ((db + 50) / 40).coerceIn(0.0, 1.0)
        main.postDelayed({ if (!stopped) onLevel?.invoke(level) }, delayMs)
    }
}
