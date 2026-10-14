package org.activitytracker.healthconnect.changes

import android.os.RemoteException
import androidx.health.connect.client.changes.DeletionChange
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.Record
import java.io.IOException
import java.time.Clock
import java.time.Duration
import java.time.Instant
import java.util.concurrent.CancellationException
import org.activitytracker.healthconnect.HealthConnectChangeAccess
import org.activitytracker.healthconnect.projection.PROJECTION_SCHEMA_VERSIONS
import org.activitytracker.healthconnect.projection.projectSourceRecord
import org.activitytracker.healthconnect.projection.ProjectionContext
import org.activitytracker.healthconnect.projection.ProjectionResult
import org.activitytracker.healthconnect.snapshot.SnapshotRecordTypes

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
) {
    init {
        require(recordType in PROJECTION_SCHEMA_VERSIONS) { "Unknown change record type" }
        require(CYCLE_ID_PATTERN.matches(cycleId)) { "Change Cycle identity must be a lowercase UUID" }
        require(changesToken.isNotEmpty()) { "Changes token must not be empty" }
    }
}

private val CYCLE_ID_PATTERN = Regex("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

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
): ChangePageResult {
    // R13: local token guards precede even the first permission read.
    val now = clock.instant()
    val age = Duration.between(request.unreadSince, now)
    if (age >= CHANGES_TOKEN_MAX_AGE) {
        return ChangePageResult.TokenExpired("changes-token-too-old")
    }
    // Equivalent to unreadSince > now + tolerance, without overflowing Instant.
    if (age < CLOCK_BACKWARD_TOLERANCE.negated()) {
        return ChangePageResult.TokenExpired("clock-behind-changes-token")
    }

    val sdkClass = SnapshotRecordTypes.classes.getValue(request.recordType)
    val readPermission = HealthPermission.getReadPermission(sdkClass)
    val before = try {
        access.getGrantedPermissions()
    } catch (exception: Exception) {
        return classifySeamException(exception)
    }
    missingPermission(before, readPermission)?.let { return it }

    val response = try {
        access.getChanges(request.changesToken)
    } catch (exception: Exception) {
        return classifySeamException(exception)
    }

    val after = try {
        access.getGrantedPermissions()
    } catch (exception: Exception) {
        return classifySeamException(exception)
    }
    missingPermission(after, readPermission)?.let { return it }

    if (response.changesTokenExpired) {
        return ChangePageResult.TokenExpired("changes-token-expired")
    }

    // Validate the entire response before any projection can refuse the page.
    if (response.nextChangesToken.isEmpty() ||
        (response.hasMore && response.nextChangesToken == request.changesToken)
    ) {
        return ChangePageResult.ReadFailed("source-contract-violation")
    }
    val upsertedIds = HashSet<String>()
    val upsertions = ArrayList<Record>()
    val deletions = LinkedHashSet<String>()
    for (change in response.changes) {
        when (change) {
            is UpsertionChange -> {
                val record = change.record
                if (!sdkClass.isInstance(record) || !upsertedIds.add(record.metadata.id)) {
                    return ChangePageResult.ReadFailed("source-contract-violation")
                }
                upsertions.add(record)
            }
            is DeletionChange -> {
                if (!isSourceIdentity(change.recordId)) {
                    return ChangePageResult.ReadFailed("source-contract-violation")
                }
                deletions.add(change.recordId)
            }
            else -> return ChangePageResult.ReadFailed("source-contract-violation")
        }
    }

    // LinkedHashSet retains each deletion's first position; upsertions retain SDK order.
    val items = ArrayList<ChangeItem>()
    for (identity in deletions) items.add(ChangeItem.Retraction(identity))
    for (record in upsertions) {
        when (val projection = projectSourceRecord(record, request.context)) {
            is ProjectionResult.Refused -> return ChangePageResult.ProjectionRefused(projection)
            is ProjectionResult.Projected -> items.add(ChangeItem.Projection(projection.bytes))
        }
    }
    return ChangePageResult.Page(
        request.recordType,
        request.cycleId,
        PROJECTION_SCHEMA_VERSIONS.getValue(request.recordType),
        items,
        response.nextChangesToken,
        if (response.hasMore) request.unreadSince else now,
        response.hasMore,
    )
}

private fun missingPermission(granted: Set<String>, readPermission: String): ChangePageResult.NotPermitted? =
    when {
        readPermission !in granted -> ChangePageResult.NotPermitted("read-permission-missing")
        HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY !in granted ->
            ChangePageResult.NotPermitted("history-permission-missing")
        else -> null
    }

/** Counts Unicode code points only after checking that every surrogate is paired. */
private fun isSourceIdentity(identity: String): Boolean {
    var index = 0
    var codePoints = 0
    while (index < identity.length) {
        val character = identity[index]
        when {
            character <= '\u001f' -> return false
            Character.isHighSurrogate(character) -> {
                if (index + 1 >= identity.length || !Character.isLowSurrogate(identity[index + 1])) {
                    return false
                }
                index += 2
            }
            Character.isLowSurrogate(character) -> return false
            else -> index++
        }
        if (++codePoints > 256) return false
    }
    return codePoints > 0
}

/** Called only by catches containing one access seam call and no other operations. */
private fun classifySeamException(exception: Exception): ChangePageResult = when (exception) {
    is CancellationException -> throw exception
    is SecurityException -> ChangePageResult.NotPermitted("read-denied")
    is RemoteException, is IllegalStateException -> ChangePageResult.ReadFailed("health-connect-unavailable")
    is IOException -> ChangePageResult.ReadFailed("health-connect-io-failure")
    is IllegalArgumentException -> ChangePageResult.ReadFailed("health-connect-request-rejected")
    else -> ChangePageResult.ReadFailed("unexpected-read-failure")
}
