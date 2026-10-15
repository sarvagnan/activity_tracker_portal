package org.activitytracker.healthconnect.bridge

import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.response.ChangesResponse
import java.io.IOException
import java.time.Clock
import java.time.Instant
import java.time.ZoneOffset
import java.util.concurrent.CancellationException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.coroutines.Continuation
import kotlin.coroutines.EmptyCoroutineContext
import kotlin.coroutines.startCoroutine
import kotlin.reflect.jvm.isAccessible
import org.activitytracker.healthconnect.HealthConnectAccess
import org.activitytracker.healthconnect.HealthConnectChangeAccess
import org.activitytracker.healthconnect.changes.ChangeHealthConnect
import org.activitytracker.healthconnect.changes.ChangeItem
import org.activitytracker.healthconnect.changes.ChangePageResult
import org.activitytracker.healthconnect.projection.ProjectionContext
import org.activitytracker.healthconnect.projection.ProjectionResult
import org.activitytracker.healthconnect.projection.projectSourceRecord
import org.activitytracker.healthconnect.snapshot.SnapshotHealthConnect
import org.activitytracker.healthconnect.snapshot.SnapshotPageResult
import org.activitytracker.healthconnect.token.TokenHealthConnect
import org.activitytracker.healthconnect.token.TokenIssuance
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class HealthConnectBridgeTest {
    @Test
    fun snapshot_variants_have_exact_keys_values_and_bytes() {
        for (exhausted in listOf(false, true)) {
            exact(
                mapOf("outcome" to "page", "projectionSchemaVersion" to 7,
                    "projections" to listOf(byteArrayOf(0, -1, -128), byteArrayOf(42)),
                    "nextPosition" to byteArrayOf(127, 0, -1), "exhausted" to exhausted),
                SnapshotPageResult.Page("steps", ID, 7,
                    listOf(byteArrayOf(0, -1, -128), byteArrayOf(42)), byteArrayOf(127, 0, -1), exhausted)
                    .toBridgeResult(),
            )
        }
        exact(refusal(), SnapshotPageResult.ProjectionRefused(REFUSAL).toBridgeResult())
        exact(failure("not-permitted", "read-denied"), SnapshotPageResult.NotPermitted("read-denied").toBridgeResult())
        exact(failure("read-failed", "health-connect-io-failure"), SnapshotPageResult.ReadFailed("health-connect-io-failure").toBridgeResult())
        exact(failure("invalid-position", "foreign-position"), SnapshotPageResult.InvalidPosition("foreign-position").toBridgeResult())
    }

    @Test
    fun change_variants_have_exact_keys_values_items_and_epoch_milliseconds() {
        for (hasMore in listOf(false, true)) {
            exact(
                mapOf("outcome" to "page", "projectionSchemaVersion" to 9,
                    "items" to listOf(mapOf("kind" to "retraction", "sourceIdentity" to "deleted"),
                        mapOf("kind" to "projection", "bytes" to byteArrayOf(-128, 0, -1))),
                    "nextChangesToken" to "next", "nextUnreadSinceMs" to -1234L, "hasMore" to hasMore),
                ChangePageResult.Page("steps", ID, 9,
                    listOf(ChangeItem.Retraction("deleted"), ChangeItem.Projection(byteArrayOf(-128, 0, -1))),
                    "next", Instant.ofEpochMilli(-1234), hasMore).toBridgeResult(),
            )
        }
        exact(refusal(), ChangePageResult.ProjectionRefused(REFUSAL).toBridgeResult())
        exact(failure("token-expired", "changes-token-expired"), ChangePageResult.TokenExpired("changes-token-expired").toBridgeResult())
        exact(failure("not-permitted", "read-denied"), ChangePageResult.NotPermitted("read-denied").toBridgeResult())
        exact(failure("read-failed", "source-contract-violation"), ChangePageResult.ReadFailed("source-contract-violation").toBridgeResult())
    }

    @Test
    fun token_variants_have_exact_keys_values_and_epoch_milliseconds() {
        exact(mapOf("outcome" to "issued", "changesToken" to "issued", "unreadSinceMs" to 1234L),
            TokenIssuance.Issued("issued", Instant.ofEpochMilli(1234)).toBridgeResult())
        exact(failure("not-permitted", "read-permission-missing"), TokenIssuance.NotPermitted("read-permission-missing").toBridgeResult())
        exact(failure("read-failed", "unexpected-read-failure"), TokenIssuance.ReadFailed("unexpected-read-failure").toBridgeResult())
    }

    @Test
    fun unavailable_returns_failure_without_constructing_access_or_reading_clock() {
        val bridge = HealthConnectBridge<JoinedAccess>(false,
            { throw AssertionError("access must not be constructed") },
            object : Clock() {
                override fun instant(): Instant = throw AssertionError("clock must not be read")
                override fun getZone() = ZoneOffset.UTC
                override fun withZone(zone: java.time.ZoneId): Clock = this
            }, "provider")
        val expected = failure("read-failed", "health-connect-unavailable")
        exact(expected, runSuspending { bridge.readSnapshotPage("steps", ID, null, IMPLEMENTATION) })
        exact(expected, runSuspending { bridge.readChangePage("steps", ID, "held", NOW.toEpochMilli().toDouble(), IMPLEMENTATION) })
        exact(expected, runSuspending { bridge.issueChangesToken("steps") })
    }

    @Test
    fun unread_since_accepts_safe_integer_boundaries_and_preserves_them_on_more_pages() {
        for (ms in listOf(-9_007_199_254_740_991L, -1L, 0L, 1L, 9_007_199_254_740_991L)) {
            val instant = Instant.ofEpochMilli(ms)
            val changes = changes().apply {
                now = instant
                respond = { _, _ -> ChangesResponse(emptyList(), "next", true, false) }
            }
            val token = changes.issueToken(StepsRecord::class)
            val bridge = bridge(changes = changes, at = instant)
            exact(mapOf("outcome" to "page", "projectionSchemaVersion" to 1, "items" to emptyList<Any>(),
                "nextChangesToken" to "next", "nextUnreadSinceMs" to ms, "hasMore" to true),
                runSuspending { bridge.readChangePage("steps", ID, token, ms.toDouble(), IMPLEMENTATION) })
            assertEquals(listOf(token), changes.changeTokens)
        }
    }

    @Test
    fun unread_since_rejects_nonfinite_fractional_and_unsafe_values_before_access() {
        val bridge = HealthConnectBridge<JoinedAccess>(true,
            { throw AssertionError("invalid arguments must precede access") }, CLOCK, PROVIDER)
        for (ms in listOf(Double.NaN, Double.POSITIVE_INFINITY, Double.NEGATIVE_INFINITY,
            0.5, -0.5, 1_234_567_890.125, 9_007_199_254_740_992.0, -9_007_199_254_740_992.0, Double.MAX_VALUE)) {
            assertThrows(ms.toString(), IllegalArgumentException::class.java) {
                runSuspending { bridge.readChangePage("steps", ID, "held", ms, IMPLEMENTATION) }
            }
        }
    }

    @Test
    fun snapshot_passes_context_and_round_trips_opaque_position() {
        val record = steps()
        val snapshot = snapshot().apply { insert(record) }
        val bridge = bridge(primary = snapshot)
        val page = runSuspending { bridge.readSnapshotPage("steps", ID, null, IMPLEMENTATION) }
        val position = page["nextPosition"] as ByteArray
        exact(mapOf("outcome" to "page", "projectionSchemaVersion" to 1,
            "projections" to listOf(projected(record)), "nextPosition" to position, "exhausted" to true), page)
        val projection = (page["projections"] as List<*>).single() as ByteArray
        checkContext(projection)
        exact(mapOf("outcome" to "page", "projectionSchemaVersion" to 1,
            "projections" to emptyList<ByteArray>(), "nextPosition" to position, "exhausted" to true),
            runSuspending { bridge.readSnapshotPage("steps", ID, position, IMPLEMENTATION) })
    }

    @Test
    fun changes_pass_context_token_and_items_and_date_drained_page_from_clock() {
        val changes = changes()
        val deleted = steps("deleted")
        changes.upsert(deleted)
        val token = changes.issueToken(StepsRecord::class)
        changes.delete("deleted")
        val record = steps()
        changes.upsert(record)
        val bridge = bridge(changes = changes)
        val page = runSuspending {
            bridge.readChangePage("steps", ID, token, NOW.minusSeconds(1).toEpochMilli().toDouble(), IMPLEMENTATION)
        }
        exact(mapOf("outcome" to "page", "projectionSchemaVersion" to 1,
            "items" to listOf(mapOf("kind" to "retraction", "sourceIdentity" to "deleted"),
                mapOf("kind" to "projection", "bytes" to projected(record))),
            "nextChangesToken" to changes.answers.single().nextChangesToken,
            "nextUnreadSinceMs" to NOW.toEpochMilli(), "hasMore" to false), page)
        val item = (page["items"] as List<*>)[1] as Map<*, *>
        checkContext(item["bytes"] as ByteArray)
        assertEquals(listOf(token), changes.changeTokens)
    }

    @Test
    fun token_issuer_passes_record_type_and_uses_injected_clock() {
        val tokens = TokenHealthConnect().apply { granted = PERMISSIONS; scriptedToken = "issued-token" }
        val at = NOW.plusNanos(987654)
        exact(mapOf("outcome" to "issued", "changesToken" to "issued-token", "unreadSinceMs" to NOW.toEpochMilli()),
            runSuspending { bridge(primary = tokens, at = at).issueChangesToken("steps") })
        assertEquals(setOf(StepsRecord::class), tokens.tokenRequests.single().recordTypes)
        assertTrue(tokens.tokenRequests.single().dataOriginFilters.isEmpty())
    }

    @Test
    fun published_failure_outcomes_reach_the_bridge() {
        exact(failure("not-permitted", "read-permission-missing"), runSuspending {
            bridge(primary = snapshot().apply { granted = emptySet() }).readSnapshotPage("steps", ID, null, IMPLEMENTATION)
        })
        exact(failure("invalid-position", "malformed-position"), runSuspending {
            bridge().readSnapshotPage("steps", ID, byteArrayOf(0), IMPLEMENTATION)
        })
        exact(failure("read-failed", "health-connect-io-failure"), runSuspending {
            bridge(primary = snapshot().apply { recordFailure = IOException() }).readSnapshotPage("steps", ID, null, IMPLEMENTATION)
        })
        exact(failure("token-expired", "changes-token-expired"), runSuspending {
            bridge().readChangePage("steps", ID, "unknown", NOW.toEpochMilli().toDouble(), IMPLEMENTATION)
        })
        exact(failure("not-permitted", "read-denied"), runSuspending {
            bridge(changes = changes().apply { changesFailure = SecurityException() })
                .readChangePage("steps", ID, "held", NOW.toEpochMilli().toDouble(), IMPLEMENTATION)
        })
        exact(failure("read-failed", "health-connect-io-failure"), runSuspending {
            bridge(changes = changes().apply { changesFailure = IOException() })
                .readChangePage("steps", ID, "held", NOW.toEpochMilli().toDouble(), IMPLEMENTATION)
        })
        exact(failure("not-permitted", "read-permission-missing"), runSuspending {
            bridge(primary = TokenHealthConnect()).issueChangesToken("steps")
        })
        exact(failure("read-failed", "health-connect-io-failure"), runSuspending {
            bridge(primary = TokenHealthConnect().apply { granted = PERMISSIONS; tokenFailure = IOException() })
                .issueChangesToken("steps")
        })
    }

    @Test
    fun malformed_requests_and_cancellation_reject_instead_of_becoming_maps() {
        assertThrows(IllegalArgumentException::class.java) {
            runSuspending { bridge().readSnapshotPage("steps", "bad-id", null, IMPLEMENTATION) }
        }
        assertThrows(IllegalArgumentException::class.java) {
            runSuspending { bridge().readChangePage("steps", ID, "", NOW.toEpochMilli().toDouble(), IMPLEMENTATION) }
        }
        assertThrows(IllegalArgumentException::class.java) {
            runSuspending { bridge().issueChangesToken("unknown") }
        }
        assertThrows(CancellationException::class.java) {
            runSuspending { bridge(primary = snapshot().apply { permissionFailure = CancellationException() })
                .readSnapshotPage("steps", ID, null, IMPLEMENTATION) }
        }
    }
}

private class JoinedAccess(
    private val primary: HealthConnectAccess,
    changes: HealthConnectChangeAccess,
) : HealthConnectAccess by primary, HealthConnectChangeAccess by changes {
    override suspend fun getGrantedPermissions(): Set<String> = primary.getGrantedPermissions()
}

private const val ID = "00000000-0000-4000-8000-000000000001"
private const val IMPLEMENTATION = "sender-test-implementation"
private const val PROVIDER = "platform-34-ext-17"
private val NOW = Instant.parse("2026-10-13T00:00:00.123Z")
private val CLOCK = Clock.fixed(NOW, ZoneOffset.UTC)
private val PERMISSIONS = setOf(HealthPermission.getReadPermission(StepsRecord::class), HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY)
private val REFUSAL = ProjectionResult.Refused("invalid-unicode", "steps", "/metadata/id")
private fun refusal() = mapOf("outcome" to "projection-refused", "failure" to "invalid-unicode", "path" to "/metadata/id")
private fun failure(outcome: String, failure: String) = mapOf("outcome" to outcome, "failure" to failure)
private fun snapshot() = SnapshotHealthConnect().apply { granted = PERMISSIONS }
private fun changes() = ChangeHealthConnect().apply { granted = PERMISSIONS; now = NOW }
private fun bridge(primary: HealthConnectAccess = snapshot(), changes: HealthConnectChangeAccess = changes(), at: Instant = NOW) =
    HealthConnectBridge(true, { JoinedAccess(primary, changes) }, Clock.fixed(at, ZoneOffset.UTC), PROVIDER)

private fun steps(id: String = "source"): StepsRecord {
    val constructor = Metadata::class.constructors.single { it.parameters.size == 7 }.apply { isAccessible = true }
    val metadata = constructor.call(Metadata.RECORDING_METHOD_UNKNOWN, id, DataOrigin("org.example.synthetic"), NOW, null, 0L, null)
    return StepsRecord(NOW.minusSeconds(60), null, NOW, null, 123, metadata)
}

private fun projected(record: StepsRecord) =
    (projectSourceRecord(record, ProjectionContext(IMPLEMENTATION, PROVIDER)) as ProjectionResult.Projected).bytes

private fun checkContext(bytes: ByteArray) {
    val json = bytes.toString(Charsets.UTF_8)
    assertTrue(json.contains("\"projection_implementation_version\":\"$IMPLEMENTATION\""))
    assertTrue(json.contains("\"provider_version\":\"$PROVIDER\""))
}

/** Deep comparison checks exact key sets, ordering, binary types and every byte. */
private fun exact(expected: Any?, actual: Any?) {
    when (expected) {
        is ByteArray -> {
            assertTrue("expected binary bytes", actual is ByteArray)
            assertArrayEquals(expected, actual as ByteArray)
        }
        is Map<*, *> -> {
            assertTrue("expected map", actual is Map<*, *>)
            actual as Map<*, *>
            assertEquals(expected.keys, actual.keys)
            expected.forEach { (key, value) -> exact(value, actual[key]) }
        }
        is List<*> -> {
            assertTrue("expected list", actual is List<*>)
            actual as List<*>
            assertEquals(expected.size, actual.size)
            expected.indices.forEach { exact(expected[it], actual[it]) }
        }
        else -> assertEquals(expected, actual)
    }
}

private fun <T> runSuspending(block: suspend () -> T): T {
    val done = CountDownLatch(1)
    var outcome: Result<T>? = null
    block.startCoroutine(Continuation(EmptyCoroutineContext) { outcome = it; done.countDown() })
    check(done.await(30, TimeUnit.SECONDS)) { "bridge call did not complete" }
    return outcome!!.getOrThrow()
}
