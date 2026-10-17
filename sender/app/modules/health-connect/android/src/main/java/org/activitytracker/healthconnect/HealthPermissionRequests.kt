package org.activitytracker.healthconnect

import androidx.health.connect.client.permission.HealthPermission
import org.activitytracker.healthconnect.projection.PROJECTION_SCHEMA_VERSIONS
import org.activitytracker.healthconnect.snapshot.SnapshotRecordTypes

/** Same inventory as health-permissions.json; parity is checked by JVM tests. */
internal object HealthPermissionRequests {
    val allowed: Set<String> = PROJECTION_SCHEMA_VERSIONS.keys.map { recordType ->
        HealthPermission.getReadPermission(SnapshotRecordTypes.classes.getValue(recordType))
    }.toSet() + setOf(
        HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY,
        HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND,
    )

    fun filter(permissions: Collection<String>): Set<String> = permissions.toSet().intersect(allowed)
}
