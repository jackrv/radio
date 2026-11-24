import AVFoundation
import MediaPlayer
import UIKit
import UserNotifications

enum PlayerState: Equatable {
    case idle, connecting, playing, buffering
    case retrying(seconds: Int)
    case failed(String)

    var text: String {
        switch self {
        case .idle: "Не слухаю"
        case .connecting: "Підключаюсь…"
        case .playing: "Слухаю ефір"
        case .buffering: "Буферизація…"
        case .retrying(let s): "Зв'язок втрачено, повтор через \(s) с"
        case .failed(let message): message
        }
    }
}

/// Два незалежні джерела: ефір із передачею та тексти. Кожне має своє опитування, стан і повтори —
/// збій одного не зупиняє інше. Плюс екран блокування.
/// Застосунок живе у фоні, поки грає потік (UIBackgroundModes: audio) — тому
/// опитування списку й локальні сповіщення працюють без push.
@MainActor
@Observable
final class RadioModel {
    let settings = ConnectionSettings()
    let transmitter = Transmitter()

    private(set) var isListening = false
    private(set) var state: PlayerState = .idle
    private var feed = ClipFeed()
    var clips: [Clip] { feed.clips }
    private(set) var airState: SourceState = .unknown
    private(set) var textState: SourceState = .unknown
    private(set) var replayingId: String?
    private(set) var lastError = ""
    /// Останні рівні звуку для анімації (старі → нові).
    private(set) var levels: [Double] = Array(repeating: 0, count: 36)
    /// Зараз реально звучить передача (а не тиша між фразами).
    var isSoundActive: Bool { (levels.suffix(5).max() ?? 0) > 0.12 }
    /// Ручний режим «Вдома»: звук вимкнено, щоб мікрофон Mac не чув телефон (петля).
    /// Сам знімається, коли iPhone виходить із мережі Mac; вмикається сам при поверненні,
    /// якщо так налаштовано.
    var atHome = UserDefaults.standard.bool(forKey: "radio345.atHome") {
        didSet {
            UserDefaults.standard.set(atHome, forKey: "radio345.atHome")
            streamPlayer?.muted = atHome || transmitter.isTransmitting
            updateNowPlaying()
        }
    }
    /// Що бачить Mac: iPhone у тій самій мережі (Tailscale напряму, приватна адреса).
    private(set) var inMacNetwork: Bool?
    private var awayPolls = 0
    var notifyInBackground = UserDefaults.standard.bool(forKey: "radio345.notify") {
        didSet {
            UserDefaults.standard.set(notifyInBackground, forKey: "radio345.notify")
            if notifyInBackground { requestNotificationPermission() }
        }
    }

    private var streamPlayer: StreamPlayer?
    private let replayPlayer = AVPlayer()
    private let cache = ClipCache(folder: URL.applicationSupportDirectory.appendingPathComponent("Radio345"))
    private var retries = 0
    private var airTask: Task<Void, Never>?
    private var textTask: Task<Void, Never>?
    private var retryTask: Task<Void, Never>?

    init() {
        feed = ClipFeed(cached: cache.load(settings.textReceiver.rawValue))
        let center = NotificationCenter.default
        center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            let type = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt).flatMap(AVAudioSession.InterruptionType.init)
            Task { @MainActor in if type == .ended { self?.resumeAfterInterruption() } }
        }
        center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.resumeAfterInterruption() }
        }
        center.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.startPolling() }
        }
        setupRemoteCommands()
        startPolling()
    }

    // MARK: Прослуховування

    func toggle() { isListening ? stop() : start() }

    func start() {
        guard settings.isConfigured else {
            state = .failed("Вкажіть адресу \(settings.receiver.title) у налаштуваннях")
            return
        }
        do {
            try AudioSessionSetup.activate()
        } catch {
            state = .failed("Аудіосесія недоступна: \(error.localizedDescription)")
            return
        }
        isListening = true
        retries = 0
        connect()
        startPolling()
        updateNowPlaying()
    }

    func stop() {
        isListening = false
        retryTask?.cancel()
        retryTask = nil
        streamPlayer?.stop()
        streamPlayer = nil
        levels = Array(repeating: 0, count: levels.count)
        state = .idle
        updateNowPlaying()
    }

    func switchChannel(_ channel: StreamChannel) {
        guard channel != settings.channel else { return }
        settings.channel = channel
        if isListening { retries = 0; connect() }
        updateNowPlaying()
    }

    private func connect() {
        retryTask?.cancel()
        retryTask = nil
        streamPlayer?.stop()
        // Новий запит щоразу: сервер віддає свіжий запас і живий край ефіру.
        guard let url = settings.url(settings.channel.path(filterSignals: settings.filterSignals), from: settings.streamReceiver,
                                     query: [URLQueryItem(name: "s", value: String(Int(Date().timeIntervalSince1970)))]) else {
            state = .failed("Некоректна адреса \(settings.streamReceiver.title)")
            return
        }
        let player = StreamPlayer(url: url, maxQueuedSeconds: settings.channel == .live ? 2.5 : 6) { [weak self] event in
            MainActor.assumeIsolated { self?.handle(event) }
        }
        player.muted = atHome
        player.onLevel = { [weak self] level in
            guard let self else { return }
            self.levels.removeFirst()
            self.levels.append(level)
        }
        streamPlayer = player
        state = .connecting
        player.start()
    }

    private func handle(_ event: StreamPlayer.Event) {
        guard isListening else { return }
        switch event {
        case .playing:
            retries = 0
            state = .playing
        case .failed(let message):
            streamPlayer?.stop()
            streamPlayer = nil
            scheduleRetry(reason: message)
        }
    }

    private func scheduleRetry(reason: String = "") {
        guard isListening, retryTask == nil else { return }
        lastError = reason
        retries = min(retries + 1, 6)
        let delay = 1 << (retries - 1)  // 1, 2, 4 … 32 с
        state = .retrying(seconds: delay)
        retryTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled, let self, self.isListening else { return }
            self.retryTask = nil
            self.connect()
        }
    }

    private func resumeAfterInterruption() {
        guard isListening else { return }
        try? AVAudioSession.sharedInstance().setActive(true)
        retries = 0
        connect()
    }

    // MARK: Повтор фрази

    func replay(_ clip: Clip) {
        guard feed.canReplay(clip), let url = settings.url("/clip/\(clip.number).mp3", from: settings.textReceiver) else { return }
        if !isListening {
            try? AudioSessionSetup.activate()
        }
        replayingId = clip.id
        replayPlayer.replaceCurrentItem(with: AVPlayerItem(url: url))
        replayPlayer.play()
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(clip.duration_s + 2))
            if self?.replayingId == clip.id { self?.replayingId = nil }
        }
    }

    // MARK: Опитування джерел

    func canReplay(_ clip: Clip) -> Bool { textState == .ok && feed.canReplay(clip) }

    /// Окремі цикли: тайм-аут вимкненого вузла не затримує інший.
    func startPolling() {
        if airTask == nil {
            airTask = pollLoop { await $0.pollAir() } finished: { $0.airTask = nil }
        }
        if textTask == nil {
            textTask = pollLoop { if $0.settings.hasSeparateTexts { await $0.pollTexts() } } finished: { $0.textTask = nil }
        }
    }

    private func pollLoop(_ step: @escaping @MainActor (RadioModel) async -> Void,
                          finished: @escaping @MainActor (RadioModel) -> Void) -> Task<Void, Never> {
        Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await step(self)
                guard !Task.isCancelled else { return }
                let active = UIApplication.shared.applicationState == .active
                // У фоні без звуку iOS однаково призупинить застосунок.
                if !active && !self.isListening { finished(self); return }
                try? await Task.sleep(for: .seconds(active ? 3 : 5))
            }
        }
    }

    func refreshNow() async {
        async let air: Void = pollAir()
        async let texts: Void = settings.hasSeparateTexts ? pollTexts() : ()
        _ = await (air, texts)
    }

    private func fetchEvents(_ source: Receiver, after: Int) async -> (SourceState, EventsResponse?) {
        guard let url = settings.url("/events", from: source, query: [URLQueryItem(name: "after", value: String(after)),
                                                                      URLQueryItem(name: "api", value: "1")]) else {
            return (.unknown, nil)
        }
        var request = URLRequest(url: url, timeoutInterval: 6)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        guard let (data, response) = try? await URLSession.shared.data(for: request) else { return (.offline, nil) }
        let state = SourceState(status: (response as? HTTPURLResponse)?.statusCode)
        guard state == .ok else { return (state, nil) }
        guard let decoded = try? JSONDecoder().decode(EventsResponse.self, from: data) else { return (.offline, nil) }
        return (.ok, decoded)
    }

    private func pollAir() async {
        let source = settings.receiver
        let shared = !settings.hasSeparateTexts
        let (state, response) = await fetchEvents(source, after: shared ? feed.cursor : Int.max)
        // Поки чекали відповіді, користувач міг перемкнути вузол.
        guard !Task.isCancelled, source == settings.receiver, shared == !settings.hasSeparateTexts else { return }
        airState = state
        if shared { textState = state }
        guard let response else {
            transmitter.serverEnabled = false
            if !isListening, state == .forbidden || state == .outdated { self.state = .failed(state.text(source.title)) }
            return
        }
        updateHome(response.at_home)
        transmitter.serverEnabled = response.transmit ?? false
        if settings.streamReceiver == source { reconnectNow() }
        if shared { await accept(response, from: source) }
    }

    private func pollTexts() async {
        let source = settings.textReceiver
        let (state, response) = await fetchEvents(source, after: feed.cursor)
        guard !Task.isCancelled, settings.hasSeparateTexts, source == settings.textReceiver else { return }
        textState = state
        guard let response else { return }
        if settings.streamReceiver == source { reconnectNow() }
        await accept(response, from: source)
    }

    private func accept(_ response: EventsResponse, from source: Receiver) async {
        var outcome = feed.merge(response)
        if outcome == .restart {
            let (state, again) = await fetchEvents(source, after: 0)
            guard !Task.isCancelled, source == settings.textReceiver, state == .ok, let again else { return }
            outcome = feed.merge(again)
        }
        guard case .added(let fresh) = outcome else { save(); return }
        save()
        updateNowPlaying()
        if notifyInBackground, UIApplication.shared.applicationState != .active {
            fresh.forEach(postLocalNotification)
        }
    }

    private var savedIds: [String] = []
    private func save() {
        let ids = clips.map(\.id)
        guard ids != savedIds else { return }
        savedIds = ids
        cache.save(clips, settings.textReceiver.rawValue)
        updateNowPlaying()
    }

    /// Вузол відповідає: не чекати до 32 с паузи. На кожній успішній відповіді, а не лише на першій після збою:
    /// перша могла прийти, коли невдала спроба ще тривала (стан .connecting).
    private func reconnectNow() {
        guard isListening, case .retrying = state else { return }
        retries = 0
        connect()
    }

    /// Наскільки зараз «світиться» інтерфейс: рівень звуку, що реально грає.
    var glow: Double {
        guard isListening, !atHome, state == .playing else { return 0 }
        let recent = levels.suffix(4)
        return recent.reduce(0, +) / Double(recent.count)
    }

    // MARK: Передача

    func beginTransmit() async {
        streamPlayer?.muted = true  // не чути себе й не заважати
        await transmitter.begin(url: settings.url("/transmit", from: settings.receiver))
        if !transmitter.isTransmitting { streamPlayer?.muted = atHome }
    }

    func endTransmit() async {
        await transmitter.end()
        streamPlayer?.muted = atHome
    }

    /// Інший вузол ефіру або текстів: свій стан, курсор і кеш; нічого зі старого не змішується.
    func receiverChanged() {
        airTask?.cancel(); textTask?.cancel()
        airTask = nil; textTask = nil
        feed = ClipFeed(cached: cache.load(settings.textReceiver.rawValue))
        savedIds = clips.map(\.id)
        airState = .unknown
        textState = .unknown
        inMacNetwork = nil
        awayPolls = 0
        transmitter.serverEnabled = false
        settingsChanged()
        startPolling()
    }

    /// Лише тексти: звук «Наживо» з вузла ефіру не переривається.
    func textSourceChanged() {
        textTask?.cancel()
        textTask = nil
        airTask?.cancel()   // лише опитування; сам потік окремо
        airTask = nil
        feed = ClipFeed(cached: cache.load(settings.textReceiver.rawValue))
        savedIds = clips.map(\.id)
        textState = .unknown
        if settings.channel == .selected { settingsChanged() }
        startPolling()
    }

    /// Адресу вузла змінено: перезапускається лише те, що з нього працює.
    func hostChanged(_ source: Receiver) {
        if settings.receiver == source { receiverChanged() }
        else if settings.textReceiver == source { textSourceChanged() }
    }

    /// Перезапуск потоку після зміни налаштувань (фільтр сигналів).
    func settingsChanged() {
        if isListening { retries = 0; connect() }
    }

    private func updateHome(_ home: Bool?) {
        guard let home else { return }
        let previous = inMacNetwork
        inMacNetwork = home
        if home {
            awayPolls = 0
            if previous == false, settings.autoHome { atHome = true }
        } else {
            // Дві відповіді поспіль: коротке перемикання Tailscale на ретранслятор не знімає режим.
            awayPolls += 1
            if awayPolls >= 2, atHome { atHome = false }
        }
    }

    // MARK: Екран блокування й сповіщення

    private func setupRemoteCommands() {
        let center = MPRemoteCommandCenter.shared()
        center.playCommand.addTarget { [weak self] _ in self?.start(); return .success }
        center.pauseCommand.addTarget { [weak self] _ in self?.stop(); return .success }
        center.stopCommand.addTarget { [weak self] _ in self?.stop(); return .success }
        center.togglePlayPauseCommand.addTarget { [weak self] _ in self?.toggle(); return .success }
    }

    private func updateNowPlaying() {
        guard isListening else {
            MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
            return
        }
        var info: [String: Any] = [
            MPMediaItemPropertyArtist: "Рація 345 · \(settings.channel.title)\(atHome ? " · вдома, без звуку" : "")",
            MPNowPlayingInfoPropertyIsLiveStream: true,
            MPNowPlayingInfoPropertyPlaybackRate: 1.0,
        ]
        info[MPMediaItemPropertyTitle] = clips.first?.text ?? "Ефір"
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in }
    }

    private func postLocalNotification(_ clip: Clip) {
        let content = UNMutableNotificationContent()
        content.title = clip.header.replacingOccurrences(of: "\n", with: " · ")
        content.body = clip.text
        content.threadIdentifier = "radio345.clips"
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "clip-\(clip.id)", content: content, trigger: nil))
    }
}
