import AVFoundation
import AudioToolbox

/// Безперервний MP3 по HTTP → AudioFileStream → AVAudioConverter → AVAudioEngine.
///
/// AVPlayer для нескінченного потоку чекає «запасу» і зупиняється, якщо дані йдуть
/// рівно в реальному часі. Тут граємо одразу те, що прийшло; якщо черга більша за
/// `maxQueuedSeconds` (після ривка мережі), нові пакети відкидаються — затримка не росте.
/// Сервер шле дані й у тиші, тож 6 с без даних означає обрив.
final class StreamPlayer: NSObject, URLSessionDataDelegate {
    enum Event {
        case playing
        case failed(String)
    }

    private let url: URL
    private let maxQueuedSeconds: Double
    private let onEvent: (Event) -> Void

    private let work = OperationQueue()
    private var session: URLSession?
    private var fileStream: AudioFileStreamID?
    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private var inputFormat: AVAudioFormat?
    private var outputFormat: AVAudioFormat?
    private var converter: AVAudioConverter?

    private let lock = NSLock()
    private var queuedFrames: Int64 = 0
    private var lastDataAt = Date()
    private var stopped = false
    private var announced = false
    private var failed = false
    private var watchdog: DispatchSourceTimer?

    /// Рівень того, що реально грає (0…1), ~15 разів на секунду; викликається в головному потоці.
    /// Міряється до мікшера, тож у режимі «Вдома» видно активність і без звуку.
    var onLevel: ((Double) -> Void)?

    /// «Вдома»: з'єднання й рушій працюють (застосунок не засинає у фоні), звук вимкнено.
    var muted = false {
        didSet { engine.mainMixerNode.outputVolume = muted ? 0 : 1 }
    }
    private var configObserver: NSObjectProtocol?

    init(url: URL, maxQueuedSeconds: Double, onEvent: @escaping (Event) -> Void) {
        self.url = url
        self.maxQueuedSeconds = maxQueuedSeconds
        self.onEvent = onEvent
        super.init()
        work.maxConcurrentOperationCount = 1
        work.name = "radio345.stream"
    }

    func start() {
        let status = AudioFileStreamOpen(Unmanaged.passUnretained(self).toOpaque(),
                                         streamPropertyListener, streamPacketsCallback,
                                         kAudioFileMP3Type, &fileStream)
        guard status == noErr else { return fail("Аудіодекодер недоступний (\(status))") }
        engine.attach(node)
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in self?.fail("Аудіовихід змінився") }

        let config = URLSessionConfiguration.default
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 15
        config.urlCache = nil
        let session = URLSession(configuration: config, delegate: self, delegateQueue: work)
        self.session = session
        touch()
        session.dataTask(with: url).resume()

        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 2, repeating: 2)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let silentFor = Date().timeIntervalSince(self.lastDataAt)
            self.lock.unlock()
            if silentFor > 6 { self.fail("Немає даних від Mac") }
        }
        timer.resume()
        watchdog = timer
    }

    func stop() {
        lock.lock()
        let already = stopped
        stopped = true
        lock.unlock()
        guard !already else { return }
        watchdog?.cancel()
        watchdog = nil
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        session?.invalidateAndCancel()
        session = nil
        node.removeTap(onBus: 0)
        node.stop()
        engine.stop()
        let stream = fileStream
        fileStream = nil
        // Закриваємо парсер у тій самій черзі, де він працює.
        work.addOperation { if let stream { AudioFileStreamClose(stream) } }
    }

    private var isStopped: Bool {
        lock.lock(); defer { lock.unlock() }
        return stopped
    }

    private func touch() {
        lock.lock(); lastDataAt = Date(); lock.unlock()
    }

    private func fail(_ message: String) {
        lock.lock()
        let report = !stopped && !failed
        failed = true
        lock.unlock()
        guard report else { return }
        DispatchQueue.main.async { self.onEvent(.failed(message)) }
    }

    // MARK: URLSession

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            completionHandler(.cancel)
            return fail(code == 401 ? "Токен не підійшов" : "Mac відповів \(code)")
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard !isStopped, let stream = fileStream else { return }
        touch()
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            _ = AudioFileStreamParseBytes(stream, UInt32(raw.count), base, [])
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        fail(error.map { "Зв'язок перервано: \($0.localizedDescription)" } ?? "Mac закрив з'єднання")
    }

    // MARK: AudioFileStream

    fileprivate func streamProperty(_ stream: AudioFileStreamID, _ id: AudioFileStreamPropertyID) {
        guard id == kAudioFileStreamProperty_ReadyToProducePackets, inputFormat == nil else { return }
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        guard AudioFileStreamGetProperty(stream, kAudioFileStreamProperty_DataFormat, &size, &asbd) == noErr,
              let input = AVAudioFormat(streamDescription: &asbd),
              let output = AVAudioFormat(standardFormatWithSampleRate: asbd.mSampleRate,
                                         channels: max(asbd.mChannelsPerFrame, 1)),
              let converter = AVAudioConverter(from: input, to: output) else {
            return fail("Непідтримуваний формат потоку")
        }
        inputFormat = input
        outputFormat = output
        self.converter = converter
        engine.connect(node, to: engine.mainMixerNode, format: output)
        node.installTap(onBus: 0, bufferSize: 1024, format: output) { [weak self] buffer, _ in
            guard let self, let data = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
            var sum: Float = 0
            for i in 0..<Int(buffer.frameLength) { sum += data[i] * data[i] }
            let rms = sqrt(sum / Float(buffer.frameLength))
            let db = 20 * log10(max(rms, 1e-6))
            let level = Double(min(max((db + 50) / 40, 0), 1))  // −50…−10 дБ → 0…1
            DispatchQueue.main.async { if !self.isStopped { self.onLevel?(level) } }
        }
        let mute = DispatchQueue.main.sync { self.muted }
        engine.mainMixerNode.outputVolume = mute ? 0 : 1
        do {
            try engine.start()
            node.play()
        } catch {
            fail("Аудіовихід недоступний: \(error.localizedDescription)")
        }
    }

    fileprivate func streamPackets(_ byteCount: UInt32, _ packetCount: UInt32, _ data: UnsafeRawPointer,
                                   _ descriptions: UnsafeMutablePointer<AudioStreamPacketDescription>?) {
        guard packetCount > 0, let input = inputFormat, let output = outputFormat, let converter,
              !isStopped else { return }

        lock.lock()
        let queued = Double(queuedFrames) / output.sampleRate
        lock.unlock()
        if queued > maxQueuedSeconds { return }  // наздоганяємо ефір замість росту затримки

        let compressed = AVAudioCompressedBuffer(format: input, packetCapacity: packetCount,
                                                 maximumPacketSize: Int(byteCount))
        compressed.data.copyMemory(from: data, byteCount: Int(byteCount))
        compressed.byteLength = byteCount
        compressed.packetCount = packetCount
        if let descriptions, let target = compressed.packetDescriptions {
            target.update(from: descriptions, count: Int(packetCount))
        }

        let framesPerPacket = max(input.streamDescription.pointee.mFramesPerPacket, 1152)
        guard let pcm = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: packetCount * framesPerPacket) else { return }
        var provided = false
        var error: NSError?
        let status = converter.convert(to: pcm, error: &error) { _, inputStatus in
            if provided {
                inputStatus.pointee = .noDataNow
                return nil
            }
            provided = true
            inputStatus.pointee = .haveData
            return compressed
        }
        guard status != .error, pcm.frameLength > 0 else { return }

        let frames = Int64(pcm.frameLength)
        lock.lock(); queuedFrames += frames; lock.unlock()
        node.scheduleBuffer(pcm, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            self.lock.lock(); self.queuedFrames -= frames; self.lock.unlock()
        }
        if !announced {
            announced = true
            DispatchQueue.main.async { if !self.isStopped { self.onEvent(.playing) } }
        }
    }
}

private func streamPropertyListener(_ client: UnsafeMutableRawPointer, _ stream: AudioFileStreamID,
                                    _ id: AudioFileStreamPropertyID,
                                    _ flags: UnsafeMutablePointer<AudioFileStreamPropertyFlags>) {
    Unmanaged<StreamPlayer>.fromOpaque(client).takeUnretainedValue().streamProperty(stream, id)
}

private func streamPacketsCallback(_ client: UnsafeMutableRawPointer, _ byteCount: UInt32, _ packetCount: UInt32,
                                   _ data: UnsafeRawPointer,
                                   _ descriptions: UnsafeMutablePointer<AudioStreamPacketDescription>?) {
    Unmanaged<StreamPlayer>.fromOpaque(client).takeUnretainedValue()
        .streamPackets(byteCount, packetCount, data, descriptions)
}
