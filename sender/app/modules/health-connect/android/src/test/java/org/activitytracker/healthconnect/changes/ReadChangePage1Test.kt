package org.activitytracker.healthconnect.changes

import android.os.RemoteException
import androidx.health.connect.client.changes.Change
import androidx.health.connect.client.changes.DeletionChange
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.response.ChangesResponse
import androidx.health.connect.client.units.Mass
import java.io.File
import java.io.IOException
import java.time.Clock
import java.time.Duration
import java.time.Instant
import java.time.ZoneOffset
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
import org.activitytracker.healthconnect.changes.ChangeHealthConnect.Call
import org.activitytracker.healthconnect.changes.ChangeItem.Projection
import org.activitytracker.healthconnect.changes.ChangeItem.Retraction
import org.activitytracker.healthconnect.changes.ChangePageResult.NotPermitted
import org.activitytracker.healthconnect.changes.ChangePageResult.Page
import org.activitytracker.healthconnect.changes.ChangePageResult.ProjectionRefused
import org.activitytracker.healthconnect.changes.ChangePageResult.ReadFailed
import org.activitytracker.healthconnect.changes.ChangePageResult.TokenExpired
import org.activitytracker.healthconnect.projection.PROJECTION_SCHEMA_VERSIONS
import org.activitytracker.healthconnect.projection.ProjectionContext
import org.activitytracker.healthconnect.projection.ProjectionResult
import org.activitytracker.healthconnect.projection.projectSourceRecord
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Acceptance tests for read-change-page@1. Requirement numbers refer to
 * packs/read-change-page-1.spec.md. Health Connect is the synthetic
 * ChangeHealthConnect; expected bytes come from project-source-record@1.
 */
class ReadChangePage1Test {
    // R2 — Request validation

    @Test
    fun unknown_record_type_is_a_caller_error() {
        for (type in listOf("", "Steps", "steps-record", "medical-resource", "exercise-route", "steps ")) {
            assertFalse(type in PROJECTION_SCHEMA_VERSIONS)
            assertThrows(type, IllegalArgumentException::class.java) { ChangePageRequest(type, CYCLE, "1", T0, CONTEXT) }
        }
        ChangePageRequest("steps", CYCLE, "1", T0, CONTEXT)
    }

    @Test
    fun malformed_cycle_identity_is_a_caller_error() {
        val malformed = listOf(
            "",
            "00000000-0000-4000-8000-00000000C001",
            "000000000000400080000000000000c001",
            "00000000-0000-4000-8000-00000000c0011",
            "{00000000-0000-4000-8000-00000000c001}",
            " 00000000-0000-4000-8000-00000000c001",
        )
        for (cycle in malformed) {
            assertThrows(cycle, IllegalArgumentException::class.java) { ChangePageRequest("steps", cycle, "1", T0, CONTEXT) }
        }
        ChangePageRequest("steps", OTHER_CYCLE, "1", T0, CONTEXT)
    }

    @Test
    fun empty_changes_token_is_a_caller_error() {
        assertThrows(IllegalArgumentException::class.java) { ChangePageRequest("steps", CYCLE, "", T0, CONTEXT) }
        ChangePageRequest("steps", CYCLE, " ", T0, CONTEXT)
        ChangePageRequest("steps", CYCLE, "not-a-token", T0, CONTEXT)
    }

    // R3 — Token age

    @Test
    fun a_token_at_the_age_limit_is_refused_without_reading() {
        assertEquals(Duration.ofDays(30), CHANGES_TOKEN_MAX_AGE)
        for (age in listOf(CHANGES_TOKEN_MAX_AGE, CHANGES_TOKEN_MAX_AGE.plusMillis(1), Duration.ofDays(400))) {
            val start = connect()
            start.hc.upsert(steps("a", T0))
            assertEquals(age.toString(), TokenExpired("changes-token-too-old"), start.read(at = start.unreadSince + age))
            assertTrue(start.hc.calls.isEmpty())
        }
    }

    @Test
    fun a_token_under_the_age_limit_is_read() {
        val start = connect()
        start.hc.upsert(steps("a", T0))
        val page = start.read(at = start.unreadSince + CHANGES_TOKEN_MAX_AGE - Duration.ofMillis(1)).page()
        assertEquals(listOf("project a"), describe(page.items))
    }

    @Test
    fun a_clock_behind_the_token_beyond_tolerance_is_refused_without_reading() {
        assertEquals(Duration.ofDays(1), CLOCK_BACKWARD_TOLERANCE)
        for (behind in listOf(CLOCK_BACKWARD_TOLERANCE.plusMillis(1), Duration.ofDays(365))) {
            val start = connect()
            start.hc.upsert(steps("a", T0))
            assertEquals(behind.toString(), TokenExpired("clock-behind-changes-token"), start.read(at = start.unreadSince - behind))
            assertTrue(start.hc.calls.isEmpty())
        }
    }

    @Test
    fun a_clock_behind_the_token_within_tolerance_is_read() {
        for (behind in listOf(Duration.ZERO, Duration.ofSeconds(1), CLOCK_BACKWARD_TOLERANCE)) {
            val start = connect()
            start.hc.upsert(steps("a", T0))
            assertEquals(behind.toString(), listOf("project a"), describe(start.read(at = start.unreadSince - behind).page().items))
        }
    }

    @Test
    fun the_fake_drops_changes_behind_a_stale_next_page_token() {
        val start = connect()
        val hc = start.hc
        hc.upsert(steps("a", T0))
        val firstReadAt = hc.now
        val next = runSuspending { hc.getChanges(start.token) }.nextChangesToken
        assertNotEquals(start.token, next)
        hc.advance(DAY)
        hc.upsert(steps("lost", T0 + minutes(1)))
        hc.advance(Duration.ofDays(33))
        hc.upsert(steps("kept", T0 + minutes(2)))
        // Cleanup expired the issued token but spared the next-page token, which now skips "lost".
        assertTrue(runSuspending { hc.getChanges(start.token) }.changesTokenExpired)
        val stale = runSuspending { hc.getChanges(next) }
        assertFalse(stale.changesTokenExpired)
        assertEquals(listOf("kept"), stale.changes.map { (it as UpsertionChange).record.metadata.id })
        hc.calls.clear()
        assertEquals(TokenExpired("changes-token-too-old"), read(hc, next, firstReadAt))
        assertTrue(hc.calls.isEmpty())
    }

    // R4 — Full-history permission, checked around the read

    @Test
    fun missing_read_permission_reads_no_changes() {
        for (granted in listOf(setOf(HISTORY, readPermission(WeightRecord::class)), emptySet())) {
            val start = connect(granted = granted)
            start.hc.upsert(steps("a", T0))
            assertEquals(NotPermitted("read-permission-missing"), start.read())
            assertTrue(start.hc.changeTokens.isEmpty())
        }
    }

    @Test
    fun missing_history_permission_reads_no_changes() {
        val start = connect(granted = setOf(readPermission(StepsRecord::class)))
        start.hc.upsert(steps("old", Instant.parse("2020-06-01T07:30:00Z")))
        // The synthetic provider silently omits this update, as Health Connect does.
        assertTrue(runSuspending { start.hc.getChanges(start.token) }.changes.isEmpty())
        start.hc.calls.clear()
        assertEquals(NotPermitted("history-permission-missing"), start.read())
        assertTrue(start.hc.changeTokens.isEmpty())
    }

    @Test
    fun permissions_are_read_before_and_after_the_changes() {
        val start = connect()
        start.hc.upsert(steps("a", T0))
        start.read().page()
        assertEquals(listOf("permissions", "changes", "permissions"), start.hc.calls.map(::kind))
    }

    @Test
    fun permission_lost_during_the_read_discards_the_page() {
        for ((lost, failure) in listOf(readPermission(StepsRecord::class) to "read-permission-missing", HISTORY to "history-permission-missing")) {
            val start = connect()
            start.hc.upsert(steps("a", T0))
            start.hc.afterRead = { start.hc.granted = start.hc.granted - lost }
            assertEquals(NotPermitted(failure), start.read())
            assertEquals(listOf("permissions", "changes", "permissions"), start.hc.calls.map(::kind))
        }
    }

    @Test
    fun security_exception_is_read_denied() {
        val fromChanges = connect().apply { hc.changesFailure = SecurityException("synthetic") }
        assertEquals(NotPermitted("read-denied"), fromChanges.read())
        val fromPermissions = connect().apply { hc.permissionFailure = SecurityException("synthetic") }
        assertEquals(NotPermitted("read-denied"), fromPermissions.read())
        assertTrue(fromPermissions.hc.changeTokens.isEmpty())
        // Revoked between the permission read and the change read: the provider itself denies it.
        val revoked = connect()
        revoked.hc.upsert(steps("a", T0))
        revoked.hc.duringRead = { revoked.hc.granted = revoked.hc.granted - readPermission(StepsRecord::class) }
        assertEquals(NotPermitted("read-denied"), revoked.read())
    }

    // R5 — The read and its expiry

    @Test
    fun one_change_read_uses_the_request_token() {
        val start = connect()
        start.hc.upsert(steps("a", T0))
        start.read().page()
        assertEquals(listOf(start.token), start.hc.changeTokens)
    }

    @Test
    fun health_connect_expiry_is_token_expired() {
        // Cleanup deleted the issued token; the caller's unread-since instant is inside the limit.
        val cleaned = connect()
        cleaned.hc.advance(Duration.ofDays(33))
        assertEquals(TokenExpired("changes-token-expired"), read(cleaned.hc, cleaned.token, cleaned.hc.now))
        val reported = connect()
        reported.hc.respond = { _, _ -> ChangesResponse(emptyList(), "", false, true) }
        assertEquals(TokenExpired("changes-token-expired"), reported.read())
    }

    @Test
    fun expiry_wins_over_other_response_fields() {
        val bodies = listOf<Triple<List<Change>, String, Boolean>>(
            Triple(listOf(UpsertionChange(steps("a", T0)), DeletionChange("b")), "77", true),
            Triple(listOf(DeletionChange("")), "", false),
            Triple(listOf(UpsertionChange(weight("w", T0)), object : Change {}), "1", true),
        )
        for ((changes, next, hasMore) in bodies) {
            val start = connect()
            start.hc.respond = { _, _ -> ChangesResponse(changes, next, hasMore, true) }
            assertEquals(TokenExpired("changes-token-expired"), start.read())
        }
    }

    @Test
    fun an_unknown_token_reads_as_expired() {
        val start = connect()
        start.hc.upsert(steps("a", T0))
        for (token in listOf("999999", "not-a-token", " ", "-1", "${start.token}0")) {
            assertEquals(token, TokenExpired("changes-token-expired"), read(start.hc, token, start.unreadSince))
        }
    }

    // R6 — Items and their order

    @Test
    fun retractions_precede_projections() {
        val start = connect(steps("a", T0), steps("b", T0 + minutes(1)), steps("c", T0 + minutes(2)))
        start.hc.upsert(steps("d", T0 + minutes(3)))
        start.hc.delete("a")
        start.hc.upsert(steps("b", T0 + minutes(1), count = 200))
        start.hc.delete("c")
        val items = describe(start.read().page().items)
        assertEquals(listOf("retract a", "retract c"), items.take(2))
        assertEquals(setOf("project b", "project d"), items.drop(2).toSet())
        assertEquals(4, items.size)
    }

    @Test
    fun each_kind_keeps_health_connect_order() {
        val upserted = listOf(steps("p-3", T0), steps("p-1", T0 + minutes(1)), steps("p-2", T0 + minutes(2)))
        val scripted = connect()
        scripted.hc.respond = { _, response ->
            val changes = listOf(
                UpsertionChange(upserted[0]), DeletionChange("d-2"), UpsertionChange(upserted[1]),
                DeletionChange("d-3"), DeletionChange("d-1"), UpsertionChange(upserted[2]),
            )
            ChangesResponse(changes, response.nextChangesToken, false, false)
        }
        assertEquals(
            listOf("retract d-2", "retract d-3", "retract d-1", "project p-3", "project p-1", "project p-2"),
            describe(scripted.read().page().items),
        )
        // The fake scrambles upsertions; the page keeps whatever order Health Connect gave.
        val scrambled = connect()
        scrambled.hc.upsert(*distinctSteps(40).toTypedArray())
        val page = scrambled.read().page()
        val answered = scrambled.hc.answers.single().changes.map { "project " + (it as UpsertionChange).record.metadata.id }
        assertNotEquals(answered.sorted(), answered)
        assertEquals(answered, describe(page.items))
    }

    @Test
    fun repeated_deletions_retract_once() {
        val start = connect(steps("x", T0), steps("y", T0 + minutes(1)))
        start.hc.delete("x")
        start.hc.upsert(steps("x", T0, count = 5))
        start.hc.delete("x", "y")
        assertEquals(listOf("retract x", "retract y"), describe(start.read().page().items))
        val scripted = connect()
        scripted.hc.respond = { _, response ->
            val changes = listOf(DeletionChange("y"), DeletionChange("x"), DeletionChange("y"), DeletionChange("x"))
            ChangesResponse(changes, response.nextChangesToken, false, false)
        }
        assertEquals(listOf("retract y", "retract x"), describe(scripted.read().page().items))
    }

    @Test
    fun a_reused_identifier_is_retracted_then_projected() {
        val start = connect(steps("a", T0))
        start.hc.delete("a")
        val again = steps("a", T0 + minutes(5), count = 321)
        start.hc.upsert(again)
        val page = start.read().page()
        assertEquals(listOf("retract a", "project a"), describe(page.items))
        assertArrayEquals(projected(again), (page.items[1] as Projection).bytes)
        // In the SDK's own order the upsertion comes first, so applying it as given would end deleted.
        assertTrue(start.hc.answers.single().changes.first() is UpsertionChange)
    }

    @Test
    fun projection_bytes_equal_the_shared_projection() {
        val records = listOf(
            steps("alpha", T0, origin = ALPHA, clientRecordId = "client-1"),
            steps("beta", T0 + minutes(1), count = 4321, origin = BETA),
        )
        val start = connect()
        start.hc.upsert(*records.toTypedArray())
        val page = start.read().page()
        assertEquals("steps", page.recordType)
        assertEquals(CYCLE, page.cycleId)
        assertEquals(PROJECTION_SCHEMA_VERSIONS.getValue("steps"), page.projectionSchemaVersion)
        assertProjects(records, page.items)
        for ((type, record) in listOf("weight" to weight("w", T0), "heart-rate" to heartRate("h", T0))) {
            val hc = ChangeHealthConnect().apply { granted = fullAccess(record::class) }
            val since = hc.now
            val token = hc.issueToken(record::class)
            hc.upsert(record)
            val other = read(hc, token, since, type = type, cycle = OTHER_CYCLE).page()
            assertEquals(type, other.recordType)
            assertEquals(OTHER_CYCLE, other.cycleId)
            assertEquals(PROJECTION_SCHEMA_VERSIONS.getValue(type), other.projectionSchemaVersion)
            assertProjects(listOf(record), other.items)
        }
    }

    @Test
    fun one_unprojectable_record_refuses_the_whole_page() {
        val bad = steps("bad", T0, clientRecordId = "\uD800")
        val start = connect(steps("gone", T0))
        start.hc.delete("gone")
        start.hc.upsert(steps("good", T0 + minutes(1)), bad, steps("also-good", T0 + minutes(2)))
        assertEquals(ProjectionRefused(refused(bad)), start.read())
    }

    @Test
    fun refusal_is_the_first_in_projection_order() {
        val first = steps("first", T0, clientRecordId = "\uD800")
        val second = steps("second", T0, origin = DataOrigin("\uD800"))
        assertNotEquals(refused(first), refused(second))
        for (order in listOf(listOf(first, second), listOf(second, first))) {
            val start = connect()
            start.hc.respond = { _, response ->
                val changes = listOf(UpsertionChange(steps("fine", T0))) + order.map(::UpsertionChange) + DeletionChange("gone")
                ChangesResponse(changes, response.nextChangesToken, false, false)
            }
            assertEquals(ProjectionRefused(refused(order.first())), start.read())
        }
    }

    // R7 — Next token, continuation, and unread-since

    @Test
    fun next_token_and_continuation_come_from_health_connect() {
        val start = connect()
        start.hc.upsert(*distinctSteps(1000).toTypedArray())
        start.hc.upsert(*(1000 until 1500).map { steps(id(it), T0 + minutes(it)) }.toTypedArray())
        val first = start.read().page()
        assertTrue(first.hasMore)
        assertEquals(start.hc.answers.last().nextChangesToken, first.nextChangesToken)
        assertNotEquals(start.token, first.nextChangesToken)
        assertEquals(1000, first.items.size)
        val second = read(start.hc, first.nextChangesToken, first.nextUnreadSince).page()
        assertFalse(second.hasMore)
        assertEquals(start.hc.answers.last().nextChangesToken, second.nextChangesToken)
        assertEquals(500, second.items.size)
        val scripted = connect()
        scripted.hc.respond = { _, _ -> ChangesResponse(emptyList(), "scripted-next", true, false) }
        val page = scripted.read().page()
        assertEquals("scripted-next", page.nextChangesToken)
        assertTrue(page.hasMore)
    }

    @Test
    fun unread_since_carries_through_a_cycle() {
        val start = connect()
        start.hc.upsert(*distinctSteps(1000).toTypedArray())
        start.hc.upsert(*(1000 until 2000).map { steps(id(it), T0 + minutes(it)) }.toTypedArray())
        start.hc.upsert(steps("last", T0))
        val at = start.unreadSince + Duration.ofDays(12)
        val first = start.read(at = at).page()
        assertTrue(first.hasMore)
        assertEquals(start.unreadSince, first.nextUnreadSince)
        val second = read(start.hc, first.nextChangesToken, first.nextUnreadSince, at = at + Duration.ofHours(1)).page()
        assertTrue(second.hasMore)
        assertEquals(start.unreadSince, second.nextUnreadSince)
    }

    @Test
    fun unread_since_restarts_when_no_page_remains() {
        val start = connect()
        start.hc.upsert(*distinctSteps(1000).toTypedArray())
        start.hc.upsert(*(1000 until 1500).map { steps(id(it), T0 + minutes(it)) }.toTypedArray())
        val at = start.unreadSince + Duration.ofDays(3)
        val first = start.read(at = at).page()
        val finalAt = at + Duration.ofMinutes(7)
        val last = read(start.hc, first.nextChangesToken, first.nextUnreadSince, at = finalAt).page()
        assertFalse(last.hasMore)
        assertEquals(finalAt, last.nextUnreadSince)
        val quietAt = finalAt + Duration.ofDays(2)
        val quiet = read(start.hc, last.nextChangesToken, last.nextUnreadSince, at = quietAt).page()
        assertTrue(quiet.items.isEmpty())
        assertEquals(quietAt, quiet.nextUnreadSince)
    }

    @Test
    fun rereading_a_token_returns_identical_items() {
        val start = connect(steps("a", T0), steps("b", T0 + minutes(1)))
        start.hc.upsert(steps("c", T0 + minutes(2)), steps("a", T0, count = 7))
        start.hc.delete("b")
        val first = start.read().page()
        val second = start.read().page()
        assertEquals(describe(first.items), describe(second.items))
        first.items.zip(second.items).forEach { (x, y) -> if (x is Projection) assertArrayEquals(x.bytes, (y as Projection).bytes) }
        assertEquals(first.hasMore, second.hasMore)
    }

    @Test
    fun no_change_returns_an_empty_final_page() {
        val start = connect(steps("a", T0))
        start.hc.upsert(weight("w", T0))
        val page = start.read().page()
        assertTrue(page.items.isEmpty())
        assertFalse(page.hasMore)
        assertEquals(start.token, page.nextChangesToken)
        assertEquals("steps", page.recordType)
        assertEquals(CYCLE, page.cycleId)
        assertEquals(PROJECTION_SCHEMA_VERSIONS.getValue("steps"), page.projectionSchemaVersion)
    }

    // R8 — Convergence

    @Test
    fun replica_converges_under_concurrent_change() {
        for (seed in 1..12) {
            val large = seed > 8
            val random = Random(seed)
            val baseline = (0 until if (large) 1500 else 20).map { steps(id(it), T0 + minutes(it), count = 1) }
            val start = connect(*baseline.toTypedArray())
            val hc = start.hc
            val replica = baseline.associate { it.metadata.id to projected(it).decodeToString() }.toMutableMap()
            var nextId = baseline.size
            val everDeleted = mutableSetOf<String>()
            fun live() = hc.current(StepsRecord::class).map { it.metadata.id }
            fun mutate() {
                repeat(random.nextInt(1, 4)) {
                    val ids = live()
                    when (random.nextInt(5)) {
                        0 -> hc.upsert(*(0 until random.nextInt(1, if (large) 1200 else 4)).map {
                            steps(id(nextId), T0 + minutes(nextId++), count = random.nextLong(1, 1000))
                        }.toTypedArray())
                        1 -> if (ids.isNotEmpty()) hc.upsert(*ids.shuffled(random).take(random.nextInt(1, 6)).map {
                            steps(it, T0 + minutes(random.nextInt(0, 5000)), count = random.nextLong(1, 1000))
                        }.toTypedArray())
                        2 -> if (ids.isNotEmpty()) ids.shuffled(random).take(random.nextInt(1, 6)).toTypedArray().let {
                            everDeleted += it
                            hc.delete(*it)
                        }
                        3 -> (everDeleted - ids.toSet()).shuffled(random).take(random.nextInt(1, 4)).let { reused ->
                            if (reused.isNotEmpty()) hc.upsert(*reused.map { steps(it, T0 + minutes(random.nextInt(0, 5000)), count = random.nextLong(1, 1000)) }.toTypedArray())
                        }
                        else -> if (ids.isNotEmpty()) ids.random(random).let {
                            everDeleted += it
                            hc.delete(it)
                            hc.upsert(steps(it, T0 + minutes(random.nextInt(0, 5000)), count = random.nextLong(1, 1000)))
                        }
                    }
                }
            }
            if (large) hc.upsert(*(0 until 1200).map { steps(id(nextId), T0 + minutes(nextId++)) }.toTypedArray())
            mutate()
            val mutatingReads = if (large) 6 else 3
            hc.duringRead = { if (it <= mutatingReads && random.nextBoolean()) mutate() }
            hc.betweenLogAndRecords = { if (it <= mutatingReads && random.nextBoolean()) mutate() }
            hc.afterRead = { if (it <= mutatingReads && random.nextBoolean()) mutate() }
            var token = start.token
            var since = start.unreadSince
            var pages = 0
            var spanned = false
            while (true) {
                val page = read(hc, token, since).page()
                pages++
                spanned = spanned || page.hasMore
                replica.apply(page)
                token = page.nextChangesToken
                since = page.nextUnreadSince
                if (!page.hasMore && hc.changeTokens.size > mutatingReads) break
                assertTrue("seed $seed did not finish", pages < 500)
            }
            val expected = hc.current(StepsRecord::class).associate { it.metadata.id to projected(it).decodeToString() }
            assertEquals("seed $seed", expected, replica)
            if (large) assertTrue("seed $seed spans pages", spanned)
        }
    }

    @Test
    fun a_record_deleted_after_its_upsert_is_only_retracted() {
        val start = connect()
        start.hc.upsert(steps("x", T0))
        start.hc.delete("x")
        assertEquals(listOf("retract x"), describe(start.read().page().items))
        // Deleted after the page's rows were read: no projection now, the retraction on a later page.
        val racing = connect()
        racing.hc.upsert(steps("y", T0))
        racing.hc.betweenLogAndRecords = { if (racing.hc.contains("y")) racing.hc.delete("y") }
        val first = racing.read().page()
        assertTrue(first.items.isEmpty())
        val second = read(racing.hc, first.nextChangesToken, first.nextUnreadSince).page()
        assertEquals(listOf("retract y"), describe(second.items))
    }

    // R9 — Page size

    @Test
    fun a_page_may_exceed_one_thousand_items() {
        val start = connect()
        for (index in 0 until 999) start.hc.upsert(steps(id(index), T0 + minutes(index)))
        start.hc.upsert(*(999 until 1999).map { steps(id(it), T0 + minutes(it)) }.toTypedArray())
        val page = start.read().page()
        assertEquals(1999, page.items.size)
        assertFalse(page.hasMore)
    }

    @Test
    fun an_empty_page_with_more_remaining_is_a_page() {
        val start = connect()
        val batch = distinctSteps(1000)
        start.hc.upsert(*batch.toTypedArray())
        start.hc.delete(*batch.map { it.metadata.id }.toTypedArray())
        val first = start.read().page()
        assertTrue(first.items.isEmpty())
        assertTrue(first.hasMore)
        val second = read(start.hc, first.nextChangesToken, first.nextUnreadSince).page()
        assertEquals(batch.map { "retract ${it.metadata.id}" }, describe(second.items))
        assertFalse(second.hasMore)
    }

    // R10 — Response validation

    @Test
    fun upserted_records_of_another_type_are_violations() {
        assertEquals(VIOLATION, answered { _, r -> r.with(r.changes + UpsertionChange(weight("w", T0))) })
        assertEquals(VIOLATION, answered { _, r -> r.with(listOf(UpsertionChange(heartRate("h", T0)))) })
    }

    @Test
    fun duplicate_upserted_identifiers_are_violations() {
        assertEquals(VIOLATION, answered { _, r -> r.with(r.changes + UpsertionChange(steps("a", T0, count = 9))) })
        assertEquals(VIOLATION, answered { _, r -> r.with(r.changes + r.changes) })
    }

    @Test
    fun deleted_identifiers_outside_the_source_identity_form_are_violations() {
        val invalid = listOf("", "x".repeat(257), "\uD83D\uDE00".repeat(257), "\u0000", "a\u001f", "\n", "\uD800", "a\uDC00b")
        for (id in invalid) {
            assertEquals(id.length.toString(), VIOLATION, answered { _, r -> r.with(r.changes + DeletionChange(id)) })
        }
        val valid = listOf("x".repeat(256), "\uD83D\uDE00".repeat(256), "\u007f", " ", "\u00e9t\u00e9", "00000000-0000-4000-8000-000000000001")
        for (id in valid) {
            val page = answered { _, r -> r.with(r.changes + DeletionChange(id)) }.page()
            assertEquals(id.length.toString(), Retraction(id), page.items.first())
        }
    }

    @Test
    fun unknown_change_kinds_are_violations() {
        assertEquals(VIOLATION, answered { _, r -> r.with(r.changes + object : Change {}) })
    }

    @Test
    fun empty_next_tokens_are_violations() {
        for (hasMore in listOf(false, true)) {
            assertEquals(VIOLATION, answered { _, r -> ChangesResponse(r.changes, "", hasMore, false) })
        }
    }

    @Test
    fun more_pages_without_a_new_token_are_violations() {
        assertEquals(VIOLATION, answered { token, r -> ChangesResponse(r.changes, token, true, false) })
    }

    // R11 — Failure classification

    @Test
    fun each_exception_class_maps_to_one_named_failure() {
        val cases = listOf<Pair<() -> Exception, ChangePageResult>>(
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
            val fromChanges = connect().apply { hc.changesFailure = exception() }
            assertEquals("changes: ${exception()::class.simpleName}", expected, fromChanges.read())
            val fromPermissions = connect().apply { hc.permissionFailure = exception() }
            assertEquals("permissions: ${exception()::class.simpleName}", expected, fromPermissions.read())
            assertTrue(fromPermissions.hc.changeTokens.isEmpty())
            val secondCheck = connect().apply { hc.afterRead = { hc.permissionFailure = exception() } }
            assertEquals("permissions after the read: ${exception()::class.simpleName}", expected, secondCheck.read())
        }
    }

    @Test
    fun cancellation_propagates() {
        val cancelled = CancellationException(SENTINEL)
        val sites = listOf<(ChangeHealthConnect) -> Unit>(
            { it.changesFailure = cancelled },
            { it.permissionFailure = cancelled },
            { hc -> hc.afterRead = { hc.permissionFailure = cancelled } },
        )
        for (site in sites) {
            val start = connect()
            site(start.hc)
            assertSame(cancelled, assertThrows(CancellationException::class.java) { start.read() })
        }
        val error = SyntheticError()
        val start = connect().apply { hc.changesFailure = error }
        assertSame(error, assertThrows(SyntheticError::class.java) { start.read() })
    }

    // R12 — Health-data-free outcomes

    @Test
    fun non_page_outcomes_carry_only_named_failures() {
        assertEquals(setOf("failure"), TokenExpired::class.memberProperties.map { it.name }.toSet())
        assertEquals(setOf("failure"), NotPermitted::class.memberProperties.map { it.name }.toSet())
        assertEquals(setOf("failure"), ReadFailed::class.memberProperties.map { it.name }.toSet())
        assertEquals(setOf("refusal"), ProjectionRefused::class.memberProperties.map { it.name }.toSet())
        val secret = connect()
        val outcomes = listOf(
            connect().apply { hc.changesFailure = IOException("$SENTINEL 77.5 kg") }.read(),
            connect().apply { hc.changesFailure = SecurityException(SENTINEL) }.read(),
            connect().apply { hc.upsert(steps("$SENTINEL-1", T0, clientRecordId = "\uD800$SENTINEL")) }.read(),
            read(secret.hc, "$SENTINEL-token", secret.unreadSince),
            read(secret.hc, "$SENTINEL-token", secret.unreadSince, at = secret.unreadSince + Duration.ofDays(2026)),
            answered { _, r -> r.with(r.changes + DeletionChange("$SENTINEL\u0000")) },
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
        val stale = connect()
        val observed = listOf(
            read(stale.hc, "999999", stale.unreadSince),
            stale.read(at = stale.unreadSince + CHANGES_TOKEN_MAX_AGE),
            stale.read(at = stale.unreadSince - Duration.ofDays(2)),
            connect().apply { hc.upsert(steps("bad", T0, clientRecordId = "\uD800")) }.read(),
            connect(granted = setOf(HISTORY)).read(),
            connect(granted = readOnly).read(),
            connect().apply { hc.changesFailure = SecurityException() }.read(),
            connect().apply { hc.changesFailure = IllegalStateException() }.read(),
            connect().apply { hc.changesFailure = IOException() }.read(),
            connect().apply { hc.changesFailure = IllegalArgumentException() }.read(),
            connect().apply { hc.changesFailure = UnsupportedOperationException() }.read(),
            answered { _, r -> r.with(r.changes + object : Change {}) },
        ).map(::classify).toSet()
        val contract = Json.parseToJsonElement(File(repositoryRoot, "governance/scopes/read-change-page.1.json").readText()).jsonObject
        val named = contract["named_failures"]!!.jsonArray
            .map { it.jsonObject["outcome"]!!.jsonPrimitive.content to it.jsonObject["name"]!!.jsonPrimitive.content }
            .toSet()
        assertEquals(named, observed)
    }

    // R13 — Order of checks

    @Test
    fun failures_follow_the_documented_order() {
        val none = emptySet<String>()
        connect(granted = none).let { start ->
            start.hc.changesFailure = IOException()
            assertEquals(TokenExpired("changes-token-too-old"), start.read(at = start.unreadSince + CHANGES_TOKEN_MAX_AGE))
            assertEquals(TokenExpired("clock-behind-changes-token"), start.read(at = start.unreadSince - Duration.ofDays(2)))
            assertTrue(start.hc.calls.isEmpty())
        }
        val missingBeforeFailingRead = connect(granted = none).apply { hc.changesFailure = IOException() }
        assertEquals(NotPermitted("read-permission-missing"), missingBeforeFailingRead.read())
        val missingBeforeExpiry = connect(granted = none)
        assertEquals(NotPermitted("read-permission-missing"), read(missingBeforeExpiry.hc, "999999", missingBeforeExpiry.unreadSince))
        val lostWithExpiry = connect()
        lostWithExpiry.hc.respond = { _, _ -> ChangesResponse(emptyList(), "", false, true) }
        lostWithExpiry.hc.afterRead = { lostWithExpiry.hc.granted = lostWithExpiry.hc.granted - HISTORY }
        assertEquals(NotPermitted("history-permission-missing"), lostWithExpiry.read())
        val lostWithViolation = connect()
        lostWithViolation.hc.respond = { _, r -> r.with(r.changes + object : Change {}) }
        lostWithViolation.hc.afterRead = { lostWithViolation.hc.granted = none }
        assertEquals(NotPermitted("read-permission-missing"), lostWithViolation.read())
        val expiredWithViolation = connect()
        expiredWithViolation.hc.respond = { _, _ -> ChangesResponse(listOf(object : Change {}, DeletionChange("")), "", true, true) }
        assertEquals(TokenExpired("changes-token-expired"), expiredWithViolation.read())
        val violationWithRefusal = connect()
        violationWithRefusal.hc.upsert(steps("bad", T0, clientRecordId = "\uD800"))
        violationWithRefusal.hc.respond = { _, r -> r.with(r.changes + DeletionChange("\u0000")) }
        assertEquals(VIOLATION, violationWithRefusal.read())
    }

    // Helpers

    private class SyntheticError : Error(SENTINEL)

    /** A steps page read whose Health Connect answer [build] replaces. */
    private fun answered(build: (token: String, response: ChangesResponse) -> ChangesResponse): ChangePageResult {
        val start = connect()
        start.hc.upsert(steps("a", T0))
        start.hc.respond = build
        return start.read()
    }

    private fun classify(result: ChangePageResult): Pair<String, String> = when (result) {
        is TokenExpired -> "token-expired" to result.failure
        is ProjectionRefused -> "projection-refusal" to "page-projection-refused"
        is NotPermitted -> "not-permitted" to result.failure
        is ReadFailed -> "read-failure" to result.failure
        is Page -> throw AssertionError("expected a failure, got a page")
    }

    private fun assertProjects(records: List<Record>, items: List<ChangeItem>) {
        val bytes = items.map { (it as Projection).bytes }.associateBy(::projectionId)
        assertEquals(records.map { it.metadata.id }.toSet(), bytes.keys)
        for (record in records) assertArrayEquals(projected(record), bytes.getValue(record.metadata.id))
    }
}

/** A Health Connect holding a baseline, and a steps token issued after it. */
private class Start(val hc: ChangeHealthConnect, val token: String, val unreadSince: Instant) {
    fun read(at: Instant = hc.now): ChangePageResult = read(hc, token, unreadSince, at)
}

private val CONTEXT = ProjectionContext("synthetic-implementation-1", "synthetic-provider-1")
private const val CYCLE = "00000000-0000-4000-8000-00000000c001"
private const val OTHER_CYCLE = "00000000-0000-4000-8000-00000000c002"
private const val SENTINEL = "SENTINEL"
private val ALPHA = DataOrigin("org.example.synthetic.alpha")
private val BETA = DataOrigin("org.example.synthetic.beta")
private val T0: Instant = Instant.parse("2026-01-01T07:30:00Z")
private val MODIFIED: Instant = Instant.parse("2026-01-02T00:00:00Z")
private val HISTORY = HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY
private val DAY: Duration = Duration.ofDays(1)
private val VIOLATION = ReadFailed("source-contract-violation")

private val repositoryRoot: File = generateSequence(File(System.getProperty("user.dir")).absoluteFile) { it.parentFile }
    .first { File(it, "governance/scopes/read-change-page.1.json").isFile }

private fun readPermission(type: KClass<out Record>) = HealthPermission.getReadPermission(type)

private fun fullAccess(vararg types: KClass<out Record>): Set<String> =
    (if (types.isEmpty()) listOf(StepsRecord::class) else types.toList()).map(::readPermission).toSet() + HISTORY

/** Writes [baseline] in one write, then issues a steps token, recording the time read before it. */
private fun connect(vararg baseline: Record, granted: Set<String> = fullAccess()): Start {
    val hc = ChangeHealthConnect()
    hc.granted = granted
    if (baseline.isNotEmpty()) hc.upsert(*baseline)
    val unreadSince = hc.now
    return Start(hc, hc.issueToken(StepsRecord::class), unreadSince)
}

// The SDK keeps Metadata's full constructor internal, as for records read from Health Connect.
private val METADATA = Metadata::class.constructors.single { it.parameters.size == 7 }.apply { isAccessible = true }

private fun metadata(id: String, origin: DataOrigin, clientRecordId: String?) =
    METADATA.call(Metadata.RECORDING_METHOD_UNKNOWN, id, origin, MODIFIED, clientRecordId, 0L, null)

private fun steps(
    id: String,
    start: Instant,
    count: Long = 100,
    origin: DataOrigin = ALPHA,
    clientRecordId: String? = null,
) = StepsRecord(start, null, start.plusSeconds(60), null, count, metadata(id, origin, clientRecordId))

private fun weight(id: String, time: Instant) = WeightRecord(time, null, Mass.kilograms(70.0), metadata(id, ALPHA, null))

private fun heartRate(id: String, start: Instant) = HeartRateRecord(
    start, null, start.plusSeconds(60), null, listOf(HeartRateRecord.Sample(start, 60)), metadata(id, ALPHA, null),
)

private fun id(index: Int) = "r-%05d".format(index)

private fun minutes(count: Int): Duration = Duration.ofMinutes(count.toLong())

private fun distinctSteps(count: Int) = (0 until count).map { steps(id(it), T0 + minutes(it)) }

private fun projected(record: Record): ByteArray =
    (projectSourceRecord(record, CONTEXT) as ProjectionResult.Projected).bytes

private fun refused(record: Record): ProjectionResult.Refused =
    projectSourceRecord(record, CONTEXT) as ProjectionResult.Refused

private fun projectionId(projection: ByteArray): String =
    Json.parseToJsonElement(projection.decodeToString()).jsonObject["metadata"]!!.jsonObject["id"]!!.jsonPrimitive.content

private fun describe(items: List<ChangeItem>): List<String> = items.map {
    when (it) {
        is Retraction -> "retract ${it.sourceIdentity}"
        is Projection -> "project ${projectionId(it.bytes)}"
    }
}

/** Applies a page to a replica of identifier to projection: a retraction removes, a projection sets. */
private fun MutableMap<String, String>.apply(page: Page) {
    for (item in page.items) {
        when (item) {
            is Retraction -> remove(item.sourceIdentity)
            is Projection -> put(projectionId(item.bytes), item.bytes.decodeToString())
        }
    }
}

private fun ChangesResponse.with(changes: List<Change>) = ChangesResponse(changes, nextChangesToken, hasMore, changesTokenExpired)

private fun kind(call: Call) = when (call) {
    Call.Permissions -> "permissions"
    is Call.Changes -> "changes"
}

private fun ChangePageResult.page(): Page = this as? Page ?: throw AssertionError("expected a page, got $this")

private fun read(
    hc: ChangeHealthConnect,
    token: String,
    unreadSince: Instant,
    at: Instant = hc.now,
    type: String = "steps",
    cycle: String = CYCLE,
): ChangePageResult = runSuspending {
    readChangePage(hc, Clock.fixed(at, ZoneOffset.UTC), ChangePageRequest(type, cycle, token, unreadSince, CONTEXT))
}

/** Runs a suspending call to completion on any thread the implementation chooses. */
private fun <T> runSuspending(block: suspend () -> T): T {
    val done = CountDownLatch(1)
    var outcome: Result<T>? = null
    block.startCoroutine(Continuation(EmptyCoroutineContext) { result ->
        outcome = result
        done.countDown()
    })
    check(done.await(120, TimeUnit.SECONDS)) { "change page read did not complete" }
    return outcome!!.getOrThrow()
}
