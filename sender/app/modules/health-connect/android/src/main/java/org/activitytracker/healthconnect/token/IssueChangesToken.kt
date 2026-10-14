package org.activitytracker.healthconnect.token

import java.time.Clock
import java.time.Instant
import org.activitytracker.healthconnect.HealthConnectAccess

/** The outcome of requesting one record type's pre-read changes token (begin-initial-snapshot@1). */
sealed interface TokenIssuance {
    /** The token, and the instant read before it was requested, truncated to whole milliseconds. */
    data class Issued(val changesToken: String, val unreadSince: Instant) : TokenIssuance

    data class NotPermitted(val failure: String) : TokenIssuance

    data class ReadFailed(val failure: String) : TokenIssuance
}

/** Request one changes token for a record type and every data origin; nothing is persisted. */
suspend fun issueChangesToken(access: HealthConnectAccess, clock: Clock, recordType: String): TokenIssuance =
    TODO("begin-initial-snapshot@1 is not implemented")
