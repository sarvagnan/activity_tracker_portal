package org.activitytracker.healthconnect

import androidx.health.connect.client.permission.HealthPermission
import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonPrimitive
import org.activitytracker.healthconnect.projection.PROJECTION_SCHEMA_VERSIONS
import org.activitytracker.healthconnect.snapshot.SnapshotRecordTypes
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class HealthPermissionsTest {
    @Test
    fun manifestInventoryExactlyMatchesNativeProjectionReadPermissions() {
        val relativePath = "sender/app/modules/health-connect/health-permissions.json"
        val inventory = generateSequence(File(System.getProperty("user.dir")).absoluteFile) { it.parentFile }
            .map { File(it, relativePath) }.first { it.isFile }
        val declared = Json.parseToJsonElement(inventory.readText()).jsonArray
            .map { it.jsonPrimitive.content }
        assertEquals("Permission inventory must not contain duplicates", declared.size, declared.toSet().size)
        val expected = PROJECTION_SCHEMA_VERSIONS.keys.map { recordType ->
            HealthPermission.getReadPermission(SnapshotRecordTypes.classes.getValue(recordType))
        }.toSet() + setOf(
            HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY,
            HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND,
        )
        val missing = expected - declared.toSet()
        val extra = declared.toSet() - expected
        assertTrue(
            "Permission inventory mismatch: missing=$missing, extra=$extra",
            missing.isEmpty() && extra.isEmpty(),
        )
    }
}
