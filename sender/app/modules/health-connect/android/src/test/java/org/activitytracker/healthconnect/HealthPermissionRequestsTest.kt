package org.activitytracker.healthconnect

import java.io.File
import androidx.health.connect.client.HealthConnectFeatures
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.*
import org.junit.Test

class HealthPermissionRequestsTest {
    @Test
    fun featureStatusesQueryExactlyHistoryAndBackgroundAndPreserveEachStatus() {
        for (history in listOf(HealthConnectFeatures.FEATURE_STATUS_AVAILABLE, HealthConnectFeatures.FEATURE_STATUS_UNAVAILABLE)) {
            for (background in listOf(HealthConnectFeatures.FEATURE_STATUS_AVAILABLE, HealthConnectFeatures.FEATURE_STATUS_UNAVAILABLE)) {
                val requested = mutableListOf<Int>()
                val statuses = HealthPermissionRequests.featureStatuses { feature ->
                    requested.add(feature)
                    when (feature) {
                        HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_HISTORY -> history
                        HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND -> background
                        else -> error("Unexpected feature")
                    }
                }
                assertEquals(listOf(
                    HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_HISTORY,
                    HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND,
                ), requested)
                assertEquals(mapOf("READ_HEALTH_DATA_HISTORY" to history, "READ_HEALTH_DATA_IN_BACKGROUND" to background), statuses)
            }
        }
    }

    @Test
    fun featureStatusFailuresPropagateRatherThanImplyingAvailability() {
        for (failedFeature in listOf(
            HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_HISTORY,
            HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND,
        )) {
            val failure = IllegalStateException("Feature check failed")
            val actual = assertThrows(IllegalStateException::class.java) {
                HealthPermissionRequests.featureStatuses { feature ->
                    if (feature == failedFeature) throw failure
                    HealthConnectFeatures.FEATURE_STATUS_AVAILABLE
                }
            }
            assertSame(failure, actual)
        }
    }

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
