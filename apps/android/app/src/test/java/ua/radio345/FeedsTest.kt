package ua.radio345

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** Ті самі перевірки, що tests/ios_feeds_check.swift для iOS; тексти синтетичні. */
class FeedsTest {
    private fun response(session: String?, ids: List<Int>, base: Double = 1000.0): EventsResponse {
        val json = JSONObject()
        json.put("clips", JSONArray(ids.map {
            JSONObject().put("id", it).put("at", base + it).put("caption", "Шапка\n\nфраза $it").put("duration_s", 1.0)
        }))
        if (session != null) json.put("session", session)
        return EventsResponse.parse(json.toString())!!
    }

    @Test fun resolve() {
        assertEquals(Receiver.PI, TextSource.SAME.resolve(Receiver.PI))
        assertEquals(Receiver.MAC, TextSource.MAC.resolve(Receiver.PI))
        assertEquals(Receiver.PI, TextSource.PI.resolve(Receiver.MAC))
    }

    @Test fun feedLifecycle() {
        val feed = ClipFeed()
        assertEquals(ClipFeed.Outcome.Quiet, feed.merge(response("a", listOf(1, 2))))   // перше завантаження мовчить
        assertEquals(2, feed.cursor)
        assertEquals(listOf(2, 1), feed.clips.map { it.number })
        assertEquals(listOf(3), (feed.merge(response("a", listOf(3))) as ClipFeed.Outcome.Added).clips.map { it.number })
        assertEquals(ClipFeed.Outcome.Quiet, feed.merge(response("a", emptyList())))
        assertEquals(3, feed.clips.size)
        assertEquals(ClipFeed.Outcome.Quiet, feed.merge(response("a", listOf(3))))      // повтор не дублюється
        assertEquals(3, feed.clips.size)

        val old = feed.clips[0]
        assertTrue(feed.canReplay(old))
        assertEquals(ClipFeed.Outcome.Restart, feed.merge(response("b", emptyList())))  // сервер перезапустився
        assertEquals(0, feed.cursor)
        val fresh = feed.merge(response("b", listOf(1), base = 5000.0)) as ClipFeed.Outcome.Added
        assertEquals(1, fresh.clips.size)
        assertEquals(1, feed.cursor)
        assertEquals(4, feed.clips.map { it.id }.toSet().size)                          // той самий номер — інша фраза
        assertEquals("b", feed.clips[0].session)
        assertFalse(feed.canReplay(old))
        assertTrue(feed.canReplay(feed.clips[0]))

        val folder = Files.createTempDirectory("clips").toFile()
        try {
            val cache = ClipCache(folder)
            assertTrue(cache.load("pi").isEmpty())
            cache.save(feed.clips, "pi")
            assertEquals(feed.clips, cache.load("pi"))
            assertTrue(cache.load("mac").isEmpty())
            val offline = ClipFeed(cache.load("pi"))
            assertEquals(4, offline.clips.size)
            assertEquals(0, offline.cursor)
            assertFalse(offline.canReplay(offline.clips[0]))
            assertEquals(ClipFeed.Outcome.Quiet, offline.merge(response("b", listOf(1), base = 5000.0)))
            assertEquals(4, offline.clips.size)
            File(folder, "clips-pi.json").writeText("{")
            assertTrue(cache.load("pi").isEmpty())                                      // пошкоджений кеш
        } finally {
            folder.deleteRecursively()
        }
    }

    @Test fun serverWithoutSession() {
        val legacy = ClipFeed()
        legacy.merge(response(null, listOf(1)))
        assertTrue(legacy.merge(response(null, listOf(2))) is ClipFeed.Outcome.Added)
        assertEquals(2, legacy.cursor)
    }

    @Test fun limitKeepsNewest() {
        val many = ClipFeed()
        many.merge(response("a", (1..100).toList()))
        assertEquals(ClipFeed.LIMIT, many.clips.size)
        assertEquals(100, many.clips[0].number)
        assertEquals(100, many.cursor)
    }

    @Test fun states() {
        assertEquals(SourceState.OK, SourceState.from(200))
        assertEquals(SourceState.FORBIDDEN, SourceState.from(403))
        assertEquals(SourceState.OUTDATED, SourceState.from(426))
        assertEquals(SourceState.OFFLINE, SourceState.from(null))
        assertEquals(SourceState.OFFLINE, SourceState.from(500))
    }

    @Test fun responseFields() {
        val r = EventsResponse.parse("""{"clips":[],"at_home":true,"transmit":false,"mode":"watch"}""")!!
        assertEquals(true, r.atHome)
        assertEquals(false, r.transmit)
        assertNull(r.session)
        assertNull(EventsResponse.parse("not json"))
    }

    @Test fun addresses() {
        assertEquals("http://radio345:8346", baseUrl(" radio345 "))
        assertEquals("http://100.64.0.1:9000", baseUrl("100.64.0.1:9000"))
        assertEquals("https://node.example:8346", baseUrl("https://node.example/path?x=1"))
        assertNull(baseUrl(""))
        assertNull(baseUrl("http://"))
        assertEquals("http://n:8346/events?after=3&api=1", buildUrl("http://n:8346", "/events", listOf("after" to "3", "api" to "1")))
        assertEquals("ws://n:8346/transmit", buildUrl("http://n:8346", "/transmit").replaceFirst(Regex("^http"), "ws"))
    }

    @Test fun clipText() {
        val clip = Clip(1, "a", 0.0, "Подія\nКому\n\nтекст", 1.0)
        assertEquals("текст", clip.text)
        assertEquals("Подія\nКому", clip.header)
        assertEquals("тільки текст", Clip(1, "a", 0.0, "тільки текст", 1.0).text)
    }

    /** MPEG1 Layer III, 128 кбіт/с, 44,1 кГц, стерео: кадр 417 байтів. */
    private fun frame(fill: Byte = 0x11): ByteArray =
        ByteArray(417) { fill }.also { it[0] = 0xFF.toByte(); it[1] = 0xFB.toByte(); it[2] = 0x90.toByte(); it[3] = 0x00 }

    @Test fun mp3Frames() {
        val header = Mp3Frames.parse(frame(), 0)!!
        assertEquals(44100, header.sampleRate)
        assertEquals(417, header.frameLength)
        assertEquals(2, header.channels)
        // MPEG2, 32 кбіт/с, 16 кГц, моно: 72 * 32000 / 16000 = 144.
        val mono = Mp3Frames.parse(byteArrayOf(0xFF.toByte(), 0xF3.toByte(), 0x48, 0xC0.toByte()), 0)!!
        assertEquals(16000, mono.sampleRate)
        assertEquals(144, mono.frameLength)
        assertEquals(1, mono.channels)

        val stream = byteArrayOf(1, 2, 0xFF.toByte(), 3) + frame() + frame() + frame()
        val parser = Mp3Frames()
        val out = ArrayList<ByteArray>()
        // По шматках різного розміру: кадри не залежать від меж читання мережі.
        var pos = 0
        for (size in listOf(5, 300, 1, 600, 1000)) {
            val end = minOf(stream.size, pos + size)
            if (pos >= end) break
            out += parser.push(stream.copyOfRange(pos, end)).map { it.second }
            pos = end
        }
        // Останній кадр чекає наступного заголовка лише до синхронізації; після неї віддається одразу.
        assertEquals(3, out.size)
        assertTrue(out.all { it.size == 417 && it[0] == 0xFF.toByte() })
    }
}
