import Foundation
import Security

/// Адреса Mac у Tailscale і токен потоку. Токен — у Keychain, адреса — в UserDefaults.
@Observable
final class ConnectionSettings {
    private static let hostKey = "radio345.host"
    private static let channelKey = "radio345.channel"
    private static let tokenAccount = "radio345.streamToken"

    var host: String { didSet { UserDefaults.standard.set(host, forKey: Self.hostKey) } }
    var token: String { didSet { Keychain.save(token, account: Self.tokenAccount) } }
    var channel: StreamChannel {
        didSet { UserDefaults.standard.set(channel.rawValue, forKey: Self.channelKey) }
    }

    /// Канал «наживо» без звуків сповіщень (+ затримка початку передачі ~2–3 с).
    var filterSignals: Bool { didSet { UserDefaults.standard.set(filterSignals, forKey: "radio345.filterSignals") } }
    /// Вмикати «Вдома», коли iPhone повертається в мережу Mac.
    var autoHome: Bool { didSet { UserDefaults.standard.set(autoHome, forKey: "radio345.autoHome") } }

    init() {
        filterSignals = UserDefaults.standard.bool(forKey: "radio345.filterSignals")
        autoHome = UserDefaults.standard.object(forKey: "radio345.autoHome") as? Bool ?? true
        host = UserDefaults.standard.string(forKey: Self.hostKey) ?? ""
        token = Keychain.load(account: Self.tokenAccount) ?? ""
        channel = StreamChannel(rawValue: UserDefaults.standard.string(forKey: Self.channelKey) ?? "") ?? .selected
    }

    var isConfigured: Bool { baseURL != nil && !token.isEmpty }

    /// `http://100.x.y.z:8346` — без шляху; порт типово 8346.
    var baseURL: URL? {
        var text = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") { text = "http://" + text }
        guard var parts = URLComponents(string: text), parts.host?.isEmpty == false else { return nil }
        if parts.port == nil { parts.port = 8346 }
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        return parts.url
    }

    func url(_ path: String, query: [URLQueryItem] = []) -> URL? {
        guard let base = baseURL, var parts = URLComponents(url: base, resolvingAgainstBaseURL: false) else { return nil }
        parts.path = path
        parts.queryItems = [URLQueryItem(name: "t", value: token)] + query
        return parts.url
    }

    /// Приймає посилання зі сторінки (`http://100.x:8346/#t=…`) або `radio345://…`.
    @discardableResult
    func apply(link: String) -> Bool {
        guard var parts = URLComponents(string: link.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
        let fragment = parts.fragment ?? ""
        let fromFragment = fragment.split(separator: "&").first { $0.hasPrefix("t=") }.map { String($0.dropFirst(2)) }
        let fromQuery = parts.queryItems?.first { $0.name == "t" }?.value
        guard let newToken = (fromFragment ?? fromQuery)?.removingPercentEncoding, !newToken.isEmpty else { return false }
        if parts.scheme == "radio345" { parts.scheme = "http" }
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        guard let hostPart = parts.host, !hostPart.isEmpty else { return false }
        host = parts.port.map { "\(hostPart):\($0)" } ?? hostPart
        token = newToken
        return true
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

enum Keychain {
    static func save(_ value: String, account: String) {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account]
        SecItemDelete(query as CFDictionary)
        guard !value.isEmpty else { return }
        var add = query
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }

    static func load(account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account,
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
