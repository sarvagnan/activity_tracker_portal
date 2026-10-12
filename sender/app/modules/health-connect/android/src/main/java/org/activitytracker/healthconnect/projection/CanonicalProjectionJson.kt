package org.activitytracker.healthconnect.projection

import java.math.BigDecimal
import java.math.BigInteger
import java.math.MathContext
import java.math.RoundingMode
import java.time.Duration
import java.time.Instant
import java.time.ZoneOffset
import kotlin.math.abs

/** Internal control flow contains only the health-data-free refusal identity. */
internal class ProjectionFailure(val failure: String, val path: String) :
    RuntimeException(null, null, false, false)

/**
 * Each field is validated immediately, in declaration order. The stored fragments
 * contain no nulls and need only key sorting when the object is finished.
 */
internal class ProjectionObject(private val path: String) {
    private val fields = mutableMapOf<String, String>()

    fun <T : Any> field(name: String, value: T?, encode: (T, String) -> String) {
        if (value != null) {
            val fieldPath = "$path/${name.replace("~", "~0").replace("/", "~1")}"
            fields[name] = encode(value, fieldPath)
        }
    }

    fun finish(): String = fields.keys.sorted().joinToString(
        separator = ",", prefix = "{", postfix = "}",
    ) { name -> text(name, path) + ":" + fields.getValue(name) }
    // JVM String comparison is lexicographic over UTF-16 code units, as RFC 8785 requires.
}

internal fun jsonObject(path: String, fields: ProjectionObject.() -> Unit): String =
    ProjectionObject(path).apply(fields).finish()

internal fun <T> array(values: List<T>, path: String, encode: (T, String) -> String): String =
    values.mapIndexed { index, value -> encode(value, "$path/$index") }
        .joinToString(separator = ",", prefix = "[", postfix = "]")

/** RFC 8785 string escaping; paired surrogates survive to the final UTF-8 encoding. */
internal fun text(value: String, path: String): String = buildString {
    append('"')
    var index = 0
    while (index < value.length) {
        val character = value[index]
        when (character) {
            '"' -> append("\\\"")
            '\\' -> append("\\\\")
            '\b' -> append("\\b")
            '\u000c' -> append("\\f")
            '\n' -> append("\\n")
            '\r' -> append("\\r")
            '\t' -> append("\\t")
            else -> when {
                character.code < 0x20 -> {
                    append("\\u00")
                    append("0123456789abcdef"[character.code ushr 4])
                    append("0123456789abcdef"[character.code and 15])
                }
                character in '\uD800'..'\uDBFF' -> {
                    if (index + 1 >= value.length || value[index + 1] !in '\uDC00'..'\uDFFF') {
                        throw ProjectionFailure("unrepresentable-text", path)
                    }
                    append(character)
                    append(value[++index])
                }
                character in '\uDC00'..'\uDFFF' ->
                    throw ProjectionFailure("unrepresentable-text", path)
                else -> append(character)
            }
        }
        index++
    }
    append('"')
}

internal fun instant(value: Instant, path: String): String {
    // Bounds are 0000-01-01T00:00:00Z inclusive and 10000-01-01T00:00:00Z exclusive.
    if (value.epochSecond < -62167219200L || value.epochSecond >= 253402300800L) {
        throw ProjectionFailure("unrepresentable-instant", path)
    }
    // Instant's ISO formatter is locale and zone independent, with 0/3/6/9 fraction digits.
    return text(value.toString(), path)
}

internal fun offset(value: ZoneOffset, path: String): String {
    val seconds = value.totalSeconds
    if (seconds % 60 != 0) throw ProjectionFailure("unrepresentable-offset", path)
    val minutes = abs(seconds) / 60
    val sign = if (seconds < 0) "-" else "+"
    val hours = (minutes / 60).toString().padStart(2, '0')
    val remainder = (minutes % 60).toString().padStart(2, '0')
    return text("$sign$hours:$remainder", path)
}

internal fun duration(value: Duration): String {
    val nanos = BigInteger.valueOf(value.seconds).multiply(BigInteger.valueOf(1000000000L))
        .add(BigInteger.valueOf(value.nano.toLong()))
    return "\"$nanos\""
}

/**
 * ECMAScript Number::toString, including shortest round-trip digits. BigDecimal's
 * Double constructor starts from the exact binary value, not Java's formatted
 * decimal. At each precision HALF_EVEN chooses the closest decimal (and the even
 * significand at an exact tie). If that candidate fails, test its floor and
 * ceiling as well: a power-of-two double has an asymmetric rounding interval,
 * so a farther decimal on its wider side can be the shortest valid candidate.
 * The first precision with a round trip is minimal; 17 digits always suffice.
 */
internal fun number(value: Double, path: String): String {
    if (!value.isFinite()) throw ProjectionFailure("non-finite-value", path)
    if (value == 0.0) return "0"
    val magnitude = abs(value)
    val exact = BigDecimal(magnitude)
    var shortest: BigDecimal? = null
    for (precision in 1..17) {
        val candidate = exact.round(MathContext(precision, RoundingMode.HALF_EVEN))
        if (candidate.toDouble() == magnitude) {
            shortest = candidate.stripTrailingZeros()
            break
        }
        val lower = exact.round(MathContext(precision, RoundingMode.FLOOR))
        val upper = exact.round(MathContext(precision, RoundingMode.CEILING))
        // When the closest point failed, at most one of its two neighbors can
        // lie in the contiguous interval of decimals that round to this double.
        val neighbor = when {
            lower.toDouble() == magnitude -> lower
            upper.toDouble() == magnitude -> upper
            else -> null
        }
        if (neighbor != null) {
            shortest = neighbor.stripTrailingZeros()
            break
        }
    }
    val decimal = checkNotNull(shortest) { "A finite double must round trip within 17 digits" }
    val digits = decimal.unscaledValue().toString()
    val k = digits.length
    val n = k - decimal.scale()
    val encoded = when {
        k <= n && n <= 21 -> digits + "0".repeat(n - k)
        0 < n && n <= 21 -> digits.substring(0, n) + "." + digits.substring(n)
        -6 < n && n <= 0 -> "0." + "0".repeat(-n) + digits
        else -> {
            val exponent = n - 1
            val mantissa = if (k == 1) digits else digits.substring(0, 1) + "." + digits.substring(1)
            mantissa + "e" + (if (exponent >= 0) "+" else "-") + abs(exponent).toString()
        }
    }
    return if (value < 0.0) "-$encoded" else encoded
}
