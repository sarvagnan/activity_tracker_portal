package org.activitytracker.healthconnect.projection

import androidx.health.connect.client.records.Record

/** Caller-supplied values that keep projection free of ambient state. */
data class ProjectionContext(
    val implementationVersion: String,
    val providerVersion: String,
) {
    init {
        require(implementationVersion.isNotEmpty()) { "implementationVersion must not be empty" }
        require(providerVersion.isNotEmpty()) { "providerVersion must not be empty" }
    }
}

/** The outcome of projecting one record (project-source-record@1). */
sealed interface ProjectionResult {
    /** Exact canonical UTF-8 Source Projection bytes. */
    class Projected(
        val recordType: String,
        val schemaVersion: Int,
        val bytes: ByteArray,
    ) : ProjectionResult

    /** A named refusal carrying no field values. */
    data class Refused(
        val failure: String,
        val recordType: String?,
        val path: String,
    ) : ProjectionResult
}

/** Every record type's current Source Projection schema version. */
val PROJECTION_SCHEMA_VERSIONS: Map<String, Int>
    get() = SourceRecordEncoder.schemaVersions

/** Project one typed Health Connect record to its Source Projection. */
fun projectSourceRecord(record: Record, context: ProjectionContext): ProjectionResult {
    val recordType = SourceRecordEncoder.recordType(record)
        ?: return ProjectionResult.Refused("unsupported-record-type", null, "")
    return try {
        ProjectionResult.Projected(
            recordType,
            1,
            SourceRecordEncoder.encode(record, recordType, context).toByteArray(Charsets.UTF_8),
        )
    } catch (refusal: ProjectionFailure) {
        ProjectionResult.Refused(refusal.failure, recordType, refusal.path)
    }
}
