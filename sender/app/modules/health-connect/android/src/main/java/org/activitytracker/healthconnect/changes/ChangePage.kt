package org.activitytracker.healthconnect.changes

import java.time.Clock
import java.time.Duration
import java.time.Instant
import org.activitytracker.healthconnect.HealthConnectChangeAccess
import org.activitytracker.healthconnect.projection.ProjectionContext
import org.activitytracker.healthconnect.projection.ProjectionResult

/** Health Connect deletes change logs after 32 days; a token this old may have lost changes. */
val CHANGES_TOKEN_MAX_AGE: Duration = Duration.ofDays(30)

/** How far the clock may run behind a token's unread-since instant and still be trusted. */
val CLOCK_BACKWARD_TOLERANCE: Duration = Duration.ofDays(1)

/** One page read of one record type's Change Cycle from a held changes token. */
class ChangePageRequest(
    val recordType: String,
    val cycleId: String,
    val changesToken: String,
    val unreadSince: Instant,
    val context: ProjectionContext,
)

/** One Wire Protocol ingest item of a change page. */
sealed interface ChangeItem {
    data class Retraction(val sourceIdentity: String) : ChangeItem

    class Projection(val bytes: ByteArray) : ChangeItem
}

/** The outcome of one change page read. Only a Page carries a token. */
sealed interface ChangePageResult {
    class Page(
        val recordType: String,
        val cycleId: String,
        val projectionSchemaVersion: Int,
        val items: List<ChangeItem>,
        val nextChangesToken: String,
        val nextUnreadSince: Instant,
        val hasMore: Boolean,
    ) : ChangePageResult

    data class TokenExpired(val failure: String) : ChangePageResult

    data class ProjectionRefused(val refusal: ProjectionResult.Refused) : ChangePageResult

    data class NotPermitted(val failure: String) : ChangePageResult

    data class ReadFailed(val failure: String) : ChangePageResult
}

/** Read one page; the returned token is tentative and no progress is persisted. */
suspend fun readChangePage(
    access: HealthConnectChangeAccess,
    clock: Clock,
    request: ChangePageRequest,
): ChangePageResult = TODO("read-change-page@1 is not implemented")
