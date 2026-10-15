package org.activitytracker.healthconnect

import android.os.Build
import android.os.ext.SdkExtensions
import androidx.health.connect.client.HealthConnectClient
import expo.modules.kotlin.functions.Coroutine
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.time.Clock
import org.activitytracker.healthconnect.bridge.HealthConnectBridge

/** Expo boundary for the published snapshot reader, change reader and changes-token issuer. */
class HealthConnectModule : Module() {
    override fun definition() = ModuleDefinition {
        Name("ActivityTrackerHealthConnect")

        AsyncFunction("readSnapshotPage") Coroutine {
                recordType: String, snapshotId: String, position: ByteArray?, implementationVersion: String ->
            bridge().readSnapshotPage(recordType, snapshotId, position, implementationVersion)
        }
        AsyncFunction("readChangePage") Coroutine {
                recordType: String, cycleId: String, changesToken: String, unreadSinceMs: Double,
                implementationVersion: String ->
            bridge().readChangePage(recordType, cycleId, changesToken, unreadSinceMs, implementationVersion)
        }
        AsyncFunction("issueChangesToken") Coroutine { recordType: String ->
            bridge().issueChangesToken(recordType)
        }
    }

    private fun bridge(): HealthConnectBridge<ClientHealthConnectAccess> {
        val context = checkNotNull(appContext.reactContext)
        return HealthConnectBridge(
            available = HealthConnectClient.getSdkStatus(context) == HealthConnectClient.SDK_AVAILABLE,
            accessFactory = { ClientHealthConnectAccess(HealthConnectClient.getOrCreate(context)) },
            clock = Clock.systemUTC(),
            providerVersion = "platform-${Build.VERSION.SDK_INT}-ext-${SdkExtensions.getExtensionVersion(Build.VERSION_CODES.UPSIDE_DOWN_CAKE)}",
        )
    }
}
