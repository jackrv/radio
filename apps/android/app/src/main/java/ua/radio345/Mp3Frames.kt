package ua.radio345

/**
 * Розбиває безперервний MP3-потік на кадри (Layer III) для MediaCodec.
 * Синхронізація: заголовок вважається справжнім, лише якщо одразу за кадром
 * іде ще один коректний заголовок — так випадкові 0xFFE у даних не збивають розбір.
 */
class Mp3Frames {
    data class Header(val sampleRate: Int, val channels: Int, val frameLength: Int, val samplesPerFrame: Int)

    private var buffer = ByteArray(16 * 1024)
    private var size = 0
    private var synced = false

    /** Додає байти й повертає всі повні кадри. */
    fun push(data: ByteArray, count: Int = data.size): List<Pair<Header, ByteArray>> {
        if (size + count > buffer.size) buffer = buffer.copyOf(maxOf(buffer.size * 2, size + count))
        System.arraycopy(data, 0, buffer, size, count)
        size += count

        val frames = ArrayList<Pair<Header, ByteArray>>()
        var pos = 0
        while (size - pos >= 4) {
            val header = parse(buffer, pos)
            if (header == null) {
                synced = false
                pos++
                continue
            }
            if (size - pos < header.frameLength) break
            if (!synced) {
                // Потрібне підтвердження наступним заголовком.
                val next = pos + header.frameLength
                if (size - next < 4) break
                if (parse(buffer, next) == null) { pos++; continue }
                synced = true
            }
            frames += header to buffer.copyOfRange(pos, pos + header.frameLength)
            pos += header.frameLength
        }
        if (pos > 0) {
            System.arraycopy(buffer, pos, buffer, 0, size - pos)
            size -= pos
        }
        // Сміття без синхронізації не накопичується.
        if (size > 64 * 1024) size = 0
        return frames
    }

    companion object {
        private val BITRATE_V1 = intArrayOf(0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        private val BITRATE_V2 = intArrayOf(0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
        private val RATES = mapOf(3 to intArrayOf(44100, 48000, 32000), 2 to intArrayOf(22050, 24000, 16000),
            0 to intArrayOf(11025, 12000, 8000))

        fun parse(b: ByteArray, at: Int): Header? {
            val b0 = b[at].toInt() and 0xFF
            val b1 = b[at + 1].toInt() and 0xFF
            val b2 = b[at + 2].toInt() and 0xFF
            val b3 = b[at + 3].toInt() and 0xFF
            if (b0 != 0xFF || (b1 and 0xE0) != 0xE0) return null
            val version = (b1 shr 3) and 3          // 3 — MPEG1, 2 — MPEG2, 0 — MPEG2.5
            val layer = (b1 shr 1) and 3            // 1 — Layer III
            if (version == 1 || layer != 1) return null
            val bitrateIndex = (b2 shr 4) and 0xF
            val rateIndex = (b2 shr 2) and 3
            if (bitrateIndex == 0 || bitrateIndex == 15 || rateIndex == 3) return null
            val padding = (b2 shr 1) and 1
            val mpeg1 = version == 3
            val bitrate = (if (mpeg1) BITRATE_V1 else BITRATE_V2)[bitrateIndex] * 1000
            val rate = RATES.getValue(version)[rateIndex]
            val length = (if (mpeg1) 144 else 72) * bitrate / rate + padding
            val channels = if ((b3 shr 6) and 3 == 3) 1 else 2
            return Header(rate, channels, length, if (mpeg1) 1152 else 576)
        }
    }
}
