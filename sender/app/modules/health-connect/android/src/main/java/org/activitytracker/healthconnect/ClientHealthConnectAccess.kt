package org.activitytracker.healthconnect

import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.response.ChangesResponse
import androidx.health.connect.client.response.ReadRecordsResponse

/** Production SDK boundary shared by the published readers and token issuer. */
class ClientHealthConnectAccess(private val client: HealthConnectClient) :
    HealthConnectAccess, HealthConnectChangeAccess {
    override suspend fun getGrantedPermissions(): Set<String> =
        client.permissionController.getGrantedPermissions()

    override suspend fun getChangesToken(request: ChangesTokenRequest): String =
        client.getChangesToken(request)

    override suspend fun <T : Record> readRecords(request: ReadRecordsRequest<T>): ReadRecordsResponse<T> =
        client.readRecords(request)

    override suspend fun getChanges(changesToken: String): ChangesResponse =
        client.getChanges(changesToken)
}
