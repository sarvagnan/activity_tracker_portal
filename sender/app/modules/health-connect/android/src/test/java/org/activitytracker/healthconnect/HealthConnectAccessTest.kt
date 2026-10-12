package org.activitytracker.healthconnect

import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import java.time.Instant
import kotlin.coroutines.Continuation
import kotlin.coroutines.EmptyCoroutineContext
import kotlin.coroutines.startCoroutine
import org.junit.Assert.*
import org.junit.Test

class HealthConnectAccessTest {
    @Test
    fun tokenRequestUsesTheSdkTypesThroughTheSeam() = runImmediate {
        val fake = SyntheticHealthConnect()
        val access: HealthConnectAccess = fake
        val request = ChangesTokenRequest(setOf(StepsRecord::class))

        assertEquals("synthetic-changes-1", access.getChangesToken(request))
        assertSame(request, fake.tokenRequests.single())
    }

    @Test
    fun emptySyntheticPagePreservesTheSdkPageToken() = runImmediate {
        val fake = SyntheticHealthConnect()
        fake.nextPageToken = "synthetic-next-page"
        val access: HealthConnectAccess = fake
        val request = ReadRecordsRequest(
            recordType = StepsRecord::class,
            timeRangeFilter = TimeRangeFilter.after(Instant.EPOCH),
            pageSize = 1,
        )

        val page = access.readRecords(request)
        assertTrue(page.records.isEmpty())
        assertEquals("synthetic-next-page", page.pageToken)
        assertSame(request, fake.pageRequests.single())
    }

    @Test
    fun syntheticFailurePassesThroughWithoutClaimingARead() = runImmediate {
        val fake = SyntheticHealthConnect()
        val failure = IllegalStateException("synthetic-unavailable")
        fake.failure = failure
        val access: HealthConnectAccess = fake

        try {
            access.getChangesToken(ChangesTokenRequest(setOf(StepsRecord::class)))
            fail("Expected synthetic failure")
        } catch (actual: IllegalStateException) {
            assertSame(failure, actual)
        }
        assertTrue(fake.tokenRequests.isEmpty())
        assertTrue(fake.pageRequests.isEmpty())
    }
}

/** These fake calls complete synchronously; fail loudly if a later fake suspends. */
private fun runImmediate(block: suspend () -> Unit) {
    var outcome: Result<Unit>? = null
    block.startCoroutine(object : Continuation<Unit> {
        override val context = EmptyCoroutineContext
        override fun resumeWith(result: Result<Unit>) {
            outcome = result
        }
    })
    checkNotNull(outcome) { "Synthetic test unexpectedly suspended" }.getOrThrow()
}
