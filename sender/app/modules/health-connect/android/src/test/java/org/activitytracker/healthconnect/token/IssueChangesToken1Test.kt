package org.activitytracker.healthconnect.token

import android.os.RemoteException
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.WeightRecord
import java.io.File
import java.io.IOException
import java.time.Clock
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import java.util.concurrent.CancellationException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import kotlin.coroutines.Continuation
import kotlin.coroutines.EmptyCoroutineContext
import kotlin.coroutines.startCoroutine
import kotlin.reflect.KClass
import kotlin.reflect.full.memberProperties
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.activitytracker.healthconnect.projection.PROJECTION_SCHEMA_VERSIONS
import org.activitytracker.healthconnect.token.TokenHealthConnect.Call
import org.activitytracker.healthconnect.token.TokenIssuance.Issued
import org.activitytracker.healthconnect.token.TokenIssuance.NotPermitted
import org.activitytracker.healthconnect.token.TokenIssuance.ReadFailed
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Acceptance tests for the Kotlin part of begin-initial-snapshot@1. Requirement
 * numbers refer to packs/begin-initial-snapshot-1.spec.md. Health Connect is the
 * synthetic TokenHealthConnect.
 */
class IssueChangesToken1Test {
    // R2 — Token request

    @Test
    fun the_clock_is_read_once_before_any_call() {
        val hc = connect()
        val clock = CountingClock(hc, T0)
        val issued = runSuspending { issueChangesToken(hc, clock, "steps") } as Issued
        assertEquals("seam calls made before each clock read", listOf(0), clock.reads)
        assertEquals(T0, issued.unreadSince)
    }

    @Test
    fun one_token_is_requested_for_the_type_and_every_origin() {
        val hc = connect()
        val issued = issue(hc)
        assertEquals(Issued("synthetic-changes-token-1", T0), issued)
        assertEquals(listOf("permissions", "token", "permissions"), hc.calls.map(::kind))
        val request = hc.tokenRequests.single()
        assertEquals(setOf(StepsRecord::class), request.recordTypes)
        assertTrue(request.dataOriginFilters.isEmpty())
    }

    @Test
    fun every_supported_type_requests_its_sdk_class() {
        for (type in PROJECTION_SCHEMA_VERSIONS.keys) {
            val sdkClass = sdkClassNamed(type)
            val hc = connect(granted = setOf(readPermission(sdkClass), HISTORY))
            assertTrue(type, issue(hc, type = type) is Issued)
            assertEquals(type, setOf(sdkClass), hc.tokenRequests.single().recordTypes)
        }
    }

    @Test
    fun unread_since_is_truncated_to_milliseconds() {
        val precise = Instant.parse("2026-10-14T09:00:00.123456789Z")
        val issued = issue(connect(), at = precise) as Issued
        assertEquals(Instant.parse("2026-10-14T09:00:00.123Z"), issued.unreadSince)
        val before1970 = Instant.parse("1969-12-31T23:59:59.999500Z")
        assertEquals(Instant.parse("1969-12-31T23:59:59.999Z"), (issue(connect(), at = before1970) as Issued).unreadSince)
    }

    @Test
    fun unknown_record_type_is_a_caller_error() {
        for (type in listOf("", "Steps", "steps-record", "medical-resource", "exercise-route", "steps ")) {
            assertFalse(type in PROJECTION_SCHEMA_VERSIONS)
            val hc = connect()
            assertThrows(type, IllegalArgumentException::class.java) { issue(hc, type = type) }
            assertTrue(hc.calls.isEmpty())
        }
    }

    // R3 — Full-history permission

    @Test
    fun missing_read_permission_requests_no_token() {
        for (granted in listOf(setOf(HISTORY, readPermission(WeightRecord::class)), emptySet())) {
            val hc = connect(granted = granted)
            assertEquals(NotPermitted("read-permission-missing"), issue(hc))
            assertTrue(hc.tokenRequests.isEmpty())
        }
    }

    @Test
    fun missing_history_permission_requests_no_token() {
        val hc = connect(granted = setOf(readPermission(StepsRecord::class)))
        assertEquals(NotPermitted("history-permission-missing"), issue(hc))
        assertTrue(hc.tokenRequests.isEmpty())
    }

    @Test
    fun permission_lost_during_the_request_discards_the_token() {
        for ((lost, failure) in listOf(readPermission(StepsRecord::class) to "read-permission-missing", HISTORY to "history-permission-missing")) {
            val hc = connect()
            hc.afterRequest = { hc.granted = hc.granted - lost }
            assertEquals(NotPermitted(failure), issue(hc))
            assertEquals(listOf("permissions", "token", "permissions"), hc.calls.map(::kind))
        }
    }

    @Test
    fun security_exception_is_read_denied() {
        val fromToken = connect().apply { tokenFailure = SecurityException(SENTINEL) }
        assertEquals(NotPermitted("read-denied"), issue(fromToken))
        val fromPermissions = connect().apply { permissionFailure = SecurityException(SENTINEL) }
        assertEquals(NotPermitted("read-denied"), issue(fromPermissions))
        assertTrue(fromPermissions.tokenRequests.isEmpty())
        // Revoked between the permission read and the request: the provider itself denies it.
        val revoked = connect()
        revoked.duringRequest = { revoked.granted = revoked.granted - readPermission(StepsRecord::class) }
        assertEquals(NotPermitted("read-denied"), issue(revoked))
    }

    // R4 — Failure classification and validation

    @Test
    fun each_exception_class_maps_to_one_named_failure() {
        val cases = listOf<Pair<() -> Exception, TokenIssuance>>(
            { SecurityException(SENTINEL) } to NotPermitted("read-denied"),
            { RemoteException(SENTINEL) } to ReadFailed("health-connect-unavailable"),
            { IllegalStateException(SENTINEL) } to ReadFailed("health-connect-unavailable"),
            { IOException(SENTINEL) } to ReadFailed("health-connect-io-failure"),
            { IllegalArgumentException(SENTINEL) } to ReadFailed("health-connect-request-rejected"),
            { NumberFormatException(SENTINEL) } to ReadFailed("health-connect-request-rejected"),
            { UnsupportedOperationException(SENTINEL) } to ReadFailed("unexpected-read-failure"),
            { TimeoutException(SENTINEL) } to ReadFailed("unexpected-read-failure"),
            { RuntimeException(SENTINEL) } to ReadFailed("unexpected-read-failure"),
        )
        for ((exception, expected) in cases) {
            val fromToken = connect().apply { tokenFailure = exception() }
            assertEquals("token: ${exception()::class.simpleName}", expected, issue(fromToken))
            val fromPermissions = connect().apply { permissionFailure = exception() }
            assertEquals("permissions: ${exception()::class.simpleName}", expected, issue(fromPermissions))
            assertTrue(fromPermissions.tokenRequests.isEmpty())
            val secondCheck = connect()
            secondCheck.afterRequest = { secondCheck.permissionFailure = exception() }
            assertEquals("permissions after the request: ${exception()::class.simpleName}", expected, issue(secondCheck))
        }
    }

    @Test
    fun cancellation_propagates() {
        val cancelled = CancellationException(SENTINEL)
        val sites = listOf<(TokenHealthConnect) -> Unit>(
            { it.tokenFailure = cancelled },
            { it.permissionFailure = cancelled },
            { hc -> hc.afterRequest = { hc.permissionFailure = cancelled } },
        )
        for (site in sites) {
            val hc = connect().also(site)
            assertSame(cancelled, assertThrows(CancellationException::class.java) { issue(hc) })
        }
        val error = SyntheticError()
        val hc = connect().apply { tokenFailure = error }
        assertSame(error, assertThrows(SyntheticError::class.java) { issue(hc) })
    }

    @Test
    fun an_empty_token_is_a_violation() {
        val hc = connect().apply { scriptedToken = "" }
        assertEquals(ReadFailed("source-contract-violation"), issue(hc))
        assertEquals(Issued(" ", T0), issue(connect().apply { scriptedToken = " " }))
    }

    // R10 — Health-data-free outcomes

    @Test
    fun non_issued_outcomes_carry_only_named_failures() {
        assertEquals(setOf("failure"), NotPermitted::class.memberProperties.map { it.name }.toSet())
        assertEquals(setOf("failure"), ReadFailed::class.memberProperties.map { it.name }.toSet())
        val outcomes = listOf(
            issue(connect().apply { tokenFailure = IOException("$SENTINEL 77.5 kg") }),
            issue(connect().apply { tokenFailure = SecurityException(SENTINEL) }),
            issue(connect().apply { afterRequest = { granted = emptySet() }; scriptedToken = "$SENTINEL-token" }),
            issue(connect().apply { scriptedToken = "" }),
        )
        for (outcome in outcomes) {
            assertFalse(outcome is Issued)
            assertFalse(outcome.toString(), outcome.toString().contains(SENTINEL))
            assertFalse(outcome.toString(), outcome.toString().contains("2026"))
        }
    }

    @Test
    fun every_kotlin_failure_is_reachable_and_in_the_contract() {
        val readOnly = setOf(readPermission(StepsRecord::class))
        val observed = listOf(
            issue(connect(granted = setOf(HISTORY))),
            issue(connect(granted = readOnly)),
            issue(connect().apply { tokenFailure = SecurityException() }),
            issue(connect().apply { tokenFailure = IllegalStateException() }),
            issue(connect().apply { tokenFailure = IOException() }),
            issue(connect().apply { tokenFailure = IllegalArgumentException() }),
            issue(connect().apply { tokenFailure = UnsupportedOperationException() }),
            issue(connect().apply { scriptedToken = "" }),
        ).map { outcome ->
            when (outcome) {
                is NotPermitted -> "not-permitted" to outcome.failure
                is ReadFailed -> "read-failure" to outcome.failure
                is Issued -> throw AssertionError("expected a failure")
            }
        }.toSet()
        val contract = Json.parseToJsonElement(File(repositoryRoot, "governance/scopes/begin-initial-snapshot.1.json").readText()).jsonObject
        val kotlinOutcomes = setOf("not-permitted", "read-failure")
        val named = contract["named_failures"]!!.jsonArray
            .map { it.jsonObject["outcome"]!!.jsonPrimitive.content to it.jsonObject["name"]!!.jsonPrimitive.content }
            .filter { it.first in kotlinOutcomes }
            .toSet()
        assertEquals(named, observed)
    }

    // R11 — Order of checks

    @Test
    fun kotlin_checks_follow_the_documented_order() {
        val none = emptySet<String>()
        connect(granted = none).let { hc ->
            hc.permissionFailure = IOException()
            assertThrows(IllegalArgumentException::class.java) { issue(hc, type = "unknown-type") }
            assertTrue(hc.calls.isEmpty())
        }
        val missingBeforeFailingRequest = connect(granted = none).apply { tokenFailure = IOException() }
        assertEquals(NotPermitted("read-permission-missing"), issue(missingBeforeFailingRequest))
        assertTrue(missingBeforeFailingRequest.tokenRequests.isEmpty())
        val failingRequestBeforeLostPermission = connect()
        failingRequestBeforeLostPermission.tokenFailure = IOException()
        failingRequestBeforeLostPermission.duringRequest = { failingRequestBeforeLostPermission.granted = none }
        assertEquals(ReadFailed("health-connect-io-failure"), issue(failingRequestBeforeLostPermission))
        val lostWithEmptyToken = connect().apply { scriptedToken = "" }
        lostWithEmptyToken.afterRequest = { lostWithEmptyToken.granted = lostWithEmptyToken.granted - HISTORY }
        assertEquals(NotPermitted("history-permission-missing"), issue(lostWithEmptyToken))
    }

    // Helpers

    private class SyntheticError : Error(SENTINEL)
}

/** A clock that moves one second per read and records how many seam calls preceded each read. */
private class CountingClock(private val hc: TokenHealthConnect, private val start: Instant) : Clock() {
    val reads = mutableListOf<Int>()

    override fun instant(): Instant {
        reads += hc.calls.size
        return start + Duration.ofSeconds(reads.size - 1L)
    }

    override fun getZone(): ZoneId = ZoneOffset.UTC

    override fun withZone(zone: ZoneId): Clock = this
}

private const val SENTINEL = "SENTINEL"
private val T0: Instant = Instant.parse("2026-10-14T09:00:00.123Z")
private val HISTORY = HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY

private val repositoryRoot: File = generateSequence(File(System.getProperty("user.dir")).absoluteFile) { it.parentFile }
    .first { File(it, "governance/scopes/begin-initial-snapshot.1.json").isFile }

private fun readPermission(type: KClass<out Record>) = HealthPermission.getReadPermission(type)

private fun connect(granted: Set<String> = setOf(readPermission(StepsRecord::class), HISTORY)) =
    TokenHealthConnect().apply { this.granted = granted }

private fun issue(hc: TokenHealthConnect, type: String = "steps", at: Instant = T0): TokenIssuance =
    runSuspending { issueChangesToken(hc, Clock.fixed(at, ZoneOffset.UTC), type) }

private fun kind(call: Call) = when (call) {
    Call.Permissions -> "permissions"
    is Call.Token -> "token"
}

/** Record type names are SDK class names without `Record`, in kebab case (project-source-record@1). */
private fun sdkClassNamed(type: String): KClass<out Record> {
    val name = type.split("-").joinToString("") { part -> part.replaceFirstChar(Char::uppercaseChar) } + "Record"
    @Suppress("UNCHECKED_CAST")
    return Class.forName("androidx.health.connect.client.records.$name").kotlin as KClass<out Record>
}

/** Runs a suspending call to completion on any thread the implementation chooses. */
private fun <T> runSuspending(block: suspend () -> T): T {
    val done = CountDownLatch(1)
    var outcome: Result<T>? = null
    block.startCoroutine(Continuation(EmptyCoroutineContext) { result ->
        outcome = result
        done.countDown()
    })
    check(done.await(120, TimeUnit.SECONDS)) { "token issuance did not complete" }
    return outcome!!.getOrThrow()
}
