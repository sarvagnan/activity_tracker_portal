package org.activitytracker.healthconnect

import androidx.health.connect.client.records.Record
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.response.ReadRecordsResponse

/** In-memory test responses only: no Android context, device, identifiers or health payloads. */
class SyntheticHealthConnect : HealthConnectAccess {
    val tokenRequests = mutableListOf<ChangesTokenRequest>()
    val pageRequests = mutableListOf<ReadRecordsRequest<*>>()
    var nextPageToken: String? = null
    var failure: Exception? = null

    override suspend fun getChangesToken(request: ChangesTokenRequest): String {
        failure?.let { throw it }
        tokenRequests.add(request)
        return "synthetic-changes-${tokenRequests.size}"
    }

    override suspend fun <T : Record> readRecords(request: ReadRecordsRequest<T>): ReadRecordsResponse<T> {
        failure?.let { throw it }
        pageRequests.add(request)
        return ReadRecordsResponse(emptyList(), nextPageToken)
    }
}
