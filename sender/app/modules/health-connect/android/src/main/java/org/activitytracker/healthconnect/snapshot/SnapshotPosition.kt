package org.activitytracker.healthconnect.snapshot

import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction
import java.time.Instant
import java.time.format.DateTimeParseException

/** UTF-8's unsigned lexicographic order, rather than JVM UTF-16 string order. */
internal val SNAPSHOT_UTF8_ORDER = Comparator<String> { left, right ->
    val a = left.toByteArray(Charsets.UTF_8)
    val b = right.toByteArray(Charsets.UTF_8)
    var difference = 0
    var index = 0
    while (difference == 0 && index < minOf(a.size, b.size)) {
        difference = (a[index].toInt() and 255) - (b[index].toInt() and 255)
        index++
    }
    if (difference == 0) a.size.compareTo(b.size) else difference
}

internal class SnapshotPosition(
    val recordType: String,
    val snapshotId: String,
    val boundaryStart: Instant? = null,
    val boundaryIds: List<String> = emptyList(),
) {
    /** These fixed ASCII keys are already in RFC 8785's UTF-16 key order. */
    fun encode(): ByteArray = buildString {
        append('{')
        if (boundaryStart != null) {
            append("\"boundary_record_ids\":[")
            boundaryIds.forEachIndexed { index, id ->
                if (index != 0) append(',')
                appendJsonString(id)
            }
            append("],\"boundary_start\":")
            appendJsonString(boundaryStart.toString())
            append(',')
        }
        append("\"position_version\":1,\"record_type\":")
        appendJsonString(recordType)
        append(",\"snapshot_id\":")
        appendJsonString(snapshotId)
        append('}')
    }.toByteArray(Charsets.UTF_8)
}

/** Decode, validate the closed shape, then compare the exact canonical bytes. */
internal fun parseSnapshotPosition(bytes: ByteArray): SnapshotPosition? {
    val decoded = try {
        Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes)).toString()
    } catch (_: CharacterCodingException) {
        return null
    }
    val parsed = try {
        PositionParser(decoded).parse()
    } catch (_: MalformedPosition) {
        return null
    }
    return parsed.takeIf { bytes.contentEquals(it.encode()) }
}

private class MalformedPosition : RuntimeException(null, null, false, false)

/** Only the closed position shape is accepted; no recursive JSON value parser. */
private class PositionParser(private val input: String) {
    private var index = 0

    fun parse(): SnapshotPosition {
        val keys = HashSet<String>()
        var recordType: String? = null
        var snapshotId: String? = null
        var boundaryStart: Instant? = null
        var boundaryIds: List<String>? = null
        expect('{')
        // Iterative both over fields and over identifiers; depth is constant.
        while (true) {
            val key = string()
            if (!keys.add(key)) malformed()
            expect(':')
            when (key) {
                "position_version" -> expect('1')
                "record_type" -> recordType = string()
                "snapshot_id" -> snapshotId = string()
                "boundary_start" -> boundaryStart = boundaryInstant(string())
                "boundary_record_ids" -> boundaryIds = identifiers()
                else -> malformed()
            }
            if (consume('}')) break
            expect(',')
        }
        whitespace()
        if (index != input.length || "position_version" !in keys || recordType == null || snapshotId == null) {
            malformed()
        }
        if ((boundaryStart == null) != (boundaryIds == null)) malformed()
        return SnapshotPosition(recordType, snapshotId, boundaryStart, boundaryIds ?: emptyList())
    }

    private fun identifiers(): List<String> {
        expect('[')
        val result = ArrayList<String>()
        if (consume(']')) malformed()
        while (true) {
            val id = string()
            if (result.isNotEmpty() && SNAPSHOT_UTF8_ORDER.compare(result.last(), id) >= 0) malformed()
            result.add(id)
            if (consume(']')) return result
            expect(',')
        }
    }

    private fun boundaryInstant(value: String): Instant {
        val instant = try {
            Instant.parse(value)
        } catch (_: DateTimeParseException) {
            malformed()
        }
        // Same 0000..9999 bounds and Instant spelling as project-source-record@1.
        if (instant.epochSecond < -62167219200L || instant.epochSecond >= 253402300800L ||
            instant.toString() != value
        ) malformed()
        return instant
    }

    private fun string(): String {
        expect('"')
        val value = StringBuilder()
        while (index < input.length) {
            val character = input[index++]
            when {
                character == '"' -> {
                    val result = value.toString()
                    if (!validUnicode(result)) malformed()
                    return result
                }
                character == '\\' -> {
                    if (index == input.length) malformed()
                    value.append(when (input[index++]) {
                        '"' -> '"'
                        '\\' -> '\\'
                        '/' -> '/'
                        'b' -> '\b'
                        'f' -> '\u000c'
                        'n' -> '\n'
                        'r' -> '\r'
                        't' -> '\t'
                        'u' -> {
                            var code = 0
                            repeat(4) {
                                if (index == input.length) malformed()
                                val digit = input[index++].digitToIntOrNull(16) ?: malformed()
                                code = code * 16 + digit
                            }
                            code.toChar()
                        }
                        else -> malformed()
                    })
                }
                character.code < 32 -> malformed()
                else -> value.append(character)
            }
        }
        malformed()
    }

    private fun whitespace() {
        while (index < input.length && input[index] in " \t\r\n") index++
    }

    private fun consume(character: Char): Boolean {
        whitespace()
        if (index == input.length || input[index] != character) return false
        index++
        return true
    }

    private fun expect(character: Char) {
        if (!consume(character)) malformed()
    }

    private fun malformed(): Nothing = throw MalformedPosition()
}

private fun validUnicode(value: String): Boolean {
    var index = 0
    while (index < value.length) {
        val character = value[index++]
        when (character) {
            in '\uD800'..'\uDBFF' -> {
                if (index == value.length || value[index++] !in '\uDC00'..'\uDFFF') return false
            }
            in '\uDC00'..'\uDFFF' -> return false
        }
    }
    return true
}

/** ECMAScript JSON.stringify escaping for valid Unicode strings (RFC 8785). */
private fun StringBuilder.appendJsonString(value: String) {
    check(validUnicode(value)) { "Invalid Unicode in snapshot position" }
    append('"')
    for (character in value) {
        when (character) {
            '"' -> append("\\\"")
            '\\' -> append("\\\\")
            '\b' -> append("\\b")
            '\u000c' -> append("\\f")
            '\n' -> append("\\n")
            '\r' -> append("\\r")
            '\t' -> append("\\t")
            else -> if (character.code < 32) {
                append("\\u00")
                append("0123456789abcdef"[character.code ushr 4])
                append("0123456789abcdef"[character.code and 15])
            } else append(character)
        }
    }
    append('"')
}
