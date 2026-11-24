import Foundation

// Лише Foundation: перевіряється на Mac без симулятора (tests/test_ios_feeds.py).

struct Clip: Codable, Identifiable, Equatable {
    /// Номер на сервері; після його перезапуску нумерація починається знову.
    let number: Int
    /// Сеанс сервера ("" — старий сервер без сеансу).
    var session: String
    let at: Double
    let caption: String
    let duration_s: Double

    var id: String { session + "#" + String(number) }
    var date: Date { Date(timeIntervalSince1970: at) }
    /// Останній абзац підпису — сам текст фрази (шапка «Подія / Кому» вище).
    var text: String { caption.components(separatedBy: "\n\n").last ?? caption }
    var header: String { caption.components(separatedBy: "\n\n").first ?? "" }
}

struct EventsResponse: Decodable {
    struct Item: Decodable {
        let id: Int
        let at: Double
        let caption: String
        let duration_s: Double
    }
    let clips: [Item]
    let mode: String?
    let at_home: Bool?
    let transmit: Bool?
    let session: String?
}

/// Звідки тексти: звідти ж, звідки ефір (типово), або з іншого вузла.
enum TextSource: String, CaseIterable, Identifiable {
    case same, mac, pi
    var id: String { rawValue }
    var title: String { self == .same ? "Як ефір" : (self == .mac ? "Mac" : "Pi") }
    func resolve(air: Receiver) -> Receiver {
        switch self {
        case .same: air
        case .mac: .mac
        case .pi: .pi
        }
    }
}

/// Список фраз одного джерела: курсор, сеанс сервера й кеш. Джерело зникло — список лишається.
struct ClipFeed: Equatable {
    static let limit = 30
    private(set) var clips: [Clip] = []      // нові зверху
    private(set) var session: String?        // nil — цього запуску сервер ще не відповідав
    private(set) var cursor = 0
    private var primed = false

    init(cached: [Clip] = []) { clips = Array(cached.prefix(Self.limit)) }

    enum Outcome: Equatable {
        case added([Clip])    // нові фрази для сповіщень
        case quiet            // нічого нового, перше завантаження або лише кеш
        case restart          // сервер перезапустився: запитайте ще раз з курсора 0
    }

    mutating func merge(_ response: EventsResponse) -> Outcome {
        let current = response.session ?? ""
        if let known = session, known != current {
            session = current
            cursor = 0
            return .restart   // відповідь рахувалася від старого курсора
        }
        session = current
        let first = !primed
        primed = true
        let incoming = response.clips.map {
            Clip(number: $0.id, session: current, at: $0.at, caption: $0.caption, duration_s: $0.duration_s)
        }
        guard let last = incoming.last else { return .quiet }
        cursor = max(cursor, last.number)
        let seen = Set(clips.map(\.id))
        let fresh = incoming.filter { !seen.contains($0.id) }
        guard !fresh.isEmpty else { return .quiet }
        clips = Array((fresh + clips).sorted { $0.at > $1.at }.prefix(Self.limit))
        return first ? .quiet : .added(fresh)
    }

    /// Звук фрази є лише на сервері поточного сеансу.
    func canReplay(_ clip: Clip) -> Bool { session != nil && clip.session == session }
}

/// Кеш текстів на диску: видно й тоді, коли джерело вимкнене.
struct ClipCache {
    let folder: URL

    private func file(_ key: String) -> URL { folder.appendingPathComponent("clips-\(key).json") }

    func load(_ key: String) -> [Clip] {
        guard let data = try? Data(contentsOf: file(key)) else { return [] }
        return (try? JSONDecoder().decode([Clip].self, from: data)) ?? []
    }

    func save(_ clips: [Clip], _ key: String) {
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        guard let data = try? JSONEncoder().encode(clips) else { return }
        #if os(iOS)
        try? data.write(to: file(key), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try? data.write(to: file(key), options: .atomic)
        #endif
    }
}

/// Стан одного джерела: помилка одного не змінює іншого.
enum SourceState: Equatable {
    case unknown, ok, offline, forbidden, outdated

    init(status: Int?) {
        switch status {
        case 200: self = .ok
        case 403: self = .forbidden
        case 426: self = .outdated
        default: self = .offline
        }
    }

    func text(_ name: String) -> String {
        switch self {
        case .unknown: "\(name): немає даних"
        case .ok: "\(name) на зв'язку"
        case .offline: "\(name) недоступний · перевірте Tailscale"
        case .forbidden: "\(name): доступ лише з вашого облікового запису Tailscale"
        case .outdated: "\(name): оновіть застосунок"
        }
    }
}
