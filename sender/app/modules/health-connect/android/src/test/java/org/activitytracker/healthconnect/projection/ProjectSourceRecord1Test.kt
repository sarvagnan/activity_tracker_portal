package org.activitytracker.healthconnect.projection

import androidx.health.connect.client.records.Record
import java.io.File
import java.util.Locale
import java.util.TimeZone
import java.util.jar.JarFile
import kotlin.reflect.KClass
import kotlin.reflect.KVisibility
import kotlin.reflect.full.memberProperties
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Acceptance tests for project-source-record@1. Requirement numbers refer to
 * packs/project-source-record-1.spec.md; expected bytes come from the shared
 * vectors under projection/v1, never from the implementation.
 */
class ProjectSourceRecord1Test {
    private val model = ProjectionVectors.model
    private val records = model["records"]!!.jsonObject

    private fun project(vector: JsonObject): ProjectionResult =
        projectSourceRecord(RecordBuilder().build(vector), RecordBuilder.context(vector))

    private fun assertProjects(vectors: List<JsonObject>) {
        val failures = vectors.mapNotNull { vector ->
            when (val result = project(vector)) {
                is ProjectionResult.Projected -> {
                    val actual = result.bytes.toString(Charsets.UTF_8)
                    when {
                        result.recordType != vector.text("record_type") -> "${vector.text("name")}: record type ${result.recordType}"
                        result.schemaVersion != 1 -> "${vector.text("name")}: schema version ${result.schemaVersion}"
                        actual != vector.text("bytes") -> "${vector.text("name")}: bytes differ\n  expected ${vector.text("bytes")}\n  actual   $actual"
                        else -> null
                    }
                }
                is ProjectionResult.Refused -> "${vector.text("name")}: refused ${result.failure} at ${result.path}"
            }
        }
        assertEquals("", failures.joinToString("\n"))
    }

    private fun expectedRefusal(refusal: JsonObject) = ProjectionResult.Refused(
        failure = refusal.text("failure"),
        recordType = if (refusal["change"]!!.jsonObject.text("op") == "custom-record") null else ProjectionVectors.named(refusal.text("base")).text("record_type"),
        path = refusal.text("path"),
    )

    private fun assertRefuses(failure: String) {
        for (refusal in ProjectionVectors.refusals(failure)) {
            val result = projectSourceRecord(RecordBuilder.refusalRecord(refusal), ProjectionContext("0.1.0-synthetic", "synthetic-provider-1"))
            assertEquals(refusal.text("name"), expectedRefusal(refusal), result)
        }
    }

    private fun sdkRecordClasses(): Set<String> {
        val location = File(Record::class.java.protectionDomain.codeSource.location.toURI())
        val names = JarFile(location).use { jar ->
            jar.entries().asSequence().map { it.name }
                .filter { it.startsWith("androidx/health/connect/client/records/") && it.endsWith("Record.class") && '$' !in it }
                .map { it.removeSuffix(".class").replace('/', '.') }
                .toList()
        }
        return names.map { Class.forName(it) }
            .filter { Record::class.java.isAssignableFrom(it) && !it.isInterface && !java.lang.reflect.Modifier.isAbstract(it.modifiers) }
            .map { it.simpleName }
            .toSet()
    }

    private fun sdkNestedClasses(sealed: String): Set<String> {
        val parent = RecordBuilder.sdkClass(sealed).java
        val location = File(Record::class.java.protectionDomain.codeSource.location.toURI())
        return JarFile(location).use { jar ->
            jar.entries().asSequence().map { it.name }
                .filter { it.startsWith("androidx/health/connect/client/records/$sealed$") && it.endsWith(".class") }
                .map { it.removePrefix("androidx/health/connect/client/records/").removeSuffix(".class") }
                .filter { name -> name.count { it == '$' } == 1 && parent.isAssignableFrom(Class.forName("androidx.health.connect.client.records.$name")) }
                .toSet()
        }
    }

    private fun modelProperties(fields: Iterable<kotlinx.serialization.json.JsonElement>): Map<String, Boolean> =
        fields.associate { it.jsonObject.text("property") to (it.jsonObject["nullable"]!!.jsonPrimitive.content == "true") }

    private fun sdkProperties(type: KClass<*>): Map<String, Boolean> =
        type.memberProperties.filter { it.visibility == KVisibility.PUBLIC }.associate { it.name to it.returnType.isMarkedNullable }

    // R2 — Complete coverage of the pinned SDK

    @Test
    fun every_pinned_record_class_has_one_schema() {
        val modelClasses = records.values.map { it.jsonObject.text("class") }
        assertEquals(modelClasses.size, modelClasses.toSet().size)
        assertEquals(sdkRecordClasses(), modelClasses.toSet())
        for (recordType in records.keys) {
            assertTrue(recordType, File(ProjectionVectors.root, "schemas/$recordType.schema.json").isFile)
        }
        assertEquals(records.keys.associateWith { 1 }, PROJECTION_SCHEMA_VERSIONS)
    }

    @Test
    fun every_public_property_maps_to_a_schema_field() {
        for ((recordType, spec) in records) {
            val expected = modelProperties(spec.jsonObject["fields"]!!.jsonArray) + ("metadata" to false)
            assertEquals(recordType, sdkProperties(RecordBuilder.sdkClass(spec.jsonObject.text("class"))), expected)
        }
        for ((type, fields) in model["types"]!!.jsonObject) {
            assertEquals(type, sdkProperties(RecordBuilder.sdkClass(type)), modelProperties(fields.jsonArray))
        }
        for ((sealed, kinds) in model["variants"]!!.jsonObject) {
            for ((kind, variant) in kinds.jsonObject) {
                val type = RecordBuilder.sdkClass(variant.jsonObject.text("class"))
                assertEquals("$sealed/$kind", sdkProperties(type), modelProperties(variant.jsonObject["fields"]!!.jsonArray))
            }
        }
    }

    @Test
    fun medical_records_classes_are_not_projected() {
        val modelClasses = records.values.map { it.jsonObject.text("class") }
        assertTrue(modelClasses.none { "Medical" in it || "Fhir" in it })
        for (name in listOf("MedicalResource", "MedicalDataSource", "FhirResource")) {
            assertFalse(name, Record::class.java.isAssignableFrom(Class.forName("androidx.health.connect.client.records.$name")))
        }
    }

    @Test
    fun unknown_record_class_is_unsupported() = assertRefuses("unsupported-record-type")

    @Test
    fun every_vector_builds_a_valid_sdk_record() {
        for (vector in ProjectionVectors.positive) RecordBuilder().build(vector)
        for (refusal in ProjectionVectors.refusals) RecordBuilder.refusalRecord(refusal)
    }

    // R3 — Common projection envelope

    @Test
    fun projection_envelope_carries_versions_and_metadata() = assertProjects(ProjectionVectors.tagged("envelope"))

    @Test
    fun metadata_device_and_client_id_are_optional() = assertProjects(ProjectionVectors.tagged("minimal"))

    // R4 — Canonical bytes

    @Test
    fun bytes_match_rfc8785_canonical_form() = assertProjects(ProjectionVectors.tagged("numbers") + ProjectionVectors.tagged("text"))

    @Test
    fun large_integers_are_decimal_strings() = assertProjects(ProjectionVectors.tagged("large-integer"))

    @Test
    fun non_finite_values_are_refused() = assertRefuses("non-finite-value")

    @Test
    fun unpaired_surrogates_are_refused() = assertRefuses("unrepresentable-text")

    // R5 — Time and duration

    @Test
    fun instants_keep_source_precision_in_utc() = assertProjects(ProjectionVectors.tagged("precision"))

    @Test
    fun zone_offsets_are_separate_and_optional() = assertProjects(ProjectionVectors.tagged("offset") + ProjectionVectors.tagged("minimal"))

    @Test
    fun unrepresentable_instants_and_offsets_are_refused() {
        assertRefuses("unrepresentable-instant")
        assertRefuses("unrepresentable-offset")
    }

    @Test
    fun durations_are_exact_nanoseconds() = assertProjects(ProjectionVectors.tagged("duration"))

    // R6 — Units without conversion loss

    @Test
    fun measurements_use_documented_storage_units() {
        val documented = mapOf(
            "BloodGlucose" to "millimoles_per_liter", "Energy" to "calories", "Length" to "meters", "Mass" to "grams",
            "Power" to "watts", "Pressure" to "millimeters_of_mercury", "Temperature" to "celsius",
            "TemperatureDelta" to "celsius", "Velocity" to "meters_per_second", "Volume" to "liters",
        )
        assertEquals(documented, model["units"]!!.jsonObject.mapValues { it.value.jsonPrimitive.content })
        assertProjects(ProjectionVectors.tagged("units"))
    }

    @Test
    fun values_read_from_storage_units_project_exactly() =
        assertProjects(ProjectionVectors.tagged("units").filter { "maximal" in it.text("name") || "number" in it.text("name") })

    // R7 — Constants and variants

    @Test
    fun constants_are_sdk_integers_within_declared_sets() {
        for ((group, spec) in model["constants"]!!.jsonObject) {
            val owner = RecordBuilder.sdkClass(if (spec.jsonObject.text("class") in setOf("Metadata", "Device")) "metadata.${spec.jsonObject.text("class")}" else spec.jsonObject.text("class")).java
            for ((name, value) in spec.jsonObject["values"]!!.jsonObject) {
                assertEquals("$group.$name", value.jsonPrimitive.content.toInt(), owner.getField(name).getInt(null))
            }
        }
        assertProjects(ProjectionVectors.tagged("constants"))
    }

    @Test
    fun undeclared_constants_are_refused() = assertRefuses("undeclared-constant")

    @Test
    fun variants_carry_a_kind_discriminator() {
        for ((sealed, kinds) in model["variants"]!!.jsonObject) {
            val declared = kinds.jsonObject.values.map { it.jsonObject.text("class") }.toSet()
            val unknown = sdkNestedClasses(sealed).filter { it.substringAfter('$').startsWith("Unknown") }.toSet()
            assertEquals(sealed, sdkNestedClasses(sealed) - unknown, declared)
        }
        assertProjects(ProjectionVectors.tagged("variants"))
    }

    @Test
    fun undeclared_variants_are_refused() = assertRefuses("undeclared-variant")

    // R8 — Absence, order, and nesting

    @Test
    fun absent_properties_are_omitted() = assertProjects(ProjectionVectors.tagged("absent"))

    @Test
    fun list_order_is_preserved_exactly() = assertProjects(ProjectionVectors.tagged("order"))

    // R9 — Exercise routes

    @Test
    fun exercise_route_results_project_by_kind() = assertProjects(ProjectionVectors.tagged("route"))

    // R10 — Determinism and purity

    @Test
    fun projection_is_independent_of_locale_and_time_zone() {
        val locale = Locale.getDefault()
        val zone = TimeZone.getDefault()
        try {
            for ((tag, zoneId) in listOf("tr-TR" to "Pacific/Kiritimati", "ar-EG" to "America/St_Johns", "hi-IN-u-nu-deva" to "Asia/Kathmandu")) {
                Locale.setDefault(Locale.forLanguageTag(tag))
                TimeZone.setDefault(TimeZone.getTimeZone(zoneId))
                assertProjects(ProjectionVectors.tagged("coverage"))
            }
        } finally {
            Locale.setDefault(locale)
            TimeZone.setDefault(zone)
        }
    }

    @Test
    fun projection_reads_only_its_inputs() {
        assertProjects(ProjectionVectors.tagged("context"))
        val vector = ProjectionVectors.named("weight-maximal")
        val record = RecordBuilder().build(vector)
        val first = projectSourceRecord(record, RecordBuilder.context(vector)) as ProjectionResult.Projected
        val second = projectSourceRecord(record, RecordBuilder.context(vector)) as ProjectionResult.Projected
        assertEquals(first.bytes.toString(Charsets.UTF_8), second.bytes.toString(Charsets.UTF_8))
    }

    // R11 — Shared schemas and vectors

    @Test
    fun every_vector_projects_to_its_expected_bytes() = assertProjects(ProjectionVectors.positive)

    // R12 — Health-data-free refusals

    @Test
    fun refusals_carry_no_field_values() {
        for (refusal in ProjectionVectors.refusals) {
            val result = projectSourceRecord(RecordBuilder.refusalRecord(refusal), ProjectionContext("0.1.0-synthetic", "synthetic-provider-1"))
            assertEquals(refusal.text("name"), expectedRefusal(refusal), result)
        }
    }
}
