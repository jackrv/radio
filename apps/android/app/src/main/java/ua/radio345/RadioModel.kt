package ua.radio345

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.Build
import android.support.v4.media.MediaMetadataCompat
import android.support.v4.media.session.MediaSessionCompat
import android.support.v4.media.session.PlaybackStateCompat
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.ProcessLifecycleOwner
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.CacheControl
import okhttp3.Request
import java.io.File

sealed interface PlayerState {
    data object Idle : PlayerState
    data object Connecting : PlayerState
    data object Playing : PlayerState
    data class Retrying(val seconds: Int) : PlayerState
    data class Failed(val message: String) : PlayerState

    val text: String
        get() = when (this) {
            Idle -> "Не слухаю"
            Connecting -> "Підключаюсь…"
            Playing -> "Слухаю ефір"
            is Retrying -> "Зв'язок втрачено, повтор через $seconds с"
            is Failed -> message
        }
}

/**
 * Два незалежні джерела: ефір із передачею та тексти. Кожне має своє опитування, стан і повтори —
 * збій одного не зупиняє інше. Поки грає потік, процес тримає служба переднього плану
 * ([RadioService]), тож опитування й локальні сповіщення працюють у фоні без push.
 */
class RadioModel(private val context: Context) {
    private val prefs = context.getSharedPreferences("radio345", Context.MODE_PRIVATE)
    private val http = Transmitter.client()
    val settings = ConnectionSettings(prefs)
    val transmitter = Transmitter(http)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    var isListening by mutableStateOf(false)
        private set
    var state by mutableStateOf<PlayerState>(PlayerState.Idle)
        private set
    private val cache = ClipCache(File(context.filesDir, "Radio345"))
    private var feed = ClipFeed(cache.load(settings.textReceiver.raw))
    var clips by mutableStateOf(feed.clips)
        private set
    var airState by mutableStateOf(SourceState.UNKNOWN)
        private set
    var textState by mutableStateOf(SourceState.UNKNOWN)
        private set
    var replayingId by mutableStateOf<String?>(null)
        private set
    var lastError by mutableStateOf("")
        private set
    /** Останні рівні звуку для анімації (старі → нові). */
    var levels by mutableStateOf(List(36) { 0.0 })
        private set
    /** Зараз реально звучить передача (а не тиша між фразами). */
    val isSoundActive: Boolean get() = (levels.takeLast(5).maxOrNull() ?: 0.0) > 0.12

    /**
     * Ручний режим «Вдома»: звук вимкнено, щоб мікрофон Mac не чув телефон (петля).
     * Сам знімається, коли телефон виходить із мережі Mac; вмикається сам при поверненні, якщо так налаштовано.
     */
    private var _atHome by mutableStateOf(prefs.getBoolean("radio345.atHome", false))
    var atHome: Boolean
        get() = _atHome
        set(v) {
            _atHome = v
            prefs.edit().putBoolean("radio345.atHome", v).apply()
            streamPlayer?.muted = v || transmitter.isTransmitting
            updateNowPlaying()
        }
    /** Що бачить Mac: телефон у тій самій мережі (Tailscale напряму, приватна адреса). */
    private var inMacNetwork: Boolean? = null
    private var awayPolls = 0

    private var _notify by mutableStateOf(prefs.getBoolean("radio345.notify", false))
    /** Перед увімкненням інтерфейс запитує дозвіл на сповіщення (Android 13+). */
    var notifyInBackground: Boolean
        get() = _notify
        set(v) { _notify = v; prefs.edit().putBoolean("radio345.notify", v).apply() }

    private var streamPlayer: StreamPlayer? = null
    private var replayPlayer: MediaPlayer? = null
    private var retries = 0
    private var airJob: Job? = null
    private var textJob: Job? = null
    private var retryJob: Job? = null
    private var replayJob: Job? = null

    val session = MediaSessionCompat(context, "Radio345").apply {
        setCallback(object : MediaSessionCompat.Callback() {
            override fun onPlay() = start()
            override fun onPause() = stop()
            override fun onStop() = stop()
        })
    }

    private val isForeground: Boolean
        get() = ProcessLifecycleOwner.get().lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)

    init {
        createChannels()
        ProcessLifecycleOwner.get().lifecycle.addObserver(LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_START) startPolling()
        })
    }

    // Прослуховування

    fun toggle() = if (isListening) stop() else start()

    fun start() {
        if (!settings.isConfigured) {
            state = PlayerState.Failed("Вкажіть адресу ${settings.receiver.title} у налаштуваннях")
            return
        }
        isListening = true
        retries = 0
        session.isActive = true
        ContextCompat.startForegroundService(context, Intent(context, RadioService::class.java))
        connect()
        startPolling()
        updateNowPlaying()
    }

    fun stop() {
        isListening = false
        retryJob?.cancel()
        retryJob = null
        streamPlayer?.stop()
        streamPlayer = null
        levels = List(levels.size) { 0.0 }
        state = PlayerState.Idle
        updateNowPlaying()
        session.isActive = false
        context.stopService(Intent(context, RadioService::class.java))
    }

    fun switchChannel(channel: StreamChannel) {
        if (channel == settings.channel) return
        settings.channel = channel
        if (isListening) { retries = 0; connect() }
        updateNowPlaying()
    }

    private fun connect() {
        retryJob?.cancel()
        retryJob = null
        streamPlayer?.stop()
        streamPlayer = null
        // Новий запит щоразу: сервер віддає свіжий запас і живий край ефіру.
        val url = settings.url(settings.channel.path(settings.filterSignals), settings.streamReceiver,
            listOf("s" to (System.currentTimeMillis() / 1000).toString()))
        if (url == null) {
            state = PlayerState.Failed("Некоректна адреса ${settings.streamReceiver.title}")
            return
        }
        val maxQueued = if (settings.channel == StreamChannel.LIVE) 2.5 else 6.0
        lateinit var player: StreamPlayer
        player = StreamPlayer(url, maxQueued) { event -> if (streamPlayer === player) handle(event) }
        player.muted = atHome || transmitter.isTransmitting
        player.onLevel = { level -> if (streamPlayer === player) levels = levels.drop(1) + level }
        streamPlayer = player
        state = PlayerState.Connecting
        player.start()
    }

    private fun handle(event: StreamPlayer.Event) {
        if (!isListening) return
        when (event) {
            StreamPlayer.Event.Playing -> { retries = 0; state = PlayerState.Playing }
            is StreamPlayer.Event.Failed -> {
                streamPlayer?.stop()
                streamPlayer = null
                scheduleRetry(event.message)
            }
        }
    }

    private fun scheduleRetry(reason: String) {
        if (!isListening || retryJob != null) return
        lastError = reason
        retries = minOf(retries + 1, 6)
        val delaySeconds = 1 shl (retries - 1)  // 1, 2, 4 … 32 с
        state = PlayerState.Retrying(delaySeconds)
        retryJob = scope.launch {
            delay(delaySeconds * 1000L)
            retryJob = null
            if (isListening) connect()
        }
    }

    // Повтор фрази

    fun canReplay(clip: Clip): Boolean = textState == SourceState.OK && feed.canReplay(clip)

    fun replay(clip: Clip) {
        if (!feed.canReplay(clip)) return
        val url = settings.url("/clip/${clip.number}.mp3", settings.textReceiver) ?: return
        replayPlayer?.release()
        replayingId = clip.id
        replayPlayer = MediaPlayer().apply {
            setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            setOnPreparedListener { it.start() }
            setOnCompletionListener { if (replayingId == clip.id) replayingId = null }
            setOnErrorListener { _, _, _ -> if (replayingId == clip.id) replayingId = null; true }
            try { setDataSource(url); prepareAsync() } catch (e: Exception) { replayingId = null }
        }
        replayJob?.cancel()
        replayJob = scope.launch {
            delay(((clip.durationS + 2) * 1000).toLong())
            if (replayingId == clip.id) replayingId = null
        }
    }

    // Опитування джерел

    /** Окремі цикли: тайм-аут вимкненого вузла не затримує інший. */
    fun startPolling() {
        if (airJob?.isActive != true) airJob = pollLoop { pollAir() }
        if (textJob?.isActive != true) textJob = pollLoop { if (settings.hasSeparateTexts) pollTexts() }
    }

    private fun pollLoop(step: suspend () -> Unit): Job = scope.launch {
        while (isActive) {
            step()
            // У фоні без звуку опитування не потрібне: служби немає, Android однаково заморозить процес.
            if (!isForeground && !isListening) return@launch
            delay(if (isForeground) 3000 else 5000)
        }
    }

    suspend fun refreshNow() {
        val air = scope.async { pollAir() }
        val texts = scope.async { if (settings.hasSeparateTexts) pollTexts() }
        air.await(); texts.await()
    }

    private suspend fun fetchEvents(source: Receiver, after: Long): Pair<SourceState, EventsResponse?> {
        val url = settings.url("/events", source, listOf("after" to after.toString(), "api" to "1"))
            ?: return SourceState.UNKNOWN to null
        return withContext(Dispatchers.IO) {
            try {
                val request = Request.Builder().url(url).cacheControl(CacheControl.FORCE_NETWORK).build()
                http.newCall(request).execute().use { response ->
                    val state = SourceState.from(response.code)
                    if (state != SourceState.OK) return@withContext state to null
                    val decoded = EventsResponse.parse(response.body?.string() ?: "")
                        ?: return@withContext SourceState.OFFLINE to null
                    SourceState.OK to decoded
                }
            } catch (e: Exception) {
                SourceState.OFFLINE to null
            }
        }
    }

    private suspend fun pollAir() {
        val source = settings.receiver
        val shared = !settings.hasSeparateTexts
        val (state, response) = fetchEvents(source, if (shared) feed.cursor.toLong() else Int.MAX_VALUE.toLong())
        // Поки чекали відповіді, користувач міг перемкнути вузол.
        if (source != settings.receiver || shared == settings.hasSeparateTexts) return
        airState = state
        if (shared) textState = state
        if (response == null) {
            transmitter.serverEnabled = false
            if (!isListening && (state == SourceState.FORBIDDEN || state == SourceState.OUTDATED)) {
                this.state = PlayerState.Failed(state.text(source.title))
            }
            return
        }
        updateHome(response.atHome)
        transmitter.serverEnabled = response.transmit ?: false
        if (settings.streamReceiver == source) reconnectNow()
        if (shared) accept(response, source)
    }

    private suspend fun pollTexts() {
        val source = settings.textReceiver
        val (state, response) = fetchEvents(source, feed.cursor.toLong())
        if (!settings.hasSeparateTexts || source != settings.textReceiver) return
        textState = state
        if (response == null) return
        if (settings.streamReceiver == source) reconnectNow()
        accept(response, source)
    }

    private suspend fun accept(response: EventsResponse, source: Receiver) {
        val target = feed
        var outcome = target.merge(response)
        if (outcome == ClipFeed.Outcome.Restart) {
            val (state, again) = fetchEvents(source, 0)
            if (source != settings.textReceiver || state != SourceState.OK || again == null || target !== feed) return
            outcome = target.merge(again)
        }
        save()
        if (outcome is ClipFeed.Outcome.Added && notifyInBackground && !isForeground) {
            outcome.clips.forEach(::postClipNotification)
        }
    }

    private var savedIds: List<String> = clips.map { it.id }

    private fun save() {
        clips = feed.clips
        val ids = clips.map { it.id }
        if (ids == savedIds) return
        savedIds = ids
        cache.save(clips, settings.textReceiver.raw)
        updateNowPlaying()
    }

    /** Вузол відповідає: не чекати до 32 с паузи. */
    private fun reconnectNow() {
        if (!isListening || state !is PlayerState.Retrying) return
        retries = 0
        connect()
    }

    /** Наскільки зараз «світиться» інтерфейс: рівень звуку, що реально грає. */
    val glow: Double
        get() {
            if (!isListening || atHome || state != PlayerState.Playing) return 0.0
            return levels.takeLast(4).average()
        }

    // Передача

    fun beginTransmit() {
        streamPlayer?.muted = true  // не чути себе й не заважати
        transmitter.begin(settings.url("/transmit", settings.receiver))
        if (!transmitter.isTransmitting) streamPlayer?.muted = atHome
    }

    fun endTransmit() {
        transmitter.end()
        streamPlayer?.muted = atHome
    }

    /** Інший вузол ефіру або текстів: свій стан, курсор і кеш; нічого зі старого не змішується. */
    fun receiverChanged() {
        airJob?.cancel(); textJob?.cancel()
        airJob = null; textJob = null
        resetFeed()
        airState = SourceState.UNKNOWN
        textState = SourceState.UNKNOWN
        inMacNetwork = null
        awayPolls = 0
        transmitter.serverEnabled = false
        settingsChanged()
        startPolling()
    }

    /** Лише тексти: звук «Наживо» з вузла ефіру не переривається. */
    fun textSourceChanged() {
        airJob?.cancel(); textJob?.cancel()
        airJob = null; textJob = null
        resetFeed()
        textState = SourceState.UNKNOWN
        if (settings.channel == StreamChannel.SELECTED) settingsChanged()
        startPolling()
    }

    private fun resetFeed() {
        feed = ClipFeed(cache.load(settings.textReceiver.raw))
        clips = feed.clips
        savedIds = clips.map { it.id }
    }

    /** Адресу вузла змінено: перезапускається лише те, що з нього працює. */
    fun hostChanged(source: Receiver) {
        if (settings.receiver == source) receiverChanged()
        else if (settings.textReceiver == source) textSourceChanged()
    }

    /** Перезапуск потоку після зміни налаштувань (фільтр сигналів). */
    fun settingsChanged() {
        if (isListening) { retries = 0; connect() }
    }

    private fun updateHome(home: Boolean?) {
        if (home == null) return
        val previous = inMacNetwork
        inMacNetwork = home
        if (home) {
            awayPolls = 0
            if (previous == false && settings.autoHome) atHome = true
        } else {
            // Дві відповіді поспіль: коротке перемикання Tailscale на ретранслятор не знімає режим.
            awayPolls++
            if (awayPolls >= 2 && atHome) atHome = false
        }
    }

    // Екран блокування й сповіщення

    private val artwork by lazy { BitmapFactory.decodeResource(context.resources, R.mipmap.ic_launcher) }

    val nowPlayingTitle: String get() = clips.firstOrNull()?.text ?: "Ефір"
    val nowPlayingSubtitle: String
        get() = "Рація 345 · ${settings.channel.title}${if (atHome) " · вдома, без звуку" else ""}"

    private fun updateNowPlaying() {
        val playing = isListening
        session.setPlaybackState(PlaybackStateCompat.Builder()
            .setActions(PlaybackStateCompat.ACTION_PLAY or PlaybackStateCompat.ACTION_PAUSE or
                PlaybackStateCompat.ACTION_STOP or PlaybackStateCompat.ACTION_PLAY_PAUSE)
            .setState(if (playing) PlaybackStateCompat.STATE_PLAYING else PlaybackStateCompat.STATE_STOPPED,
                PlaybackStateCompat.PLAYBACK_POSITION_UNKNOWN, 1f)
            .build())
        session.setMetadata(MediaMetadataCompat.Builder()
            .putString(MediaMetadataCompat.METADATA_KEY_TITLE, nowPlayingTitle)
            .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, nowPlayingSubtitle)
            .putBitmap(MediaMetadataCompat.METADATA_KEY_ART, artwork)
            .build())
        if (playing) RadioService.refresh(context, this)
    }

    private fun createChannels() {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL_LISTEN, "Прослуховування",
            NotificationManager.IMPORTANCE_LOW).apply { setShowBadge(false) })
        manager.createNotificationChannel(NotificationChannel(CHANNEL_CLIPS, "Фрази з ефіру",
            NotificationManager.IMPORTANCE_DEFAULT).apply { setSound(null, null) })
    }

    fun canNotify(): Boolean = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    private fun postClipNotification(clip: Clip) {
        if (!canNotify()) return
        val open = PendingIntent.getActivity(context, 0, Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)
        val notification = NotificationCompat.Builder(context, CHANNEL_CLIPS)
            .setSmallIcon(R.drawable.ic_radio)
            .setContentTitle(clip.header.replace("\n", " · "))
            .setContentText(clip.text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(clip.text))
            .setGroup("radio345.clips")
            .setWhen(clip.atMillis)
            .setShowWhen(true)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        context.getSystemService(NotificationManager::class.java).notify(clip.id.hashCode(), notification)
    }

    companion object {
        const val CHANNEL_LISTEN = "listen"
        const val CHANNEL_CLIPS = "clips"
    }
}
