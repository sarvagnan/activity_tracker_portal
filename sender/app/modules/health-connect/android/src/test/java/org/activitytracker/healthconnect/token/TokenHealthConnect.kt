package org.activitytracker.healthconnect.token

import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.response.ReadRecordsResponse
import org.activitytracker.healthconnect.HealthConnectAccess

/**
 * Synthetic Health Connect for changes-token issuance, with no device, Context, or real data.
 *
 * It models what begin-initial-snapshot@1 relies on from AOSP HealthConnectServiceImpl
 * getChangeLogToken: issuing a token needs each requested type's read permission, and a
 * missing one is a SecurityException. It issues distinct synthetic tokens unless a token is
 * scripted, and it fails the test if anything reads records.
 */
class TokenHealthConnect : HealthConnectAccess {
    sealed interface Call {
        data object Permissions : Call
        class Token(val request: ChangesTokenRequest) : Call
    }

    val calls = mutableListOf<Call>()
    val tokenRequests: List<ChangesTokenRequest> get() = calls.filterIsInstance<Call.Token>().map { it.request }

    var granted: Set<String> = emptySet()

    /** Answers the next token request instead of a generated token. */
    var scriptedToken: String? = null

    /** Thrown by the next permission or token request instead of answering. */
    var permissionFailure: Throwable? = null
    var tokenFailure: Throwable? = null

    /** Runs inside a token request, after the call is recorded and before it is answered. */
    var duringRequest: () -> Unit = {}

    /** Runs after a token request is answered, before the issuer's next call. */
    var afterRequest: () -> Unit = {}

    private var issued = 0

    override suspend fun getGrantedPermissions(): Set<String> {
        calls += Call.Permissions
        permissionFailure?.let { throw it }
        return granted.toSet()
    }

    override suspend fun getChangesToken(request: ChangesTokenRequest): String {
        calls += Call.Token(request)
        duringRequest()
        tokenFailure?.let { throw it }
        if (request.recordTypes.isEmpty()) throw IllegalArgumentException("Requested record types must not be empty.")
        for (type in request.recordTypes) {
            if (HealthPermission.getReadPermission(type) !in granted) {
                throw SecurityException("synthetic read permission missing")
            }
        }
        val token = scriptedToken ?: "synthetic-changes-token-${++issued}"
        afterRequest()
        return token
    }

    override suspend fun <T : Record> readRecords(request: ReadRecordsRequest<T>): ReadRecordsResponse<T> =
        throw AssertionError("beginning a snapshot never reads records")
}
