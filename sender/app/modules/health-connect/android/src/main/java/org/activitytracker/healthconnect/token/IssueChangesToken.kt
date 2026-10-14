package org.activitytracker.healthconnect.token

import android.os.RemoteException
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.request.ChangesTokenRequest
import java.io.IOException
import java.time.Clock
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.concurrent.CancellationException
import org.activitytracker.healthconnect.HealthConnectAccess
import org.activitytracker.healthconnect.snapshot.SnapshotRecordTypes

/** The outcome of requesting one record type's pre-read changes token (begin-initial-snapshot@1). */
sealed interface TokenIssuance {
    /** The token, and the instant read before it was requested, truncated to whole milliseconds. */
    data class Issued(val changesToken: String, val unreadSince: Instant) : TokenIssuance

    data class NotPermitted(val failure: String) : TokenIssuance

    data class ReadFailed(val failure: String) : TokenIssuance
}

/** Request one changes token for a record type and every data origin; nothing is persisted. */
suspend fun issueChangesToken(access: HealthConnectAccess, clock: Clock, recordType: String): TokenIssuance {
    val sdkClass = SnapshotRecordTypes.classes[recordType]
        ?: throw IllegalArgumentException("Unknown record type")
    val unreadSince = clock.instant().truncatedTo(ChronoUnit.MILLIS)
    val readPermission = HealthPermission.getReadPermission(sdkClass)
    return try {
        missingPermission(access.getGrantedPermissions(), readPermission)?.let { return it }
        val token = access.getChangesToken(
            ChangesTokenRequest(setOf(sdkClass), dataOriginFilters = emptySet()),
        )
        missingPermission(access.getGrantedPermissions(), readPermission)?.let { return it }
        if (token.isEmpty()) TokenIssuance.ReadFailed("source-contract-violation")
        else TokenIssuance.Issued(token, unreadSince)
    } catch (exception: CancellationException) {
        throw exception
    } catch (exception: SecurityException) {
        TokenIssuance.NotPermitted("read-denied")
    } catch (exception: RemoteException) {
        TokenIssuance.ReadFailed("health-connect-unavailable")
    } catch (exception: IllegalStateException) {
        TokenIssuance.ReadFailed("health-connect-unavailable")
    } catch (exception: IOException) {
        TokenIssuance.ReadFailed("health-connect-io-failure")
    } catch (exception: IllegalArgumentException) {
        TokenIssuance.ReadFailed("health-connect-request-rejected")
    } catch (exception: Exception) {
        TokenIssuance.ReadFailed("unexpected-read-failure")
    }
}

private fun missingPermission(granted: Set<String>, readPermission: String): TokenIssuance.NotPermitted? = when {
    readPermission !in granted -> TokenIssuance.NotPermitted("read-permission-missing")
    HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY !in granted ->
        TokenIssuance.NotPermitted("history-permission-missing")
    else -> null
}
