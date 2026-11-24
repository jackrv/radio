import Foundation

enum Receiver: String { case mac, pi }

func response(_ session: String?, _ ids: [Int], base: Double = 1000) -> EventsResponse {
    var json: [String: Any] = ["clips": ids.map { ["id": $0, "at": base + Double($0), "caption": "Шапка\n\nфраза \($0)", "duration_s": 1.0] }]
    if let session { json["session"] = session }
    return try! JSONDecoder().decode(EventsResponse.self, from: JSONSerialization.data(withJSONObject: json))
}
func check(_ ok: Bool, _ name: String) { print(ok ? "ok \(name)" : "FAIL \(name)") }

@main struct Main {
    static func main() {
        check(TextSource.same.resolve(air: .pi) == .pi && TextSource.mac.resolve(air: .pi) == .mac
              && TextSource.pi.resolve(air: .mac) == .pi, "resolve")

        var feed = ClipFeed()
        check(feed.merge(response("a", [1, 2])) == .quiet && feed.cursor == 2 && feed.clips.map(\.number) == [2, 1], "first load is silent")
        if case .added(let fresh) = feed.merge(response("a", [3])) { check(fresh.map(\.number) == [3], "new phrase is announced") }
        else { check(false, "new phrase is announced") }
        check(feed.merge(response("a", [])) == .quiet && feed.clips.count == 3, "empty answer keeps list")
        check(feed.merge(response("a", [3])) == .quiet && feed.clips.count == 3, "repeat is not duplicated")

        let old = feed.clips[0]
        check(feed.canReplay(old), "current session can replay")
        check(feed.merge(response("b", [])) == .restart && feed.cursor == 0, "server restart resets cursor")
        if case .added(let fresh) = feed.merge(response("b", [1], base: 5000)) { check(fresh.count == 1 && feed.cursor == 1, "phrases after restart are seen") }
        else { check(false, "phrases after restart are seen") }
        check(feed.clips.count == 4 && Set(feed.clips.map(\.id)).count == 4 && feed.clips[0].session == "b", "same number, different phrase")
        check(!feed.canReplay(old) && feed.canReplay(feed.clips[0]), "old session cannot replay")

        var legacy = ClipFeed()
        _ = legacy.merge(response(nil, [1]))
        if case .added = legacy.merge(response(nil, [2])) { check(legacy.cursor == 2, "server without session works") }
        else { check(false, "server without session works") }

        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let cache = ClipCache(folder: folder)
        check(cache.load("pi").isEmpty, "empty cache")
        cache.save(feed.clips, "pi")
        check(cache.load("pi") == feed.clips && cache.load("mac").isEmpty, "cache per source")
        var offline = ClipFeed(cached: cache.load("pi"))
        check(offline.clips.count == 4 && offline.cursor == 0 && !offline.canReplay(offline.clips[0]), "cache is shown while source is off")
        check(offline.merge(response("b", [1], base: 5000)) == .quiet && offline.clips.count == 4, "cached phrase is not announced again")
        try? Data("{".utf8).write(to: folder.appendingPathComponent("clips-pi.json"))
        check(cache.load("pi").isEmpty, "damaged cache is ignored")

        var many = ClipFeed()
        _ = many.merge(response("a", Array(1...100)))
        check(many.clips.count == ClipFeed.limit && many.clips[0].number == 100 && many.cursor == 100, "limit keeps newest")

        check(SourceState(status: 200) == .ok && SourceState(status: 403) == .forbidden && SourceState(status: 426) == .outdated
              && SourceState(status: nil) == .offline && SourceState(status: 500) == .offline, "states")
    }
}
