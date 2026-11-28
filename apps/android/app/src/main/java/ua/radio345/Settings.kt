package ua.radio345

import android.content.SharedPreferences
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/**
 * Адреси Mac і Pi у Tailscale. Токена немає: сервер пускає лише пристрої вашого облікового запису Tailscale.
 * Ключі ті самі, що в iOS-застосунку.
 */
class ConnectionSettings(private val prefs: SharedPreferences) {
    private fun put(key: String, value: String) = prefs.edit().putString(key, value).apply()
    private fun put(key: String, value: Boolean) = prefs.edit().putBoolean(key, value).apply()

    private var _receiver by mutableStateOf(Receiver.from(prefs.getString("radio345.receiver", null)))
    var receiver: Receiver
        get() = _receiver
        set(v) { _receiver = v; put("radio345.receiver", v.raw) }

    private var _macHost by mutableStateOf(prefs.getString("radio345.host", "") ?: "")
    var macHost: String
        get() = _macHost
        set(v) { _macHost = v; put("radio345.host", v) }

    private var _piHost by mutableStateOf(prefs.getString("radio345.piHost", "") ?: "")
    var piHost: String
        get() = _piHost
        set(v) { _piHost = v; put("radio345.piHost", v) }

    /** Тексти й «Відібрані» — з цього вузла; ефір і передача — завжди з `receiver`. */
    private var _textSource by mutableStateOf(TextSource.from(prefs.getString("radio345.textSource", null)))
    var textSource: TextSource
        get() = _textSource
        set(v) { _textSource = v; put("radio345.textSource", v.raw) }

    private var _channel by mutableStateOf(StreamChannel.from(prefs.getString("radio345.channel", null)))
    var channel: StreamChannel
        get() = _channel
        set(v) { _channel = v; put("radio345.channel", v.raw) }

    /** Канал «наживо» без звуків сповіщень (+ затримка початку передачі ~2–3 с). */
    private var _filterSignals by mutableStateOf(prefs.getBoolean("radio345.filterSignals", false))
    var filterSignals: Boolean
        get() = _filterSignals
        set(v) { _filterSignals = v; put("radio345.filterSignals", v) }

    /** Вмикати «Вдома», коли телефон повертається в мережу приймача. */
    private var _autoHome by mutableStateOf(prefs.getBoolean("radio345.autoHome", true))
    var autoHome: Boolean
        get() = _autoHome
        set(v) { _autoHome = v; put("radio345.autoHome", v) }

    /** Вузол текстів. */
    val textReceiver: Receiver get() = textSource.resolve(receiver)
    /** Чи це два різні вузли (інакше досить одного опитування). */
    val hasSeparateTexts: Boolean get() = textReceiver != receiver
    /** Звідки звук обраного каналу: «Наживо» — з ефіру, «Відібрані» — з вузла текстів. */
    val streamReceiver: Receiver get() = if (channel == StreamChannel.LIVE) receiver else textReceiver

    fun host(source: Receiver): String = if (source == Receiver.MAC) macHost else piHost

    val isConfigured: Boolean get() = baseUrl(host(receiver)) != null

    fun url(path: String, from: Receiver, query: List<Pair<String, String>> = emptyList()): String? =
        baseUrl(host(from))?.let { buildUrl(it, path, query) }
}
