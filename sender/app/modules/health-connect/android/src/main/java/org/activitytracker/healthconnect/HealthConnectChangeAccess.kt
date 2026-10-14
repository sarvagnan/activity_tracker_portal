package org.activitytracker.healthconnect

import androidx.health.connect.client.response.ChangesResponse

/**
 * SDK boundary for change pages: permissions and one change read, nothing else.
 *
 * Separate from HealthConnectAccess so the published snapshot unit's bound fake
 * keeps compiling; a production adapter implements both.
 */
interface HealthConnectChangeAccess {
    /** The permissions granted to the Sender, as the SDK's PermissionController reports them. */
    suspend fun getGrantedPermissions(): Set<String>

    suspend fun getChanges(changesToken: String): ChangesResponse
}
