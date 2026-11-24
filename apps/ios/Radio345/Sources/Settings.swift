import Foundation
import Security

/// Куди підключатися: приймач на Mac чи радіовузол на Raspberry Pi.
enum Receiver: String, CaseIterable, Identifiable {
    case mac, pi
    var id: String { rawValue }
    var title: String { self == .mac ? "Mac" : "Pi" }
}

/// Адреси Mac і Pi у Tailscale (UserDefaults). Токена немає: сервер пускає лише
/// пристрої вашого облікового запису Tailscale.
@Observable
final class ConnectionSettings {
    private static let receiverKey = "radio345.receiver"
    private static let channelKey = "radio345.channel"

    var receiver: Receiver { didSet { UserDefaults.standard.set(receiver.rawValue, forKey: Self.receiverKey) } }
    var macHost: String { didSet { UserDefaults.standard.set(macHost, forKey: "radio345.host") } }
    var piHost: String { didSet { UserDefaults.standard.set(piHost, forKey: "radio345.piHost") } }
    /// Тексти й «Відібрані» — з цього вузла; ефір і передача — завжди з `receiver`.
    var textSource: TextSource { didSet { UserDefaults.standard.set(textSource.rawValue, forKey: "radio345.textSource") } }
    var channel: StreamChannel {
        didSet { UserDefaults.standard.set(channel.rawValue, forKey: Self.channelKey) }
    }

    /// Канал «наживо» без звуків сповіщень (+ затримка початку передачі ~2–3 с).
    var filterSignals: Bool { didSet { UserDefaults.standard.set(filterSignals, forKey: "radio345.filterSignals") } }
    /// Вмикати «Вдома», коли iPhone повертається в мережу приймача.
    var autoHome: Bool { didSet { UserDefaults.standard.set(autoHome, forKey: "radio345.autoHome") } }

    init() {
        let defaults = UserDefaults.standard
        filterSignals = defaults.bool(forKey: "radio345.filterSignals")
        autoHome = defaults.object(forKey: "radio345.autoHome") as? Bool ?? true
        receiver = Receiver(rawValue: defaults.string(forKey: Self.receiverKey) ?? "") ?? .mac
        textSource = TextSource(rawValue: defaults.string(forKey: "radio345.textSource") ?? "") ?? .same
        macHost = defaults.string(forKey: "radio345.host") ?? ""
        piHost = defaults.string(forKey: "radio345.piHost") ?? ""
        channel = StreamChannel(rawValue: defaults.string(forKey: Self.channelKey) ?? "") ?? .selected
        // Старий токен із Keychain більше не потрібен.
        SecItemDelete([kSecClass as String: kSecClassGenericPassword,
                       kSecAttrAccount as String: "radio345.streamToken"] as CFDictionary)
    }

    /// Вузол текстів.
    var textReceiver: Receiver { textSource.resolve(air: receiver) }
    /// Чи це два різні вузли (інакше досить одного опитування).
    var hasSeparateTexts: Bool { textReceiver != receiver }
    /// Звідки звук обраного каналу: «Наживо» — з ефіру, «Відібрані» — з вузла текстів.
    var streamReceiver: Receiver { channel == .live ? receiver : textReceiver }

    func host(_ source: Receiver) -> String { source == .mac ? macHost : piHost }

    var isConfigured: Bool { baseURL(receiver) != nil }

    /// `http://radio345:8346` — без шляху; порт типово 8346.
    func baseURL(_ source: Receiver) -> URL? {
        var text = host(source).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") { text = "http://" + text }
        guard var parts = URLComponents(string: text), parts.host?.isEmpty == false else { return nil }
        if parts.port == nil { parts.port = 8346 }
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        return parts.url
    }

    func url(_ path: String, from source: Receiver, query: [URLQueryItem] = []) -> URL? {
        guard let base = baseURL(source), var parts = URLComponents(url: base, resolvingAgainstBaseURL: false) else { return nil }
        parts.path = path
        parts.queryItems = query.isEmpty ? nil : query
        return parts.url
    }
}

enum StreamChannel: String, CaseIterable, Identifiable {
    case selected, live
    var id: String { rawValue }
    var title: String { self == .selected ? "Відібрані" : "Наживо" }
    func path(filterSignals: Bool) -> String {
        switch self {
        case .selected: "/stream.mp3"
        case .live: filterSignals ? "/live-clean.mp3" : "/live.mp3"
        }
    }
    var hint: String {
        self == .selected
            ? "Фрази за режимом Telegram, після розпізнавання"
            : "Усе мовлення одразу, без відбору за текстом"
    }
}
