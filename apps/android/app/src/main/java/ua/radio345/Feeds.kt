package ua.radio345

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

// Лише Kotlin і org.json: перевіряється JVM-тестами без пристрою (FeedsTest).

/** Куди підключатися: приймач на Mac чи радіовузол на Raspberry Pi. */
enum class Receiver(val raw: String, val title: String) {
    MAC("mac", "Mac"), PI("pi", "Pi");

    companion object {
        fun from(raw: String?) = entries.firstOrNull { it.raw == raw } ?: MAC
    }
}

/** Звідки тексти: звідти ж, звідки ефір (типово), або з іншого вузла. */
enum class TextSource(val raw: String, val title: String) {
    SAME("same", "Як ефір"), MAC("mac", "Mac"), PI("pi", "Pi");

    fun resolve(air: Receiver): Receiver = when (this) {
        SAME -> air
        MAC -> Receiver.MAC
        PI -> Receiver.PI
    }

    companion object {
        fun from(raw: String?) = entries.firstOrNull { it.raw == raw } ?: SAME
    }
}

enum class StreamChannel(val raw: String, val title: String, val hint: String) {
    SELECTED("selected", "Відібрані", "Фрази за режимом Telegram, після розпізнавання"),
    LIVE("live", "Наживо", "Усе мовлення одразу, без відбору за текстом");

    fun path(filterSignals: Boolean): String = when (this) {
        SELECTED -> "/stream.mp3"
        LIVE -> if (filterSignals) "/live-clean.mp3" else "/live.mp3"
    }

    companion object {
        fun from(raw: String?) = entries.firstOrNull { it.raw == raw } ?: SELECTED
    }
}

data class Clip(
    /** Номер на сервері; після його перезапуску нумерація починається знову. */
    val number: Int,
    /** Сеанс сервера ("" — старий сервер без сеансу). */
    val session: String,
    val at: Double,
    val caption: String,
    val durationS: Double,
) {
    val id: String get() = "$session#$number"
    /** Останній абзац підпису — сам текст фрази (шапка «Подія / Кому» вище). */
    val text: String get() = caption.split("\n\n").last()
    val header: String get() = caption.split("\n\n").first()
    val atMillis: Long get() = (at * 1000).toLong()
}

data class EventsResponse(
    val clips: List<Item>,
    val mode: String?,
    val atHome: Boolean?,
    val transmit: Boolean?,
    val session: String?,
) {
    data class Item(val id: Int, val at: Double, val caption: String, val durationS: Double)

    companion object {
        /** null — не JSON або без обов'язкових полів. */
        fun parse(body: String): EventsResponse? = try {
            val json = JSONObject(body)
            val array = json.getJSONArray("clips")
            val items = (0 until array.length()).map {
                val item = array.getJSONObject(it)
                Item(item.getInt("id"), item.getDouble("at"), item.getString("caption"), item.getDouble("duration_s"))
            }
            EventsResponse(
                items,
                json.optStringOrNull("mode"),
                if (json.has("at_home") && !json.isNull("at_home")) json.getBoolean("at_home") else null,
                if (json.has("transmit") && !json.isNull("transmit")) json.getBoolean("transmit") else null,
                json.optStringOrNull("session"),
            )
        } catch (e: Exception) {
            null
        }
    }
}

private fun JSONObject.optStringOrNull(key: String): String? =
    if (has(key) && !isNull(key)) getString(key) else null

/** Список фраз одного джерела: курсор, сеанс сервера й кеш. Джерело зникло — список лишається. */
class ClipFeed(cached: List<Clip> = emptyList()) {
    companion object {
        const val LIMIT = 30
    }

    var clips: List<Clip> = cached.take(LIMIT)   // нові зверху
        private set
    /** null — цього запуску сервер ще не відповідав. */
    var session: String? = null
        private set
    var cursor = 0
        private set
    private var primed = false

    sealed interface Outcome {
        /** Нові фрази для сповіщень. */
        data class Added(val clips: List<Clip>) : Outcome
        /** Нічого нового, перше завантаження або лише кеш. */
        data object Quiet : Outcome
        /** Сервер перезапустився: запитайте ще раз з курсора 0. */
        data object Restart : Outcome
    }

    fun merge(response: EventsResponse): Outcome {
        val current = response.session ?: ""
        val known = session
        if (known != null && known != current) {
            session = current
            cursor = 0
            return Outcome.Restart   // відповідь рахувалася від старого курсора
        }
        session = current
        val first = !primed
        primed = true
        val incoming = response.clips.map { Clip(it.id, current, it.at, it.caption, it.durationS) }
        val last = incoming.lastOrNull() ?: return Outcome.Quiet
        cursor = maxOf(cursor, last.number)
        val seen = clips.map { it.id }.toSet()
        val fresh = incoming.filter { it.id !in seen }
        if (fresh.isEmpty()) return Outcome.Quiet
        clips = (fresh + clips).sortedByDescending { it.at }.take(LIMIT)
        return if (first) Outcome.Quiet else Outcome.Added(fresh)
    }

    /** Звук фрази є лише на сервері поточного сеансу. */
    fun canReplay(clip: Clip): Boolean = session != null && clip.session == session
}

/** Кеш текстів на диску: видно й тоді, коли джерело вимкнене. */
class ClipCache(private val folder: File) {
    private fun file(key: String) = File(folder, "clips-$key.json")

    fun load(key: String): List<Clip> = try {
        val array = JSONArray(file(key).readText())
        (0 until array.length()).map {
            val o = array.getJSONObject(it)
            Clip(o.getInt("number"), o.getString("session"), o.getDouble("at"), o.getString("caption"), o.getDouble("duration_s"))
        }
    } catch (e: Exception) {
        emptyList()
    }

    fun save(clips: List<Clip>, key: String) {
        try {
            folder.mkdirs()
            val array = JSONArray()
            clips.forEach {
                array.put(JSONObject().put("number", it.number).put("session", it.session).put("at", it.at)
                    .put("caption", it.caption).put("duration_s", it.durationS))
            }
            val tmp = File(folder, "clips-$key.json.tmp")
            tmp.writeText(array.toString())
            tmp.renameTo(file(key))
        } catch (e: Exception) {
            // Кеш — лише зручність.
        }
    }
}

/** Стан одного джерела: помилка одного не змінює іншого. */
enum class SourceState {
    UNKNOWN, OK, OFFLINE, FORBIDDEN, OUTDATED;

    fun text(name: String): String = when (this) {
        UNKNOWN -> "$name: немає даних"
        OK -> "$name на зв'язку"
        OFFLINE -> "$name недоступний · перевірте Tailscale"
        FORBIDDEN -> "$name: доступ лише з вашого облікового запису Tailscale"
        OUTDATED -> "$name: оновіть застосунок"
    }

    companion object {
        fun from(status: Int?): SourceState = when (status) {
            200 -> OK
            403 -> FORBIDDEN
            426 -> OUTDATED
            else -> OFFLINE
        }
    }
}

/** `http://radio345:8346` — без шляху; порт типово 8346. null — адреси немає або вона некоректна. */
fun baseUrl(host: String): String? {
    var text = host.trim()
    if (text.isEmpty()) return null
    if (!text.contains("://")) text = "http://$text"
    return try {
        val uri = java.net.URI(text)
        val name = uri.host
        if (name.isNullOrEmpty()) return null
        val scheme = uri.scheme ?: "http"
        val port = if (uri.port == -1) 8346 else uri.port
        val shown = if (name.contains(':') && !name.startsWith("[")) "[$name]" else name
        "$scheme://$shown:$port"
    } catch (e: Exception) {
        null
    }
}

fun buildUrl(base: String, path: String, query: List<Pair<String, String>> = emptyList()): String {
    if (query.isEmpty()) return base + path
    return base + path + "?" + query.joinToString("&") { (k, v) ->
        java.net.URLEncoder.encode(k, "UTF-8") + "=" + java.net.URLEncoder.encode(v, "UTF-8")
    }
}
