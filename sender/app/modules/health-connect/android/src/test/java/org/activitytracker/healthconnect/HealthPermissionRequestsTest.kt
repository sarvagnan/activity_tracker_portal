package org.activitytracker.healthconnect

import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.*
import org.junit.Test

class HealthPermissionRequestsTest {
    @Test
    fun allowedRequestsExactlyMatchTheManifestInventory() {
        val inventory = generateSequence(File(System.getProperty("user.dir")).absoluteFile) { it.parentFile }
            .map { File(it, "sender/app/modules/health-connect/health-permissions.json") }.first { it.isFile }
        val declared = Json.parseToJsonElement(inventory.readText()).jsonArray.map { it.jsonPrimitive.content }.toSet()
        assertEquals(declared, HealthPermissionRequests.allowed)
        assertEquals(declared, HealthPermissionRequests.filter(declared.toList() + "android.permission.CAMERA"))
        assertTrue(declared.contains("android.permission.health.READ_HEALTH_DATA_HISTORY"))
        assertTrue(declared.contains("android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND"))
    }

    @Test
    fun filtersUnknownWriteAndDuplicatePermissions() {
        val read = "android.permission.health.READ_STEPS"
        assertEquals(setOf(read), HealthPermissionRequests.filter(listOf(
            read, read, "android.permission.health.WRITE_STEPS", "private-record-value",
        )))
        assertTrue(HealthPermissionRequests.filter(emptyList()).isEmpty())
        assertTrue(HealthPermissionRequests.filter(listOf("android.permission.CAMERA")).isEmpty())
    }
}
