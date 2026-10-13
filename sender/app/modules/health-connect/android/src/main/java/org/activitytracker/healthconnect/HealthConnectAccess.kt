package org.activitytracker.healthconnect

import androidx.health.connect.client.records.Record
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.response.ReadRecordsResponse

/** SDK boundary for the snapshot units; no traversal, projection or persistence. */
interface HealthConnectAccess {
    /** The permissions granted to the Sender, as the SDK's PermissionController reports them. */
    suspend fun getGrantedPermissions(): Set<String>

    suspend fun getChangesToken(request: ChangesTokenRequest): String

    suspend fun <T : Record> readRecords(request: ReadRecordsRequest<T>): ReadRecordsResponse<T>
}
