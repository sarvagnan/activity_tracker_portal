package org.activitytracker.healthconnect.bridge

import java.time.Clock
import java.time.Instant
import org.activitytracker.healthconnect.HealthConnectAccess
import org.activitytracker.healthconnect.HealthConnectChangeAccess
import org.activitytracker.healthconnect.changes.ChangeItem
import org.activitytracker.healthconnect.changes.ChangePageRequest
import org.activitytracker.healthconnect.changes.ChangePageResult
import org.activitytracker.healthconnect.changes.readChangePage as readPublishedChangePage
import org.activitytracker.healthconnect.projection.ProjectionContext
import org.activitytracker.healthconnect.snapshot.SnapshotPageRequest
import org.activitytracker.healthconnect.snapshot.SnapshotPageResult
import org.activitytracker.healthconnect.snapshot.readSnapshotPage as readPublishedSnapshotPage
import org.activitytracker.healthconnect.token.TokenIssuance
import org.activitytracker.healthconnect.token.issueChangesToken as issuePublishedChangesToken

/** Platform-free adaptation to the coordinator's SnapshotRead, ChangeRead and IssuedToken shapes. */
class HealthConnectBridge<A>(
    private val available: Boolean,
    private val accessFactory: () -> A,
    private val clock: Clock,
    private val providerVersion: String,
) where A : HealthConnectAccess, A : HealthConnectChangeAccess {
    suspend fun readSnapshotPage(
        recordType: String,
        snapshotId: String,
        position: ByteArray?,
        implementationVersion: String,
    ): Map<String, Any?> {
        if (!available) return unavailable()
        val request = SnapshotPageRequest(
            recordType, snapshotId, position, ProjectionContext(implementationVersion, providerVersion),
        )
        return readPublishedSnapshotPage(accessFactory(), request).toBridgeResult()
    }

    suspend fun readChangePage(
        recordType: String,
        cycleId: String,
        changesToken: String,
        unreadSinceMs: Double,
        implementationVersion: String,
    ): Map<String, Any?> {
        if (!available) return unavailable()
        require(unreadSinceMs.isFinite() &&
            unreadSinceMs >= -MAX_SAFE_INTEGER && unreadSinceMs <= MAX_SAFE_INTEGER &&
            unreadSinceMs % 1.0 == 0.0
        ) { "unreadSinceMs must be a safe integer" }
        val request = ChangePageRequest(
            recordType, cycleId, changesToken, Instant.ofEpochMilli(unreadSinceMs.toLong()),
            ProjectionContext(implementationVersion, providerVersion),
        )
        return readPublishedChangePage(accessFactory(), clock, request).toBridgeResult()
    }

    suspend fun issueChangesToken(recordType: String): Map<String, Any?> {
        if (!available) return unavailable()
        return issuePublishedChangesToken(accessFactory(), clock, recordType).toBridgeResult()
    }
}

private const val MAX_SAFE_INTEGER = 9_007_199_254_740_991.0

private fun unavailable(): Map<String, Any?> =
    mapOf("outcome" to "read-failed", "failure" to "health-connect-unavailable")

// ByteArray stays binary: Expo converts it to Uint8Array, including inside lists and maps.
internal fun SnapshotPageResult.toBridgeResult(): Map<String, Any?> = when (this) {
    is SnapshotPageResult.Page -> mapOf(
        "outcome" to "page",
        "projectionSchemaVersion" to projectionSchemaVersion,
        "projections" to projections,
        "nextPosition" to nextPosition,
        "exhausted" to exhausted,
    )
    is SnapshotPageResult.ProjectionRefused -> mapOf(
        "outcome" to "projection-refused", "failure" to refusal.failure, "path" to refusal.path,
    )
    is SnapshotPageResult.NotPermitted -> mapOf("outcome" to "not-permitted", "failure" to failure)
    is SnapshotPageResult.ReadFailed -> mapOf("outcome" to "read-failed", "failure" to failure)
    is SnapshotPageResult.InvalidPosition -> mapOf("outcome" to "invalid-position", "failure" to failure)
}

internal fun ChangePageResult.toBridgeResult(): Map<String, Any?> = when (this) {
    is ChangePageResult.Page -> mapOf(
        "outcome" to "page",
        "projectionSchemaVersion" to projectionSchemaVersion,
        "items" to items.map { item ->
            when (item) {
                is ChangeItem.Retraction -> mapOf("kind" to "retraction", "sourceIdentity" to item.sourceIdentity)
                is ChangeItem.Projection -> mapOf("kind" to "projection", "bytes" to item.bytes)
            }
        },
        "nextChangesToken" to nextChangesToken,
        "nextUnreadSinceMs" to nextUnreadSince.toEpochMilli(),
        "hasMore" to hasMore,
    )
    is ChangePageResult.ProjectionRefused -> mapOf(
        "outcome" to "projection-refused", "failure" to refusal.failure, "path" to refusal.path,
    )
    is ChangePageResult.TokenExpired -> mapOf("outcome" to "token-expired", "failure" to failure)
    is ChangePageResult.NotPermitted -> mapOf("outcome" to "not-permitted", "failure" to failure)
    is ChangePageResult.ReadFailed -> mapOf("outcome" to "read-failed", "failure" to failure)
}

internal fun TokenIssuance.toBridgeResult(): Map<String, Any?> = when (this) {
    is TokenIssuance.Issued -> mapOf(
        "outcome" to "issued", "changesToken" to changesToken, "unreadSinceMs" to unreadSince.toEpochMilli(),
    )
    is TokenIssuance.NotPermitted -> mapOf("outcome" to "not-permitted", "failure" to failure)
    is TokenIssuance.ReadFailed -> mapOf("outcome" to "read-failed", "failure" to failure)
}
