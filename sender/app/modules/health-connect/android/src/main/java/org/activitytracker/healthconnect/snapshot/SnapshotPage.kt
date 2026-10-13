package org.activitytracker.healthconnect.snapshot

import org.activitytracker.healthconnect.HealthConnectAccess
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
)

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

/** Read one page; persists nothing. Interface stub until the frozen pack is implemented. */
suspend fun readSnapshotPage(access: HealthConnectAccess, request: SnapshotPageRequest): SnapshotPageResult =
    TODO("read-snapshot-page@1 is not implemented")
