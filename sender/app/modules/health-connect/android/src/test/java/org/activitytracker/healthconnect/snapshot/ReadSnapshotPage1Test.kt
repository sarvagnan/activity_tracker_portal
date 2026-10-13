package org.activitytracker.healthconnect.snapshot

import android.os.RemoteException
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.records.metadata.Device
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.units.Mass
import java.io.File
import java.io.IOException
import java.time.Duration
import java.time.Instant
import java.util.concurrent.CancellationException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import kotlin.coroutines.Continuation
import kotlin.coroutines.EmptyCoroutineContext
import kotlin.coroutines.startCoroutine
import kotlin.random.Random
import kotlin.reflect.KClass
import kotlin.reflect.full.memberProperties
import kotlin.reflect.jvm.isAccessible
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.activitytracker.healthconnect.projection.PROJECTION_SCHEMA_VERSIONS
import org.activitytracker.healthconnect.projection.ProjectionContext
import org.activitytracker.healthconnect.projection.ProjectionResult
import org.activitytracker.healthconnect.projection.projectSourceRecord
import org.activitytracker.healthconnect.snapshot.SnapshotHealthConnect.Call
import org.activitytracker.healthconnect.snapshot.SnapshotHealthConnect.Companion.startOf
import org.activitytracker.healthconnect.snapshot.SnapshotPageResult.InvalidPosition
import org.activitytracker.healthconnect.snapshot.SnapshotPageResult.NotPermitted
import org.activitytracker.healthconnect.snapshot.SnapshotPageResult.Page
import org.activitytracker.healthconnect.snapshot.SnapshotPageResult.ProjectionRefused
import org.activitytracker.healthconnect.snapshot.SnapshotPageResult.ReadFailed
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Acceptance tests for read-snapshot-page@1. Requirement numbers refer to
 * packs/read-snapshot-page-1.spec.md. Health Connect is the synthetic
 * SnapshotHealthConnect; expected bytes come from project-source-record@1.
 */
class ReadSnapshotPage1Test {
    // R2 — Request validation

    @Test
    fun unknown_record_type_is_a_caller_error() {
        for (type in listOf("", "Steps", "steps-record", "medical-resource", "exercise-route", "steps ")) {
            assertFalse(type in PROJECTION_SCHEMA_VERSIONS)
            assertThrows(type, IllegalArgumentException::class.java) { SnapshotPageRequest(type, SNAPSHOT, null, CONTEXT) }
        }
        SnapshotPageRequest("steps", SNAPSHOT, null, CONTEXT)
    }

    @Test
    fun malformed_snapshot_identity_is_a_caller_error() {
        val malformed = listOf(
            "",
            "00000000-0000-4000-8000-00000000A001",
            "000000000000400080000000000000a001",
            "00000000-0000-4000-8000-00000000a0011",
            "{00000000-0000-4000-8000-00000000a001}",
            " 00000000-0000-4000-8000-00000000a001",
        )
        for (snapshot in malformed) {
            assertThrows(snapshot, IllegalArgumentException::class.java) { SnapshotPageRequest("steps", snapshot, null, CONTEXT) }
        }
        SnapshotPageRequest("steps", OTHER_SNAPSHOT, null, CONTEXT)
    }

    // R3 — Full-history permission, checked around the read

    @Test
    fun missing_read_permission_reads_no_records() {
        for (granted in listOf(setOf(HISTORY, readPermission(WeightRecord::class)), emptySet())) {
            val hc = connect(steps("a", T0), granted = granted)
            assertEquals(NotPermitted("read-permission-missing"), read(hc))
            assertTrue(hc.recordRequests.isEmpty())
        }
    }

    @Test
    fun missing_history_permission_reads_no_records() {
        // The synthetic provider would silently hide this record, as Health Connect does.
        val hc = connect(steps("old", Instant.parse("2020-06-01T07:30:00Z")), granted = setOf(readPermission(StepsRecord::class)))
        assertEquals(NotPermitted("history-permission-missing"), read(hc))
        assertTrue(hc.recordRequests.isEmpty())
    }

    @Test
    fun permissions_are_read_before_and_after_the_records() {
        val hc = connect(steps("a", T0))
        read(hc).page()
        assertEquals(listOf("permissions", "records", "permissions"), hc.calls.map(::kind))
    }

    @Test
    fun permission_lost_during_the_read_discards_the_page() {
        for ((lost, failure) in listOf(readPermission(StepsRecord::class) to "read-permission-missing", HISTORY to "history-permission-missing")) {
            val hc = connect(steps("a", T0))
            hc.afterRead = { hc.granted = hc.granted - lost }
            assertEquals(NotPermitted(failure), read(hc))
            assertEquals(listOf("permissions", "records", "permissions"), hc.calls.map(::kind))
        }
    }

    @Test
    fun security_exception_is_read_denied() {
        val fromRecords = connect(steps("a", T0)).apply { recordFailure = SecurityException("synthetic") }
        assertEquals(NotPermitted("read-denied"), read(fromRecords))
        val fromPermissions = connect(steps("a", T0)).apply { permissionFailure = SecurityException("synthetic") }
        assertEquals(NotPermitted("read-denied"), read(fromPermissions))
        assertTrue(fromPermissions.recordRequests.isEmpty())
    }

    // R4 — The read request

    @Test
    fun first_page_reads_every_origin_over_the_full_range() {
        val hc = connect(
            steps("alpha", T0, origin = ALPHA),
            steps("beta", T0.plusSeconds(60), origin = BETA),
            steps("before-1970", Instant.parse("1969-07-20T20:17:00Z")),
            steps("future", Instant.parse("2030-03-01T09:00:00Z")),
        )
        val page = read(hc).page()
        assertEquals(listOf("before-1970", "alpha", "beta", "future"), page.ids())
        val request = hc.recordRequests.single()
        assertEquals(StepsRecord::class, request.recordType)
        assertEquals(FULL_RANGE_START, request.timeRangeFilter.startTime)
        assertEquals(FULL_RANGE_END, request.timeRangeFilter.endTime)
        assertNull(request.timeRangeFilter.localStartTime)
        assertNull(request.timeRangeFilter.localEndTime)
        assertTrue(request.dataOriginFilter.isEmpty())
        assertTrue(request.ascendingOrder)
        assertEquals(SNAPSHOT_PAGE_RECORD_LIMIT, request.pageSize)
        assertNull(request.pageToken)
    }

    @Test
    fun later_pages_resume_at_the_boundary_start_time() {
        val shapes = listOf<Triple<String, KClass<out Record>, (String, Instant) -> Record>>(
            Triple("steps", StepsRecord::class) { id, start -> steps(id, start) },
            Triple("weight", WeightRecord::class) { id, start -> weight(id, start) },
            Triple("heart-rate", HeartRateRecord::class) { id, start -> heartRate(id, start) },
        )
        for ((type, sdkClass, make) in shapes) {
            val records = (0 until 1001).map { make(id(it), T0 + minutes(it)) }
            val hc = connect(*records.toTypedArray(), granted = fullAccess(sdkClass))
            val first = read(hc, type = type).page()
            assertEquals(type, (0 until 1000).map(::id), first.ids())
            val second = read(hc, first.nextPosition, type = type).page()
            assertEquals(type, listOf(id(1000)), second.ids())
            val request = hc.recordRequests.last()
            assertEquals(type, sdkClass, request.recordType)
            assertEquals(type, startOf(records[999]), request.timeRangeFilter.startTime)
            assertEquals(type, FULL_RANGE_END, request.timeRangeFilter.endTime)
            assertEquals(type, SNAPSHOT_PAGE_RECORD_LIMIT + 1, request.pageSize)
            assertTrue(type, request.dataOriginFilter.isEmpty())
            assertTrue(type, request.ascendingOrder)
        }
    }

    @Test
    fun no_request_carries_a_page_token() {
        val hc = connect(*distinctSteps(2500).toTypedArray())
        val pages = traverse(hc)
        assertEquals((0 until 2500).map(::id), pages.flatMap { it.ids() })
        assertTrue(hc.recordRequests.all { it.pageToken == null })
    }

    @Test
    fun record_limit_grows_with_the_boundary_group() {
        for ((identifiers, limit) in listOf(1 to 1001, 3 to 1003, 4000 to 5000, 4999 to 5000, 6000 to 5000)) {
            val hc = connect()
            read(hc, boundaryPosition(T0, (0 until identifiers).map(::id))).page()
            assertEquals("$identifiers identifiers", limit, hc.recordRequests.single().pageSize)
        }
    }

    @Test
    fun every_supported_type_reads_its_sdk_class() {
        for (type in PROJECTION_SCHEMA_VERSIONS.keys) {
            val sdkClass = sdkClassNamed(type)
            val hc = connect(granted = fullAccess(sdkClass))
            read(hc, type = type).page()
            assertEquals(type, sdkClass, hc.recordRequests.single().recordType)
        }
    }

    // R5 — Complete pages

    @Test
    fun page_bytes_equal_the_shared_projection() {
        val cases = listOf<Triple<String, KClass<out Record>, List<Record>>>(
            Triple("steps", StepsRecord::class, listOf(steps("a", T0, origin = BETA), steps("b", T0.plusSeconds(90), clientRecordId = "synthetic-client-1"))),
            Triple("weight", WeightRecord::class, listOf(weight("w1", T0), weight("w2", T0.plusMillis(250)))),
            Triple("heart-rate", HeartRateRecord::class, listOf(heartRate("h1", T0), heartRate("h2", T0.plusSeconds(600)))),
        )
        for ((type, sdkClass, records) in cases) {
            val hc = connect(*records.toTypedArray(), granted = fullAccess(sdkClass))
            val page = read(hc, type = type).page()
            assertEquals(type, page.recordType)
            assertEquals(SNAPSHOT, page.snapshotId)
            assertEquals(PROJECTION_SCHEMA_VERSIONS.getValue(type), page.projectionSchemaVersion)
            assertEquals(type, records.size, page.projections.size)
            records.zip(page.projections).forEach { (record, bytes) -> assertArrayEquals(type, projected(record), bytes) }
        }
    }

    @Test
    fun page_keeps_health_connect_order() {
        val hc = connect(steps("c", T0 + minutes(2)), steps("a", T0), steps("b1", T0 + minutes(1)), steps("b0", T0 + minutes(1)))
        assertEquals(listOf("a", "b1", "b0", "c"), read(hc).page().ids())
    }

    @Test
    fun one_unprojectable_record_refuses_the_whole_page() {
        val bad = steps("bad", T0 + minutes(1), clientRecordId = "\uD800")
        val hc = connect(steps("a", T0), bad, steps("c", T0 + minutes(2)))
        assertEquals(ProjectionRefused(refused(bad)), read(hc))
    }

    @Test
    fun refusal_is_the_first_in_page_order() {
        val later = steps("later", T0 + minutes(2), clientRecordId = "\uD800")
        val earlier = steps("earlier", T0 + minutes(1), device = Device(Device.TYPE_UNKNOWN, "\uDC00", null))
        assertNotEquals(refused(later), refused(earlier))
        val hc = connect(later, steps("a", T0), earlier)
        assertEquals(ProjectionRefused(refused(earlier)), read(hc))
    }

    @Test
    fun records_beyond_the_page_are_not_projected() {
        val bad = steps("bad", T0 + minutes(1000), clientRecordId = "\uD800")
        val hc = connect(*distinctSteps(1000).toTypedArray(), bad)
        val first = read(hc).page()
        assertEquals((0 until 1000).map(::id), first.ids())
        assertFalse(first.exhausted)
        assertEquals(ProjectionRefused(refused(bad)), read(hc, first.nextPosition))
    }

    // R6 — Project-owned position

    @Test
    fun positions_are_canonical_json() {
        val hc = connect(steps("r1", T0), steps("r3", T0 + minutes(1)), steps("r2", T0 + minutes(1)))
        assertEquals(
            """{"boundary_record_ids":["r2","r3"],"boundary_start":"2026-01-01T07:31:00Z","position_version":1,"record_type":"steps","snapshot_id":"$SNAPSHOT"}""",
            read(hc).page().nextPosition.decodeToString(),
        )
        val millis = connect(steps("m", T0.plusMillis(123)))
        assertEquals(
            """{"boundary_record_ids":["m"],"boundary_start":"2026-01-01T07:30:00.123Z","position_version":1,"record_type":"steps","snapshot_id":"$SNAPSHOT"}""",
            read(millis).page().nextPosition.decodeToString(),
        )
        for ((nanos, text) in listOf(123_000L to "07:30:00.000123Z", 123_456_789L to "07:30:00.123456789Z")) {
            assertEquals(
                """{"boundary_record_ids":["p"],"boundary_start":"2026-01-01T$text","position_version":1,"record_type":"steps","snapshot_id":"$SNAPSHOT"}""",
                read(connect(steps("p", T0.plusNanos(nanos)))).page().nextPosition.decodeToString(),
            )
        }
        // UTF-8 byte order puts U+FF71 (EF BD B1) before U+1F600 (F0 9F 98 80); UTF-16 order would not.
        val escaped = connect(steps("\uD83D\uDE00", T0), steps("q\"uote\\back", T0), steps("\uFF71", T0))
        assertEquals(
            "{\"boundary_record_ids\":[\"q\\\"uote\\\\back\",\"\uFF71\",\"\uD83D\uDE00\"],\"boundary_start\":\"2026-01-01T07:30:00Z\",\"position_version\":1,\"record_type\":\"steps\",\"snapshot_id\":\"$SNAPSHOT\"}",
            read(escaped).page().nextPosition.decodeToString(),
        )
        assertEquals(listOf("\uD83D\uDE00", "q\"uote\\back", "\uFF71"), read(escaped).page().ids())
    }

    @Test
    fun null_position_equals_the_start_form() {
        val hc = connect(steps("a", T0), steps("b", T0 + minutes(1)), steps("c", T0 + minutes(2)))
        assertSamePage(read(hc).page(), read(hc, startPosition()).page())
        val empty = read(connect()).page()
        assertArrayEquals(startPosition(), empty.nextPosition)
        assertArrayEquals(startPosition(), read(connect(), startPosition()).page().nextPosition)
    }

    @Test
    fun next_position_names_the_last_start_time_and_its_identifiers() {
        val boundary = T0 + minutes(998)
        val group = (1..5).map { steps("g-$it", boundary) }
        val hc = connect(*distinctSteps(998).toTypedArray(), *group.toTypedArray())
        val first = read(hc).page()
        assertArrayEquals(boundaryPosition(boundary, listOf("g-1", "g-2")), first.nextPosition)
        val distinct = connect(*distinctSteps(1001).toTypedArray())
        assertArrayEquals(boundaryPosition(T0 + minutes(999), listOf(id(999))), read(distinct).page().nextPosition)
    }

    @Test
    fun boundary_identifiers_accumulate_within_one_start_time() {
        val records = (0 until 2500).map { steps(id(it), T0) }
        val hc = connect(*records.toTypedArray())
        val first = read(hc).page()
        assertArrayEquals(boundaryPosition(T0, (0 until 1000).map(::id)), first.nextPosition)
        val second = read(hc, first.nextPosition).page()
        assertArrayEquals(boundaryPosition(T0, (0 until 2000).map(::id)), second.nextPosition)
    }

    @Test
    fun malformed_positions_are_rejected_before_reading() {
        val start = """{"position_version":1,"record_type":"steps","snapshot_id":"$SNAPSHOT"}"""
        val tail = """"position_version":1,"record_type":"steps","snapshot_id":"$SNAPSHOT"}"""
        val malformed = listOf(
            byteArrayOf(0xff.toByte(), 0xfe.toByte()),
            ByteArray(0),
            "position".encodeToByteArray(),
            "[]".encodeToByteArray(),
            "$start\n".encodeToByteArray(),
            """{"position_version": 1,"record_type":"steps","snapshot_id":"$SNAPSHOT"}""".encodeToByteArray(),
            """{"record_type":"steps","position_version":1,"snapshot_id":"$SNAPSHOT"}""".encodeToByteArray(),
            "{\"position_version\":1,\"record_type\":\"st\\u0065ps\",\"snapshot_id\":\"$SNAPSHOT\"}".encodeToByteArray(),
            """{"position_version":1,"record_type":"steps"}""".encodeToByteArray(),
            """{"cursor":"x",$tail""".encodeToByteArray(),
            """{"position_version":"1","record_type":"steps","snapshot_id":"$SNAPSHOT"}""".encodeToByteArray(),
            """{"position_version":2,"record_type":"steps","snapshot_id":"$SNAPSHOT"}""".encodeToByteArray(),
            """{"position_version":1.0,"record_type":"steps","snapshot_id":"$SNAPSHOT"}""".encodeToByteArray(),
            """{"boundary_start":"2026-01-01T07:30:00Z",$tail""".encodeToByteArray(),
            """{"boundary_record_ids":["a"],$tail""".encodeToByteArray(),
            """{"boundary_record_ids":[],"boundary_start":"2026-01-01T07:30:00Z",$tail""".encodeToByteArray(),
            """{"boundary_record_ids":["b","a"],"boundary_start":"2026-01-01T07:30:00Z",$tail""".encodeToByteArray(),
            """{"boundary_record_ids":["a","a"],"boundary_start":"2026-01-01T07:30:00Z",$tail""".encodeToByteArray(),
            """{"boundary_record_ids":[1],"boundary_start":"2026-01-01T07:30:00Z",$tail""".encodeToByteArray(),
            """{"boundary_record_ids":["a"],"boundary_start":"yesterday",$tail""".encodeToByteArray(),
            """{"boundary_record_ids":["a"],"boundary_start":"2026-01-01T07:30:00.000Z",$tail""".encodeToByteArray(),
            """{"boundary_record_ids":["a"],"boundary_start":"2026-01-01T08:30:00+01:00",$tail""".encodeToByteArray(),
            """{"boundary_record_ids":["a"],"boundary_start":null,$tail""".encodeToByteArray(),
            """{"position_version":1,"position_version":1,"record_type":"steps","snapshot_id":"$SNAPSHOT"}""".encodeToByteArray(),
            "{\"boundary_record_ids\":[\"\\ud800\"],\"boundary_start\":\"2026-01-01T07:30:00Z\",$tail".encodeToByteArray(),
            "{\"boundary_record_ids\":[\"\\u0061\"],\"boundary_start\":\"2026-01-01T07:30:00Z\",$tail".encodeToByteArray(),
            """{"boundary_record_ids":["a"],"boundary_start":"+10000-01-01T00:00:00Z",$tail""".encodeToByteArray(),
            """{"boundary_record_ids":["a"],"boundary_start":"-0001-01-01T00:00:00Z",$tail""".encodeToByteArray(),
        )
        for (position in malformed) {
            val hc = connect(steps("a", T0))
            assertEquals(position.decodeToString(), InvalidPosition("malformed-position"), read(hc, position))
            assertTrue(position.decodeToString(), hc.calls.isEmpty())
        }
    }

    @Test
    fun foreign_positions_are_rejected_before_reading() {
        val foreign = listOf(
            startPosition(type = "weight"),
            startPosition(snapshot = OTHER_SNAPSHOT),
            boundaryPosition(T0, listOf("a"), snapshot = OTHER_SNAPSHOT),
            boundaryPosition(T0, listOf("a"), type = "heart-rate"),
        )
        for (position in foreign) {
            val hc = connect(steps("a", T0))
            assertEquals(position.decodeToString(), InvalidPosition("foreign-position"), read(hc, position))
            assertTrue(position.decodeToString(), hc.calls.isEmpty())
        }
    }

    // R7 — Exhaustion and no durable progress

    @Test
    fun exhaustion_requires_no_token_and_no_untaken_records() {
        val boundary = T0 + minutes(998)
        val after = (0 until 1002).map { steps("s-%05d".format(it), boundary + minutes(1 + it)) }
        val hc = connect(*distinctSteps(998).toTypedArray(), steps("g-1", boundary), steps("g-2", boundary), *after.toTypedArray())
        val tokens = mutableListOf<String?>()
        hc.respond = { _, records, token -> tokens += token; records to token }
        val first = read(hc).page()
        assertFalse(first.exhausted)
        hc.delete("g-1")
        hc.delete("g-2")
        val second = read(hc, first.nextPosition).page()
        assertNull("the provider reports no further page", tokens.last())
        assertEquals(after.take(1000).map { it.metadata.id }, second.ids())
        assertFalse("two matched records remain untaken", second.exhausted)
        val third = read(hc, second.nextPosition).page()
        assertEquals(listOf("s-01000", "s-01001"), third.ids())
        assertTrue(third.exhausted)
    }

    @Test
    fun exhausted_position_rereads_as_an_empty_exhausted_page() {
        val hc = connect(steps("a", T0), steps("b", T0 + minutes(1)))
        val last = read(hc).page()
        assertTrue(last.exhausted)
        val again = read(hc, last.nextPosition).page()
        assertTrue(again.projections.isEmpty())
        assertTrue(again.exhausted)
        assertArrayEquals(last.nextPosition, again.nextPosition)
    }

    @Test
    fun rereading_a_position_is_byte_identical() {
        val hc = connect(*distinctSteps(2500).toTypedArray())
        assertSamePage(read(hc).page(), read(hc).page())
        val first = read(hc).page()
        assertSamePage(read(hc, first.nextPosition).page(), read(hc, first.nextPosition).page())
    }

    @Test
    fun empty_type_is_one_empty_exhausted_page() {
        for (hc in listOf(connect(), connect(weight("w", T0), granted = fullAccess(StepsRecord::class, WeightRecord::class)))) {
            val page = read(hc).page()
            assertTrue(page.projections.isEmpty())
            assertTrue(page.exhausted)
            assertArrayEquals(startPosition(), page.nextPosition)
        }
    }

    // R8 — Exactly once for unchanged records

    @Test
    fun unchanged_records_appear_exactly_once_under_concurrent_change() {
        for ((seed, group) in (1..8).map { it to 3 } + (9..12).map { it to 1200 }) {
            val random = Random(seed)
            val initial = (0 until 3200).map { steps(id(it), T0 + minutes(it / group), origin = if (it % 2 == 0) ALPHA else BETA) }
            val hc = connect(*initial.toTypedArray())
            val live = initial.associateBy { it.metadata.id }.toMutableMap()
            val changed = mutableSetOf<String>()
            var inserted = 0
            fun mutate(tail: List<String>) {
                repeat(random.nextInt(1, 30)) {
                    val pool = if (tail.isNotEmpty() && random.nextBoolean()) tail.filter { it in live } else live.keys.toList()
                    if (pool.isEmpty()) return@repeat
                    val id = pool[random.nextInt(pool.size)]
                    val current = live.getValue(id)
                    when (random.nextInt(4)) {
                        0 -> { hc.delete(id); live.remove(id); changed += id }
                        1 -> current.copy(start = T0 + minutes(random.nextInt(0, 1100))).let { hc.replace(it); live[id] = it; changed += id }
                        2 -> current.copy(count = current.count + 1).let { hc.replace(it); live[id] = it; changed += id }
                        else -> steps("n-%05d".format(inserted++), T0 + minutes(random.nextInt(0, 1100))).let { hc.insert(it); live[it.metadata.id] = it }
                    }
                }
            }
            hc.duringRead = { read -> if (read > 1 && random.nextInt(3) == 0) mutate(emptyList()) }
            val seen = mutableListOf<String>()
            var position: ByteArray? = null
            for (step in 1..100) {
                val page = read(hc, position).page()
                seen += page.ids()
                if (page.exhausted) break
                mutate(page.ids().takeLast(6))
                position = page.nextPosition
                assertTrue("seed $seed did not exhaust", step < 100)
            }
            val unchanged = initial.filter { it.metadata.id !in changed }
            val counts = seen.groupingBy { it }.eachCount()
            for (record in unchanged) {
                assertEquals("seed $seed, ${record.metadata.id}", 1, counts[record.metadata.id])
            }
            val starts = initial.associate { it.metadata.id to it.startTime }
            val order = seen.filter { it in starts && it !in changed }.map { starts.getValue(it) }
            assertEquals("seed $seed keeps start order", order.sorted(), order)
        }
    }

    @Test
    fun deleting_a_returned_boundary_record_skips_nothing() {
        val hc = boundaryGroupConnection()
        val first = read(hc).page()
        assertEquals(listOf("g-1", "g-2"), first.ids().takeLast(2))
        hc.delete("g-1")
        val second = read(hc, first.nextPosition).page()
        assertEquals(listOf("g-3", "g-4"), second.ids())
        assertTrue(second.exhausted)
    }

    @Test
    fun moving_a_returned_boundary_record_skips_nothing() {
        val hc = boundaryGroupConnection()
        val first = read(hc).page()
        hc.replace(steps("g-1", T0 + minutes(998 + 60)))
        val second = read(hc, first.nextPosition).page()
        assertEquals(listOf("g-3", "g-4", "g-1"), second.ids())
        assertTrue(second.exhausted)
    }

    // R9 — Large equal-time groups

    @Test
    fun an_equal_time_group_larger_than_a_page_spans_pages() {
        val hc = connect(*(0 until 2500).map { steps(id(it), T0) }.toTypedArray())
        val pages = traverse(hc)
        assertEquals(listOf(1000, 1000, 500), pages.map { it.projections.size })
        assertEquals(listOf(false, false, true), pages.map { it.exhausted })
        assertEquals((0 until 2500).map(::id), pages.flatMap { it.ids() })
        assertEquals(listOf(1000, 2000, 3000), hc.recordRequests.map { it.pageSize })
        assertEquals(listOf(FULL_RANGE_START, T0, T0), hc.recordRequests.map { it.timeRangeFilter.startTime })
    }

    @Test
    fun a_boundary_group_of_exactly_the_read_limit_exhausts() {
        val hc = connect(*(0 until HEALTH_CONNECT_READ_LIMIT).map { steps(id(it), T0) }.toTypedArray())
        val pages = traverse(hc)
        assertEquals(List(5) { 1000 }, pages.map { it.projections.size })
        assertTrue(pages.last().exhausted)
        val again = read(hc, pages.last().nextPosition).page()
        assertTrue(again.projections.isEmpty())
        assertTrue(again.exhausted)
        assertArrayEquals(pages.last().nextPosition, again.nextPosition)
    }

    @Test
    fun a_boundary_group_beyond_the_read_limit_fails() {
        for (more in listOf(steps(id(HEALTH_CONNECT_READ_LIMIT), T0), steps("later", T0 + minutes(1)))) {
            val hc = connect(*(0 until HEALTH_CONNECT_READ_LIMIT).map { steps(id(it), T0) }.toTypedArray(), more)
            var position: ByteArray? = null
            repeat(5) { position = read(hc, position).page().also { assertFalse(it.exhausted) }.nextPosition }
            assertEquals(ReadFailed("boundary-group-too-large"), read(hc, position))
            assertEquals(HEALTH_CONNECT_READ_LIMIT, hc.recordRequests.last().pageSize)
        }
    }

    // R10 — Response validation

    @Test
    fun oversized_responses_are_violations() {
        val hc = connect(*distinctSteps(1000).toTypedArray())
        hc.respond = { _, records, token -> (records + steps("extra", T0 + minutes(5000))) to token }
        assertEquals(ReadFailed("source-order-violation"), read(hc))
    }

    @Test
    fun records_before_the_requested_start_are_violations() {
        val hc = connect(steps("a", T0 + minutes(10)), steps("b", T0 + minutes(11)))
        hc.respond = { _, records, token -> (listOf(steps("early", T0)) + records) to token }
        assertEquals(ReadFailed("source-order-violation"), read(hc, boundaryPosition(T0 + minutes(10), listOf("z"))))
    }

    @Test
    fun unordered_responses_are_violations() {
        val hc = connect(steps("a", T0), steps("b", T0 + minutes(1)))
        hc.respond = { _, records, token -> records.reversed() to token }
        assertEquals(ReadFailed("source-order-violation"), read(hc))
    }

    @Test
    fun duplicate_identifiers_are_violations() {
        val hc = connect(steps("a", T0), steps("b", T0 + minutes(1)))
        hc.respond = { _, records, token -> (records + steps("b", T0 + minutes(2))) to token }
        assertEquals(ReadFailed("source-order-violation"), read(hc))
    }

    @Test
    fun records_of_another_type_are_violations() {
        val hc = connect(steps("a", T0), steps("b", T0 + minutes(1)))
        hc.respond = { _, records, token -> (records + weight("w", T0 + minutes(2))) to token }
        assertEquals(ReadFailed("source-order-violation"), read(hc))
    }

    @Test
    fun violations_anywhere_in_the_response_count() {
        val later = (0 until 1003).map { steps("s-%05d".format(it), T0 + minutes(1 + it)) }
        val beyond = connect(*later.toTypedArray())
        beyond.respond = { _, records, token -> (records.dropLast(2) + records.takeLast(2).reversed()) to token }
        assertEquals(ReadFailed("source-order-violation"), read(beyond, boundaryPosition(T0, listOf("x-1", "x-2", "x-3"))))
        val excluded = connect(steps("e-1", T0), steps("e-2", T0), *later.toTypedArray())
        excluded.respond = { _, records, token -> (listOf(records[0], records[0]) + records.drop(2)) to token }
        assertEquals(ReadFailed("source-order-violation"), read(excluded, boundaryPosition(T0, listOf("e-1", "e-2"))))
    }

    // R11 — Failure classification

    @Test
    fun each_exception_class_maps_to_one_named_failure() {
        val cases = listOf<Pair<() -> Exception, SnapshotPageResult>>(
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
            val fromRecords = connect(steps("a", T0)).apply { recordFailure = exception() }
            assertEquals("records: ${exception()::class.simpleName}", expected, read(fromRecords))
            val fromPermissions = connect(steps("a", T0)).apply { permissionFailure = exception() }
            assertEquals("permissions: ${exception()::class.simpleName}", expected, read(fromPermissions))
            assertTrue(fromPermissions.recordRequests.isEmpty())
            val secondCheck = connect(steps("a", T0)).apply { afterRead = { permissionFailure = exception() } }
            assertEquals("permissions after the read: ${exception()::class.simpleName}", expected, read(secondCheck))
        }
    }

    @Test
    fun cancellation_propagates() {
        val cancelled = CancellationException(SENTINEL)
        val sites = listOf<(SnapshotHealthConnect) -> Unit>(
            { it.recordFailure = cancelled },
            { it.permissionFailure = cancelled },
            { hc -> hc.afterRead = { hc.permissionFailure = cancelled } },
        )
        for (site in sites) {
            val thrown = assertThrows(CancellationException::class.java) { read(connect(steps("a", T0)).also(site)) }
            assertSame(cancelled, thrown)
        }
        val error = SyntheticError()
        assertSame(error, assertThrows(SyntheticError::class.java) { read(connect(steps("a", T0)).apply { recordFailure = error }) })
    }

    // R12 — Health-data-free outcomes

    @Test
    fun non_page_outcomes_carry_only_named_failures() {
        assertEquals(setOf("failure"), NotPermitted::class.memberProperties.map { it.name }.toSet())
        assertEquals(setOf("failure"), ReadFailed::class.memberProperties.map { it.name }.toSet())
        assertEquals(setOf("failure"), InvalidPosition::class.memberProperties.map { it.name }.toSet())
        assertEquals(setOf("refusal"), ProjectionRefused::class.memberProperties.map { it.name }.toSet())
        val outcomes = listOf(
            read(connect(steps("$SENTINEL-1", T0)).apply { recordFailure = IOException("$SENTINEL 77.5 kg") }),
            read(connect(steps("$SENTINEL-2", T0)).apply { recordFailure = SecurityException(SENTINEL) }),
            read(connect(steps("$SENTINEL-3", T0, clientRecordId = "\uD800$SENTINEL"))),
            read(connect(steps("a", T0)), boundaryPosition(T0, listOf(SENTINEL), snapshot = OTHER_SNAPSHOT)),
        )
        for (outcome in outcomes) {
            assertFalse(outcome is Page)
            assertFalse(outcome.toString(), outcome.toString().contains(SENTINEL))
            assertFalse(outcome.toString(), outcome.toString().contains("2026"))
        }
    }

    @Test
    fun every_named_failure_is_reachable_and_in_the_contract() {
        val readOnly = setOf(readPermission(StepsRecord::class))
        val observed = listOf(
            read(connect(steps("bad", T0, clientRecordId = "\uD800"))),
            read(connect(steps("a", T0), granted = setOf(HISTORY))),
            read(connect(steps("a", T0), granted = readOnly)),
            read(connect(steps("a", T0)).apply { recordFailure = SecurityException() }),
            read(connect(steps("a", T0)).apply { recordFailure = IllegalStateException() }),
            read(connect(steps("a", T0)).apply { recordFailure = IOException() }),
            read(connect(steps("a", T0)).apply { recordFailure = IllegalArgumentException() }),
            read(connect(steps("a", T0)).apply { recordFailure = UnsupportedOperationException() }),
            read(connect(steps("a", T0), steps("b", T0 + minutes(1))).apply { respond = { _, records, token -> records.reversed() to token } }),
            read(connect(*(0..HEALTH_CONNECT_READ_LIMIT).map { steps(id(it), T0) }.toTypedArray()), boundaryPosition(T0, (0 until HEALTH_CONNECT_READ_LIMIT).map(::id))),
            read(connect(), "{}".encodeToByteArray()),
            read(connect(), startPosition(snapshot = OTHER_SNAPSHOT)),
        ).map(::classify).toSet()
        val contract = Json.parseToJsonElement(File(repositoryRoot, "governance/scopes/read-snapshot-page.1.json").readText()).jsonObject
        val named = contract["named_failures"]!!.jsonArray
            .map { it.jsonObject["outcome"]!!.jsonPrimitive.content to it.jsonObject["name"]!!.jsonPrimitive.content }
            .toSet()
        assertEquals(named, observed)
    }

    // R13 — Order of checks

    @Test
    fun failures_follow_the_documented_order() {
        val none = emptySet<String>()
        val malformedAndForeign = """{"position_version":1,"record_type":"weight","snapshot_id":"$OTHER_SNAPSHOT"} """.encodeToByteArray()
        connect(steps("a", T0), granted = none).let { hc ->
            assertEquals(InvalidPosition("malformed-position"), read(hc, malformedAndForeign))
            assertTrue(hc.calls.isEmpty())
        }
        connect(steps("a", T0), granted = none).let { hc ->
            assertEquals(InvalidPosition("foreign-position"), read(hc, startPosition(type = "weight")))
            assertTrue(hc.calls.isEmpty())
        }
        val missingBeforeFailingRead = connect(steps("a", T0), granted = none).apply { recordFailure = IOException() }
        assertEquals(NotPermitted("read-permission-missing"), read(missingBeforeFailingRead))
        val lostWithBrokenResponse = connect(steps("a", T0), steps("b", T0 + minutes(1)))
        lostWithBrokenResponse.respond = { _, records, token -> records.reversed() to token }
        lostWithBrokenResponse.afterRead = { lostWithBrokenResponse.granted = lostWithBrokenResponse.granted - HISTORY }
        assertEquals(NotPermitted("history-permission-missing"), read(lostWithBrokenResponse))
        val lostWithRefusal = connect(steps("bad", T0, clientRecordId = "\uD800"))
        lostWithRefusal.afterRead = { lostWithRefusal.granted = none }
        assertEquals(NotPermitted("read-permission-missing"), read(lostWithRefusal))
        val brokenWithRefusal = connect(steps("bad", T0, clientRecordId = "\uD800"), steps("b", T0 + minutes(1)))
        brokenWithRefusal.respond = { _, records, token -> records.reversed() to token }
        assertEquals(ReadFailed("source-order-violation"), read(brokenWithRefusal))
    }

    // Helpers

    private class SyntheticError : Error(SENTINEL)

    private fun boundaryGroupConnection(): SnapshotHealthConnect {
        val boundary = T0 + minutes(998)
        return connect(*distinctSteps(998).toTypedArray(), *(1..4).map { steps("g-$it", boundary) }.toTypedArray())
    }

    private fun classify(result: SnapshotPageResult): Pair<String, String> = when (result) {
        is ProjectionRefused -> "projection-refusal" to "page-projection-refused"
        is NotPermitted -> "not-permitted" to result.failure
        is ReadFailed -> "read-failure" to result.failure
        is InvalidPosition -> "invalid-position" to result.failure
        is Page -> throw AssertionError("expected a failure, got a page")
    }

    private fun assertSamePage(expected: Page, actual: Page) {
        assertEquals(expected.recordType, actual.recordType)
        assertEquals(expected.snapshotId, actual.snapshotId)
        assertEquals(expected.projectionSchemaVersion, actual.projectionSchemaVersion)
        assertEquals(expected.projections.size, actual.projections.size)
        expected.projections.zip(actual.projections).forEach { (a, b) -> assertArrayEquals(a, b) }
        assertArrayEquals(expected.nextPosition, actual.nextPosition)
        assertEquals(expected.exhausted, actual.exhausted)
    }

    private fun traverse(hc: SnapshotHealthConnect, type: String = "steps"): List<Page> {
        val pages = mutableListOf<Page>()
        var position: ByteArray? = null
        repeat(50) {
            val page = read(hc, position, type).page()
            pages += page
            if (page.exhausted) return pages
            position = page.nextPosition
        }
        fail("traversal did not exhaust")
        return pages
    }
}

private val CONTEXT = ProjectionContext("synthetic-implementation-1", "synthetic-provider-1")
private const val SNAPSHOT = "00000000-0000-4000-8000-00000000a001"
private const val OTHER_SNAPSHOT = "00000000-0000-4000-8000-00000000a002"
private const val SENTINEL = "SENTINEL"
private val ALPHA = DataOrigin("org.example.synthetic.alpha")
private val BETA = DataOrigin("org.example.synthetic.beta")
private val T0: Instant = Instant.parse("2026-01-01T07:30:00Z")
private val MODIFIED: Instant = Instant.parse("2026-01-02T00:00:00Z")
private val HISTORY = HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY
private val FULL_RANGE_START: Instant = Instant.ofEpochMilli(Long.MIN_VALUE)
private val FULL_RANGE_END: Instant = Instant.ofEpochMilli(Long.MAX_VALUE)

private val repositoryRoot: File = generateSequence(File(System.getProperty("user.dir")).absoluteFile) { it.parentFile }
    .first { File(it, "governance/scopes/read-snapshot-page.1.json").isFile }

private fun readPermission(type: KClass<out Record>) = HealthPermission.getReadPermission(type)

private fun fullAccess(vararg types: KClass<out Record>): Set<String> =
    (if (types.isEmpty()) listOf(StepsRecord::class) else types.toList()).map(::readPermission).toSet() + HISTORY

private fun connect(vararg records: Record, granted: Set<String> = fullAccess()) =
    SnapshotHealthConnect().apply {
        insert(*records)
        this.granted = granted
    }

// The SDK keeps Metadata's full constructor internal, as for records read from Health Connect.
private val METADATA = Metadata::class.constructors.single { it.parameters.size == 7 }.apply { isAccessible = true }

private fun metadata(id: String, origin: DataOrigin, clientRecordId: String?, device: Device?) =
    METADATA.call(Metadata.RECORDING_METHOD_UNKNOWN, id, origin, MODIFIED, clientRecordId, 0L, device)

private fun steps(
    id: String,
    start: Instant,
    count: Long = 100,
    origin: DataOrigin = ALPHA,
    clientRecordId: String? = null,
    device: Device? = null,
) = StepsRecord(start, null, start.plusSeconds(60), null, count, metadata(id, origin, clientRecordId, device))

private fun StepsRecord.copy(start: Instant = startTime, count: Long = this.count) =
    steps(metadata.id, start, count, metadata.dataOrigin)

private fun weight(id: String, time: Instant) = WeightRecord(time, null, Mass.kilograms(70.0), metadata(id, ALPHA, null, null))

private fun heartRate(id: String, start: Instant) = HeartRateRecord(
    start, null, start.plusSeconds(60), null, listOf(HeartRateRecord.Sample(start, 60)), metadata(id, ALPHA, null, null),
)

private fun id(index: Int) = "r-%05d".format(index)

private fun minutes(count: Int): Duration = Duration.ofMinutes(count.toLong())

private fun distinctSteps(count: Int) = (0 until count).map { steps(id(it), T0 + minutes(it)) }

private fun projected(record: Record): ByteArray =
    (projectSourceRecord(record, CONTEXT) as ProjectionResult.Projected).bytes

private fun refused(record: Record): ProjectionResult.Refused =
    projectSourceRecord(record, CONTEXT) as ProjectionResult.Refused

private fun kind(call: Call) = when (call) {
    Call.Permissions -> "permissions"
    is Call.Records -> "records"
}

private val UTF8_ORDER = Comparator<String> { a, b ->
    val x = a.encodeToByteArray()
    val y = b.encodeToByteArray()
    (0 until minOf(x.size, y.size)).map { (x[it].toInt() and 0xff) - (y[it].toInt() and 0xff) }.firstOrNull { it != 0 }
        ?: (x.size - y.size)
}

private fun startPosition(type: String = "steps", snapshot: String = SNAPSHOT) =
    """{"position_version":1,"record_type":"$type","snapshot_id":"$snapshot"}""".encodeToByteArray()

/** A JSON string as RFC 8785 (ECMAScript JSON.stringify) writes it. */
private fun jsonString(value: String): String = buildString {
    append('"')
    for (char in value) {
        when (char) {
            '"' -> append("\\\"")
            '\\' -> append("\\\\")
            '\b' -> append("\\b")
            '\u000C' -> append("\\f")
            '\n' -> append("\\n")
            '\r' -> append("\\r")
            '\t' -> append("\\t")
            else -> if (char < ' ') append("\\u%04x".format(char.code)) else append(char)
        }
    }
    append('"')
}

/** Record type names are SDK class names without `Record`, in kebab case (project-source-record@1). */
private fun sdkClassNamed(type: String): KClass<out Record> {
    val name = type.split("-").joinToString("") { part -> part.replaceFirstChar(Char::uppercaseChar) } + "Record"
    @Suppress("UNCHECKED_CAST")
    return Class.forName("androidx.health.connect.client.records.$name").kotlin as KClass<out Record>
}

private fun boundaryPosition(start: Instant, ids: Collection<String>, type: String = "steps", snapshot: String = SNAPSHOT): ByteArray {
    val sorted = ids.sortedWith(UTF8_ORDER).joinToString(",", transform = ::jsonString)
    return """{"boundary_record_ids":[$sorted],"boundary_start":"$start","position_version":1,"record_type":"$type","snapshot_id":"$snapshot"}"""
        .encodeToByteArray()
}

private fun projectionId(projection: ByteArray): String =
    Json.parseToJsonElement(projection.decodeToString()).jsonObject["metadata"]!!.jsonObject["id"]!!.jsonPrimitive.content

private fun Page.ids(): List<String> = projections.map(::projectionId)

private fun SnapshotPageResult.page(): Page = this as? Page ?: throw AssertionError("expected a page, got $this")

private fun read(
    hc: SnapshotHealthConnect,
    position: ByteArray? = null,
    type: String = "steps",
    snapshot: String = SNAPSHOT,
): SnapshotPageResult = runSuspending { readSnapshotPage(hc, SnapshotPageRequest(type, snapshot, position, CONTEXT)) }

/** Runs a suspending read to completion on any thread the implementation chooses. */
private fun <T> runSuspending(block: suspend () -> T): T {
    val done = CountDownLatch(1)
    var outcome: Result<T>? = null
    block.startCoroutine(Continuation(EmptyCoroutineContext) { result ->
        outcome = result
        done.countDown()
    })
    check(done.await(120, TimeUnit.SECONDS)) { "snapshot page read did not complete" }
    return outcome!!.getOrThrow()
}
