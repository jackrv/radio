import AVFoundation
import Foundation

/// Передача з iPhone (PTT): мікрофон → PCM16 моно 16 кГц → WebSocket `/transmit` на Mac.
/// Поки до Mac не підключено передавач, Mac відтворює звук на своїх динаміках; з передавачем
/// той самий потік піде на його вхід разом із ключем PTT. На час передачі Mac не слухає
/// мікрофон (напівдуплекс), а тут вимикається прийом, щоб не чути себе.
///
/// Мікрофонний рушій готується заздалегідь (`prepare`), тож натискання лише запускає його:
/// інакше губилися перші ~0,2 с фрази. Індикатор мікрофона горить тільки під час передачі.
///
/// З платним акаунтом можна додати фреймворк PushToTalk (`PTChannelManager`).
@MainActor
@Observable
final class Transmitter {
    /// Mac дозволяє передачу (`transmit` у /events).
    var serverEnabled = false {
        didSet { if serverEnabled && !oldValue { prepare() } }
    }
    private(set) var isTransmitting = false
    private(set) var level: Double = 0
    /// Останні рівні голосу для кругових смуг (старі → нові).
    private(set) var levels: [Double] = Array(repeating: 0, count: 48)
    var lastError: String?

    var isAvailable: Bool { serverEnabled }
    var unavailableReason: String { "Передачу на приймачі вимкнено" }

    private var engine: AVAudioEngine?
    private let outlet = SocketOutlet()

    /// Рушій із тапом і конвертером без запуску мікрофона.
    func prepare() {
        guard engine == nil else { return }
        do { try AudioSessionSetup.activate() } catch { return }
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.sampleRate > 0,
              let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true),
              let converter = AVAudioConverter(from: inFormat, to: outFormat) else { return }
        let outlet = self.outlet
        input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { [weak self] buffer, _ in
            guard let socket = outlet.socket,
                  let data = Self.convert(buffer, with: converter, to: outFormat) else { return }
            socket.send(.data(data.pcm)) { error in
                if error != nil { Task { @MainActor in self?.failed("Зв'язок із приймачем перервано") } }
            }
            Task { @MainActor in self?.push(level: data.level) }
        }
        engine.prepare()
        self.engine = engine
    }

    func begin(url: URL?) async {
        guard !isTransmitting else { return }
        lastError = nil
        switch AVAudioApplication.shared.recordPermission {
        case .granted: break
        case .undetermined:
            // Перший раз: системний запит; передача почнеться з наступного натискання.
            _ = await AVAudioApplication.requestRecordPermission()
            return
        default:
            lastError = "Немає доступу до мікрофона: Налаштування → Рація 345"
            return
        }
        guard let url, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            lastError = "Некоректна адреса приймача"
            return
        }
        parts.scheme = parts.scheme == "https" ? "wss" : "ws"
        guard let wsURL = parts.url else { return }
        prepare()
        guard let engine else {
            lastError = "Мікрофон недоступний"
            return
        }
        isTransmitting = true  // інтерфейс реагує одразу
        let socket = URLSession.shared.webSocketTask(with: wsURL)
        socket.resume()        // повідомлення до завершення рукостискання стають у чергу
        outlet.socket = socket
        do {
            try engine.start()
        } catch {
            lastError = "Передача не почалася: \(error.localizedDescription)"
            await end()
        }
    }

    func end() async {
        let socket = outlet.socket
        outlet.socket = nil
        engine?.stop()
        engine?.prepare()
        socket?.cancel(with: .normalClosure, reason: nil)
        isTransmitting = false
        level = 0
        levels = Array(repeating: 0, count: levels.count)
    }

    private func push(level: Double) {
        guard isTransmitting else { return }
        self.level = level
        levels.removeFirst()
        levels.append(level)
    }

    private func failed(_ message: String) {
        guard isTransmitting else { return }
        lastError = message
        Task { await end() }
    }

    private nonisolated static func convert(_ buffer: AVAudioPCMBuffer, with converter: AVAudioConverter,
                                            to format: AVAudioFormat) -> (pcm: Data, level: Double)? {
        let ratio = format.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return nil }
        var provided = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if provided { status.pointee = .noDataNow; return nil }
            provided = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, out.frameLength > 0, let samples = out.int16ChannelData?[0] else { return nil }
        let count = Int(out.frameLength)
        var sum: Double = 0
        for i in 0..<count { let v = Double(samples[i]) / 32768; sum += v * v }
        let db = 20 * log10(max(sqrt(sum / Double(count)), 1e-6))
        return (Data(bytes: samples, count: count * 2), min(max((db + 50) / 40, 0), 1))
    }
}

/// Поточне з'єднання для аудіопотоку (тап працює не в головному потоці).
private final class SocketOutlet: @unchecked Sendable {
    private let lock = NSLock()
    private var _socket: URLSessionWebSocketTask?
    var socket: URLSessionWebSocketTask? {
        get { lock.lock(); defer { lock.unlock() }; return _socket }
        set { lock.lock(); _socket = newValue; lock.unlock() }
    }
}

/// Одна категорія на весь час роботи: зміна категорії посеред прийому перезапускає аудіо.
enum AudioSessionSetup {
    static func activate() throws {
        let session = AVAudioSession.sharedInstance()
        if session.category != .playAndRecord {
            try session.setCategory(.playAndRecord, mode: .default,
                                    options: [.defaultToSpeaker, .allowBluetoothA2DP])
        }
        // Інакше iOS вимикає вібрацію, поки мікрофон готовий до запису.
        try? session.setAllowHapticsAndSystemSoundsDuringRecording(true)
        try session.setActive(true)
    }
}
