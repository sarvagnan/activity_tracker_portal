package org.activitytracker.healthconnect.projection

import androidx.health.connect.client.records.ExerciseCompletionGoal
import androidx.health.connect.client.records.ExercisePerformanceTarget
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.units.BloodGlucose
import androidx.health.connect.client.units.Energy
import androidx.health.connect.client.units.Length
import androidx.health.connect.client.units.Mass
import androidx.health.connect.client.units.Percentage
import androidx.health.connect.client.units.Power
import androidx.health.connect.client.units.Pressure
import androidx.health.connect.client.units.Temperature
import androidx.health.connect.client.units.TemperatureDelta
import androidx.health.connect.client.units.Velocity
import androidx.health.connect.client.units.Volume
import java.io.File
import java.time.Duration
import java.time.Instant
import java.time.ZoneOffset
import kotlin.reflect.KClass
import kotlin.reflect.KVisibility
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** The checked-in projection model and synthetic vectors under projection/v1. */
object ProjectionVectors {
    val root: File = generateSequence(File(System.getProperty("user.dir")).absoluteFile) { it.parentFile }
        .first { File(it, "projection/v1/model.json").isFile }
        .resolve("projection/v1")
    val model: JsonObject = read("model.json")
    val positive: List<JsonObject> = read("vectors/positive.json").array("vectors")
    val refusals: List<JsonObject> = read("vectors/refusals.json").array("vectors")

    fun named(name: String): JsonObject = positive.single { it.text("name") == name }

    fun tagged(tag: String): List<JsonObject> =
        positive.filter { vector -> vector["tags"]!!.jsonArray.any { it.jsonPrimitive.content == tag } }
            .also { require(it.isNotEmpty()) { "no vectors tagged $tag" } }

    fun refusals(failure: String): List<JsonObject> =
        refusals.filter { it.text("failure") == failure }.also { require(it.isNotEmpty()) { "no refusals for $failure" } }

    private fun read(path: String): JsonObject = Json.parseToJsonElement(File(root, path).readText()).jsonObject
}

fun JsonObject.text(key: String): String = this[key]!!.jsonPrimitive.content

fun JsonObject.array(key: String): List<JsonObject> = this[key]!!.jsonArray.map { it.jsonObject }

/** A Record subclass the pinned SDK does not define. */
class SyntheticUnknownRecord(override val metadata: Metadata) : Record

/**
 * Builds SDK records from a vector's projection through SDK constructors.
 * [overrides] replaces the value at a JSON pointer with an SDK value that has no
 * JSON form, such as NaN or an SDK Unknown variant.
 */
class RecordBuilder(private val overrides: Map<String, Any?> = emptyMap()) {
    private val model = ProjectionVectors.model

    fun build(vector: JsonObject): Record {
        val projection = vector["projection"]!!.jsonObject
        val spec = model["records"]!!.jsonObject[vector.text("record_type")]!!.jsonObject
        val metadata = buildObject("metadata.Metadata", projection["metadata"]!!.jsonObject, "/metadata") as Metadata
        val values = convertFields(spec["fields"]!!.jsonArray, projection["record"]!!.jsonObject, "/record") + ("metadata" to metadata)
        return construct(sdkClass(spec.text("class")), values) as Record
    }

    private fun convertFields(fields: JsonArray, json: JsonObject, path: String): Map<String, Any?> =
        fields.associate { entry ->
            val field = entry.jsonObject
            val pointer = "$path/${field.text("json")}"
            val value = json[field.text("json")]
            field.text("property") to when {
                pointer in overrides -> overrides[pointer]
                value == null -> null
                else -> convert(field.text("kind"), value, pointer)
            }
        }

    private fun convert(kind: String, value: JsonElement, pointer: String): Any? {
        val primitive = { value.jsonPrimitive.content }
        return when {
            kind == "instant" -> Instant.parse(primitive())
            kind == "offset" -> ZoneOffset.of(primitive())
            kind == "duration" -> primitive().toBigInteger().divideAndRemainder(NANOS_PER_SECOND).let { (seconds, nanos) -> Duration.ofSeconds(seconds.toLong(), nanos.toLong()) }
            kind == "string" -> primitive()
            kind == "bool" -> primitive().toBooleanStrict()
            kind == "int" -> primitive().toInt()
            kind == "long" || kind == "long-string" -> primitive().toLong()
            kind == "double" -> primitive().toDouble()
            kind == "percentage" -> Percentage(primitive().toDouble())
            kind.startsWith("measure:") -> measure(kind.substringAfter(':'), primitive().toDouble())
            kind.startsWith("constant:") -> primitive().toInt()
            kind.startsWith("object:") -> buildObject(kind.substringAfter(':'), value.jsonObject, pointer)
            kind.startsWith("list:") -> value.jsonArray.mapIndexed { index, item ->
                overrides.getOrElse("$pointer/$index") { buildObject(kind.substringAfter(':'), item.jsonObject, "$pointer/$index") }
            }
            kind.startsWith("variant:") -> buildVariant(kind.substringAfter(':'), value.jsonObject, pointer)
            kind.startsWith("list-variant:") -> value.jsonArray.mapIndexed { index, item ->
                overrides.getOrElse("$pointer/$index") { buildVariant(kind.substringAfter(':'), item.jsonObject, "$pointer/$index") }
            }
            else -> error("unknown field kind $kind")
        }
    }

    private fun measure(unit: String, value: Double): Any = when (unit) {
        "BloodGlucose" -> BloodGlucose.millimolesPerLiter(value)
        "Energy" -> Energy.calories(value)
        "Length" -> Length.meters(value)
        "Mass" -> Mass.grams(value)
        "Power" -> Power.watts(value)
        "Pressure" -> Pressure.millimetersOfMercury(value)
        "Temperature" -> Temperature.celsius(value)
        "TemperatureDelta" -> TemperatureDelta.celsius(value)
        "Velocity" -> Velocity.metersPerSecond(value)
        "Volume" -> Volume.liters(value)
        else -> error("unknown unit class $unit")
    }

    private fun buildObject(type: String, json: JsonObject, pointer: String): Any {
        val fields = model["types"]!!.jsonObject[type]!!.jsonArray
        return construct(sdkClass(type), convertFields(fields, json, pointer))
    }

    private fun buildVariant(sealed: String, json: JsonObject, pointer: String): Any {
        val variant = model["variants"]!!.jsonObject[sealed]!!.jsonObject[json.text("kind")]!!.jsonObject
        val type = sdkClass(variant.text("class"))
        type.objectInstance?.let { return it }
        return construct(type, convertFields(variant["fields"]!!.jsonArray, json, pointer))
    }

    /** Calls the non-private constructor covering the most supplied properties; any left over are set on backing fields. */
    private fun construct(type: KClass<*>, values: Map<String, Any?>): Any {
        val supplied = values.filterValues { it != null }.keys
        val constructor = type.constructors
            .filter { it.visibility != KVisibility.PRIVATE }
            .filter { candidate -> candidate.parameters.all { it.isOptional || it.type.isMarkedNullable || it.name in supplied } }
            .maxByOrNull { candidate -> candidate.parameters.count { it.name in supplied } }
            ?: error("no constructor of ${type.simpleName} accepts ${values.keys}")
        val arguments = constructor.parameters
            .filter { parameter -> parameter.name in supplied || (parameter.name in values && !parameter.isOptional) }
            .associateWith { values[it.name] }
        val instance = constructor.callBy(arguments)
        val accepted = constructor.parameters.mapNotNull { it.name }.toSet()
        for ((name, value) in values) {
            if (name !in accepted && value != null) setBackingField(instance, name, value)
        }
        return instance
    }

    private fun setBackingField(instance: Any, name: String, value: Any) {
        val field = generateSequence<Class<*>>(instance.javaClass) { it.superclass }
            .firstNotNullOfOrNull { type -> type.declaredFields.firstOrNull { it.name == name } }
            ?: error("${instance.javaClass.simpleName} has no property $name")
        field.isAccessible = true
        field.set(instance, value)
    }

    companion object {
        fun sdkClass(name: String): KClass<*> = Class.forName("androidx.health.connect.client.records.$name").kotlin

        fun context(vector: JsonObject): ProjectionContext {
            val context = vector["context"]!!.jsonObject
            return ProjectionContext(context.text("implementation_version"), context.text("provider_version"))
        }

        /** The SDK record a refusal vector describes. */
        fun refusalRecord(refusal: JsonObject): Record {
            val base = ProjectionVectors.named(refusal.text("base"))
            val change = refusal["change"]!!.jsonObject
            if (change.text("op") == "custom-record") return SyntheticUnknownRecord(RecordBuilder().build(base).metadata)
            return RecordBuilder(overrides(change, base["projection"]!!.jsonObject)).build(base)
        }

        private val NANOS_PER_SECOND: java.math.BigInteger = java.math.BigInteger.valueOf(1_000_000_000)

        private fun overrides(change: JsonObject, projection: JsonObject): Map<String, Any?> = when (change.text("op")) {
            "multi" -> change["changes"]!!.jsonArray.fold(emptyMap()) { merged, each -> merged + overrides(each.jsonObject, projection) }
            "set-double" -> mapOf(change.text("path") to when (change.text("value")) {
                "NaN" -> Double.NaN
                "Infinity" -> Double.POSITIVE_INFINITY
                "-Infinity" -> Double.NEGATIVE_INFINITY
                else -> change.text("value").toDouble()
            })
            "set-int" -> mapOf(change.text("path") to change.text("value").toInt())
            "set-string-utf16" -> mapOf(
                change.text("path") to String(change["value"]!!.jsonArray.map { it.jsonPrimitive.content.removePrefix("0x").toInt(16).toChar() }.toCharArray()),
            )
            "unknown-variant" -> mapOf(
                change.text("path") to if (change.text("path").endsWith("completion_goal")) ExerciseCompletionGoal.UnknownGoal else ExercisePerformanceTarget.UnknownTarget,
            )
            "shift-years" -> change["paths"]!!.jsonArray.map { it.jsonPrimitive.content }.associateWith { path ->
                Instant.parse(at(projection, path).jsonPrimitive.content).atZone(ZoneOffset.UTC).plusYears(change.text("years").toLong()).toInstant()
            }
            "offset-seconds" -> mapOf(change.text("path") to ZoneOffset.ofTotalSeconds(change.text("total_seconds").toInt()))
            else -> error("unknown refusal change ${change.text("op")}")
        }

        private fun at(element: JsonElement, pointer: String): JsonElement =
            pointer.removePrefix("/").split("/").fold(element) { node, token ->
                if (node is JsonArray) node[token.toInt()] else node.jsonObject[token]!!
            }
    }
}
