package org.activitytracker.healthconnect.snapshot

import android.os.RemoteException
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import java.io.IOException
import java.time.Instant
import java.util.concurrent.CancellationException
import org.activitytracker.healthconnect.HealthConnectAccess
import org.activitytracker.healthconnect.projection.PROJECTION_SCHEMA_VERSIONS
import org.activitytracker.healthconnect.projection.projectSourceRecord
import org.activitytracker.healthconnect.projection.ProjectionContext
import org.activitytracker.healthconnect.projection.ProjectionResult

/** Records on one snapshot page (read-snapshot-page@1). */
const val SNAPSHOT_PAGE_RECORD_LIMIT = 1000

/** Health Connect's largest record limit for one read. */
const val HEALTH_CONNECT_READ_LIMIT = 5000

/** One page read of one record type's snapshot; a null position is the first page. */
class SnapshotPageRequest(
    val recordType: String,
    val snapshotId: String,
    val position: ByteArray?,
    val context: ProjectionContext,
) {
    init {
        require(recordType in PROJECTION_SCHEMA_VERSIONS) { "Unknown snapshot record type" }
        require(SNAPSHOT_ID_PATTERN.matches(snapshotId)) { "Snapshot identity must be a lowercase UUID" }
    }
}

private val SNAPSHOT_ID_PATTERN = Regex("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

/** The outcome of one snapshot page read. Only a Page carries a position. */
sealed interface SnapshotPageResult {
    class Page(
        val recordType: String,
        val snapshotId: String,
        val projectionSchemaVersion: Int,
        val projections: List<ByteArray>,
        val nextPosition: ByteArray,
        val exhausted: Boolean,
    ) : SnapshotPageResult

    data class ProjectionRefused(val refusal: ProjectionResult.Refused) : SnapshotPageResult

    data class NotPermitted(val failure: String) : SnapshotPageResult

    data class ReadFailed(val failure: String) : SnapshotPageResult

    data class InvalidPosition(val failure: String) : SnapshotPageResult
}

/** Read one page; the returned position is tentative and no progress is persisted. */
suspend fun readSnapshotPage(access: HealthConnectAccess, request: SnapshotPageRequest): SnapshotPageResult {
    // R13: position checks precede even the first permission read.
    val position = if (request.position == null) {
        SnapshotPosition(request.recordType, request.snapshotId)
    } else {
        parseSnapshotPosition(request.position)
            ?: return SnapshotPageResult.InvalidPosition("malformed-position")
    }
    if (position.recordType != request.recordType || position.snapshotId != request.snapshotId) {
        return SnapshotPageResult.InvalidPosition("foreign-position")
    }

    val sdkClass = SnapshotRecordTypes.classes.getValue(request.recordType)
    val readPermission = HealthPermission.getReadPermission(sdkClass)
    val start = position.boundaryStart ?: Instant.ofEpochMilli(Long.MIN_VALUE)
    val limit = SNAPSHOT_PAGE_RECORD_LIMIT + minOf(
        position.boundaryIds.size, HEALTH_CONNECT_READ_LIMIT - SNAPSHOT_PAGE_RECORD_LIMIT,
    )
    // Build outside the seam catches: our own request defects must propagate.
    val readRequest = ReadRecordsRequest(
        recordType = sdkClass,
        timeRangeFilter = TimeRangeFilter.between(start, Instant.ofEpochMilli(Long.MAX_VALUE)),
        dataOriginFilter = emptySet(),
        ascendingOrder = true,
        pageSize = limit,
        pageToken = null,
    )

    val before = try {
        access.getGrantedPermissions()
    } catch (exception: Exception) {
        return classifySeamException(exception)
    }
    missingPermission(before, readPermission)?.let { return it }

    val response = try {
        access.readRecords(readRequest)
    } catch (exception: Exception) {
        return classifySeamException(exception)
    }

    val after = try {
        access.getGrantedPermissions()
    } catch (exception: Exception) {
        return classifySeamException(exception)
    }
    missingPermission(after, readPermission)?.let { return it }

    // R10 applies to the entire response, including excluded and untaken records.
    if (response.records.size > limit) return SnapshotPageResult.ReadFailed("source-order-violation")
    val identifiers = HashSet<String>()
    val starts = ArrayList<Instant>(response.records.size)
    var previousStart = start
    for (record in response.records) {
        if (!sdkClass.isInstance(record)) return SnapshotPageResult.ReadFailed("source-order-violation")
        val recordStart = SnapshotRecordTypes.start(record)
        if (recordStart < previousStart || !identifiers.add(record.metadata.id)) {
            return SnapshotPageResult.ReadFailed("source-order-violation")
        }
        starts.add(recordStart)
        previousStart = recordStart
    }

    val excluded = position.boundaryIds.toHashSet()
    val remaining = response.records.indices.filter { index ->
        starts[index] != position.boundaryStart || response.records[index].metadata.id !in excluded
    }
    val page = remaining.take(SNAPSHOT_PAGE_RECORD_LIMIT)
    // An exhausted equal-time group can reread empty; only a token makes it unpassable.
    if (page.isEmpty() && response.pageToken != null) {
        return SnapshotPageResult.ReadFailed("boundary-group-too-large")
    }

    val projections = ArrayList<ByteArray>(page.size)
    for (index in page) {
        when (val projection = projectSourceRecord(response.records[index], request.context)) {
            is ProjectionResult.Refused -> return SnapshotPageResult.ProjectionRefused(projection)
            is ProjectionResult.Projected -> projections.add(projection.bytes)
        }
    }

    val nextPosition = if (page.isEmpty()) position else {
        val boundary = starts[page.last()]
        val ids = if (boundary == position.boundaryStart) excluded else HashSet<String>()
        for (index in page) {
            if (starts[index] == boundary) ids.add(response.records[index].metadata.id)
        }
        SnapshotPosition(request.recordType, request.snapshotId, boundary, ids.sortedWith(SNAPSHOT_UTF8_ORDER))
    }
    return SnapshotPageResult.Page(
        request.recordType,
        request.snapshotId,
        PROJECTION_SCHEMA_VERSIONS.getValue(request.recordType),
        projections,
        nextPosition.encode(),
        response.pageToken == null && remaining.size == page.size,
    )
}

private fun missingPermission(granted: Set<String>, readPermission: String): SnapshotPageResult.NotPermitted? =
    when {
        readPermission !in granted -> SnapshotPageResult.NotPermitted("read-permission-missing")
        HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY !in granted ->
            SnapshotPageResult.NotPermitted("history-permission-missing")
        else -> null
    }

/** Called only by catches containing one access seam call and no other operations. */
private fun classifySeamException(exception: Exception): SnapshotPageResult = when (exception) {
    is CancellationException -> throw exception
    is SecurityException -> SnapshotPageResult.NotPermitted("read-denied")
    is RemoteException, is IllegalStateException -> SnapshotPageResult.ReadFailed("health-connect-unavailable")
    is IOException -> SnapshotPageResult.ReadFailed("health-connect-io-failure")
    is IllegalArgumentException -> SnapshotPageResult.ReadFailed("health-connect-request-rejected")
    else -> SnapshotPageResult.ReadFailed("unexpected-read-failure")
}
