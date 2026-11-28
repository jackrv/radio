package ua.radio345

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import java.util.concurrent.TimeUnit
import kotlin.math.log10
import kotlin.math.sqrt

/**
 * Передача з телефона (PTT): мікрофон → PCM16 моно 16 кГц → WebSocket `/transmit` на вузлі.
 * На час передачі вузол не слухає ефір (напівдуплекс), а тут вимикається прийом, щоб не чути себе.
 * Дозвіл на мікрофон запитує інтерфейс; без нього `begin` лише повідомляє про помилку.
 */
class Transmitter(private val client: OkHttpClient) {
    /** Вузол дозволяє передачу (`transmit` у /events). */
    var serverEnabled by mutableStateOf(false)
    var isTransmitting by mutableStateOf(false)
        private set
    var level by mutableStateOf(0.0)
        private set
    /** Останні рівні голосу для кругових смуг (старі → нові). */
    var levels by mutableStateOf(List(48) { 0.0 })
        private set
    var lastError by mutableStateOf<String?>(null)

    val isAvailable: Boolean get() = serverEnabled
    val unavailableReason = "Передачу на приймачі вимкнено"

    private val main = Handler(Looper.getMainLooper())
    private var socket: WebSocket? = null
    private var recorder: AudioRecord? = null
    private var thread: Thread? = null
    @Volatile private var running = false

    @SuppressLint("MissingPermission")  // дозвіл перевіряє інтерфейс перед викликом
    fun begin(url: String?) {
        if (isTransmitting) return
        lastError = null
        if (url == null) { lastError = "Некоректна адреса приймача"; return }
        val wsUrl = url.replaceFirst(Regex("^http"), "ws")
        val rate = 16000
        val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val record = try {
            AudioRecord(MediaRecorder.AudioSource.MIC, rate, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(min * 2, 8192))
        } catch (e: Exception) { null }
        if (record == null || record.state != AudioRecord.STATE_INITIALIZED) {
            record?.release()
            lastError = "Мікрофон недоступний"
            return
        }
        isTransmitting = true  // інтерфейс реагує одразу
        // Повідомлення до завершення рукостискання OkHttp ставить у чергу.
        val ws = client.newWebSocket(Request.Builder().url(wsUrl).build(), object : WebSocketListener() {
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                main.post {
                    if (socket === webSocket) failed(
                        if (response?.code == 409) "Зараз передає інший пристрій" else "Зв'язок із приймачем перервано")
                }
            }
        })
        socket = ws
        recorder = record
        running = true
        try {
            record.startRecording()
        } catch (e: Exception) {
            lastError = "Передача не почалася: ${e.localizedMessage}"
            end()
            return
        }
        thread = Thread({ pump(record, ws) }, "radio345-ptt").also { it.start() }
    }

    /** 32 мс на повідомлення: як 512 відліків на 16 кГц. */
    private fun pump(record: AudioRecord, ws: WebSocket) {
        val samples = ShortArray(512)
        val bytes = ByteArray(samples.size * 2)
        while (running) {
            val n = record.read(samples, 0, samples.size)
            if (n <= 0) continue
            var sum = 0.0
            for (i in 0 until n) {
                val s = samples[i].toInt()
                bytes[2 * i] = (s and 0xFF).toByte()
                bytes[2 * i + 1] = ((s shr 8) and 0xFF).toByte()
                val v = s / 32768.0
                sum += v * v
            }
            if (!running) break
            ws.send(bytes.toByteString(0, n * 2))
            val db = 20 * log10(maxOf(sqrt(sum / n), 1e-6))
            val lvl = ((db + 50) / 40).coerceIn(0.0, 1.0)
            main.post { push(lvl) }
        }
    }

    fun end() {
        running = false
        val record = recorder
        recorder = null
        thread?.join(300)
        thread = null
        try { record?.stop() } catch (_: Exception) {}
        record?.release()
        socket?.close(1000, null)
        socket = null
        isTransmitting = false
        level = 0.0
        levels = List(levels.size) { 0.0 }
    }

    private fun push(value: Double) {
        if (!isTransmitting) return
        level = value
        levels = levels.drop(1) + value
    }

    private fun failed(message: String) {
        if (!isTransmitting) return
        lastError = message
        end()
    }

    companion object {
        fun client(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(6, TimeUnit.SECONDS)
            .readTimeout(6, TimeUnit.SECONDS)
            .build()
    }
}
