import AVFoundation
import MediaPlayer
import UIKit
import UserNotifications

struct Clip: Decodable, Identifiable, Equatable {
    let id: Int
    let at: Double
    let caption: String
    let duration_s: Double

    var date: Date { Date(timeIntervalSince1970: at) }
    /// Останній абзац підпису — сам текст фрази (шапка «Подія / Кому» вище).
    var text: String { caption.components(separatedBy: "\n\n").last ?? caption }
    var header: String { caption.components(separatedBy: "\n\n").first ?? "" }
}

private struct EventsResponse: Decodable {
    let clips: [Clip]
    let mode: String?
    let at_home: Bool?
    let transmit: Bool?
}

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

/// Прослуховування потоку з Mac, список фраз, екран блокування.
/// Застосунок живе у фоні, поки грає потік (UIBackgroundModes: audio) — тому
/// опитування списку й локальні сповіщення працюють без push.
@MainActor
@Observable
final class RadioModel {
    let settings = ConnectionSettings()
    let transmitter = Transmitter()

    private(set) var isListening = false
    private(set) var state: PlayerState = .idle
    private(set) var clips: [Clip] = []
    private(set) var macReachable: Bool?
    private(set) var replayingId: Int?
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
    private var lastClipId = 0
    private var retries = 0
    private var pollTask: Task<Void, Never>?
    private var retryTask: Task<Void, Never>?

    init() {
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
            state = .failed("Вкажіть адресу Mac і токен")
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
        guard let url = settings.url(settings.channel.path(filterSignals: settings.filterSignals),
                                     query: [URLQueryItem(name: "s", value: String(Int(Date().timeIntervalSince1970)))]) else {
            state = .failed("Некоректна адреса Mac")
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
        guard let url = settings.url("/clip/\(clip.id).mp3") else { return }
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

    // MARK: Список фраз

    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.pollOnce()
                let active = UIApplication.shared.applicationState == .active
                // У фоні без звуку iOS однаково призупинить застосунок.
                if !active && !self.isListening { self.pollTask = nil; return }
                try? await Task.sleep(for: .seconds(active ? 3 : 5))
            }
        }
    }

    func refreshNow() async { await pollOnce() }

    private func pollOnce() async {
        guard let url = settings.url("/events", query: [URLQueryItem(name: "after", value: String(lastClipId))]) else {
            macReachable = nil
            return
        }
        var request = URLRequest(url: url, timeoutInterval: 6)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if code == 401 {
                macReachable = true
                if !isListening { state = .failed("Токен не підійшов") }
                return
            }
            guard code == 200 else { macReachable = false; return }
            macReachable = true
            let decoded = try JSONDecoder().decode(EventsResponse.self, from: data)
            updateHome(decoded.at_home)
            transmitter.serverEnabled = decoded.transmit ?? false
            let fresh = decoded.clips
            guard !fresh.isEmpty else { return }
            let isFirstLoad = lastClipId == 0
            lastClipId = fresh.last!.id
            clips = Array((fresh.reversed() + clips).prefix(30))
            updateNowPlaying()
            if !isFirstLoad, notifyInBackground, UIApplication.shared.applicationState != .active {
                fresh.forEach(postLocalNotification)
            }
        } catch {
            macReachable = false
        }
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
        await transmitter.begin(url: settings.url("/transmit"))
        if !transmitter.isTransmitting { streamPlayer?.muted = atHome }
    }

    func endTransmit() async {
        await transmitter.end()
        streamPlayer?.muted = atHome
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
