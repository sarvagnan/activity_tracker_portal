package org.activitytracker.healthconnect.changes

import androidx.health.connect.client.changes.DeletionChange
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.response.ChangesResponse
import java.time.Duration
import java.time.Instant
import kotlin.reflect.KClass
import org.activitytracker.healthconnect.HealthConnectChangeAccess

/**
 * Synthetic Health Connect change log for change pages, with no device, Context, or real data.
 *
 * It models the platform semantics read-change-page@1 relies on, from AOSP HealthFitness
 * (ChangeLogsHelper, ChangeLogsRequestHelper, DailyCleanupJob, RecordHelper) and the pinned
 * SDK's HealthConnectClientUpsideDownImpl.getChanges:
 * - one write logs rows of up to 1,000 identifiers of one record type;
 * - a token is a stored request: a record type, a log position, and the time it was issued;
 * - a page collects the type's rows after the position until it holds 1,000 identifiers, so it
 *   may hold up to 1,999, stores a new token without a time at its last row, and reports whether
 *   rows remain; a page that reads no rows returns the request's own token;
 * - upsertions are the records held for the page's upserted identifiers when it is read, each
 *   once; deletions are the page's deleted identifiers, with repeats; upsertions come first;
 * - cleanup deletes rows and timed tokens 32 days after they were written, sparing untimed ones;
 * - an unknown token is an invalid argument, which the SDK reports as an expired token;
 * - reading without the type's read permission is a SecurityException;
 * - without the history permission, upserted records starting more than 30 days before the
 *   first grant are silently omitted.
 *
 * Health Connect defines no upsertion order, so the fake scrambles it deterministically.
 */
class ChangeHealthConnect : HealthConnectChangeAccess {
    sealed interface Call {
        data object Permissions : Call
        data class Changes(val token: String) : Call
    }

    private class Row(val row: Long, val type: KClass<out Record>, val upsert: Boolean, val ids: List<String>, val time: Instant)

    private class Token(val type: KClass<out Record>, val position: Long, val time: Instant?)

    private val records = LinkedHashMap<String, Record>()
    private val log = mutableListOf<Row>()
    private val tokens = HashMap<String, Token>()
    private var nextRow = 1L
    private var nextToken = 1L

    val calls = mutableListOf<Call>()
    val changeTokens: List<String> get() = calls.filterIsInstance<Call.Changes>().map { it.token }

    /** Every change response the reader received, after any [respond] replacement. */
    val answers = mutableListOf<ChangesResponse>()

    var granted: Set<String> = emptySet()
    var firstGrant: Instant = Instant.parse("2026-01-01T00:00:00Z")

    /** The provider's clock: it stamps log rows and tokens and drives cleanup. */
    var now: Instant = Instant.parse("2026-10-13T00:00:00Z")

    /** Thrown by the next permission or change read instead of answering. */
    var permissionFailure: Throwable? = null
    var changesFailure: Throwable? = null

    /** Runs inside a change read, after the call is recorded and before the log is read. */
    var duringRead: (read: Int) -> Unit = {}

    /** Runs inside a change read, after the page's rows are chosen and before its records are read. */
    var betweenLogAndRecords: (read: Int) -> Unit = {}

    /** Runs after a change read is answered, before the reader's next call. */
    var afterRead: (read: Int) -> Unit = {}

    /** Replaces the answer to a change read, to model a provider that breaks its contract. */
    var respond: ((token: String, response: ChangesResponse) -> ChangesResponse)? = null

    /** getChangesToken: a token at the end of the log, stored with the current time. */
    fun issueToken(type: KClass<out Record>): String = store(Token(type, nextRow - 1, now))

    /** One write that inserts or updates every record, logging their identifiers as upserted. */
    fun upsert(vararg records: Record) {
        records.forEach { this.records[it.metadata.id] = it }
        logWrite(records.groupBy({ it::class }, { it.metadata.id }), upsert = true)
    }

    /** One write that deletes every identifier, logging them as deleted. */
    fun delete(vararg ids: String) {
        val byType = ids.groupBy { id -> records.remove(id)?.let { it::class } ?: error("no synthetic record $id") }
        logWrite(byType, upsert = false)
    }

    fun contains(id: String) = id in records

    fun current(type: KClass<out Record>): List<Record> = records.values.filter { type.isInstance(it) }

    /** Moves the provider's clock forward and runs its daily cleanup. */
    fun advance(duration: Duration) {
        now += duration
        val cutoff = now - CHANGE_LOG_RETENTION
        log.removeIf { !it.time.isAfter(cutoff) }
        tokens.values.removeIf { it.time != null && !it.time.isAfter(cutoff) }
    }

    override suspend fun getGrantedPermissions(): Set<String> {
        calls += Call.Permissions
        permissionFailure?.let { throw it }
        return granted.toSet()
    }

    override suspend fun getChanges(changesToken: String): ChangesResponse {
        calls += Call.Changes(changesToken)
        val read = changeTokens.size
        duringRead(read)
        changesFailure?.let { throw it }
        val token = tokens[changesToken]
            ?: return answer(changesToken, ChangesResponse(emptyList(), "", false, true), read)
        if (HealthPermission.getReadPermission(token.type) !in granted) {
            throw SecurityException("synthetic read permission missing")
        }
        val upserted = LinkedHashSet<String>()
        val deleted = mutableListOf<String>()
        var count = 0
        var last: Long? = null
        var hasMore = false
        for (row in log) {
            if (row.row <= token.position || row.type != token.type) continue
            if (count >= PAGE_SIZE) {
                hasMore = true
                break
            }
            if (row.upsert) upserted += row.ids else deleted += row.ids
            count += row.ids.size
            last = row.row
        }
        val next = last?.let { store(Token(token.type, it, null)) } ?: changesToken
        betweenLogAndRecords(read)
        val historyFloor =
            if (HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY in granted) null
            else firstGrant - Duration.ofDays(30)
        val upsertions = upserted
            .mapNotNull { records[it] }
            .filter { token.type.isInstance(it) && (historyFloor == null || startOf(it) >= historyFloor) }
            .sortedWith(compareBy({ Integer.reverse(it.metadata.id.hashCode()) }, { it.metadata.id }))
        val changes = upsertions.map(::UpsertionChange) + deleted.map(::DeletionChange)
        return answer(changesToken, ChangesResponse(changes, next, hasMore, false), read)
    }

    private fun answer(token: String, response: ChangesResponse, read: Int): ChangesResponse {
        val answer = respond?.invoke(token, response) ?: response
        answers += answer
        afterRead(read)
        return answer
    }

    private fun store(token: Token): String = (nextToken++).toString().also { tokens[it] = token }

    private fun logWrite(idsByType: Map<out KClass<out Record>, List<String>>, upsert: Boolean) {
        for ((type, ids) in idsByType) {
            for (chunk in ids.chunked(PAGE_SIZE)) log += Row(nextRow++, type, upsert, chunk, now)
        }
    }

    companion object {
        /** The SDK's fixed change page size, in identifiers; also the largest row a write logs. */
        const val PAGE_SIZE = 1000

        /** How long Health Connect keeps change-log rows and timed tokens. */
        val CHANGE_LOG_RETENTION: Duration = Duration.ofDays(32)

        /** `time` for instantaneous types, `startTime` for interval and series types; the SDK's shape interfaces are internal. */
        fun startOf(record: Record): Instant {
            val accessor = record.javaClass.methods.firstOrNull { it.name == "getTime" && it.parameterCount == 0 && it.returnType == Instant::class.java }
                ?: record.javaClass.methods.firstOrNull { it.name == "getStartTime" && it.parameterCount == 0 && it.returnType == Instant::class.java }
                ?: throw AssertionError("record ${record::class.simpleName} has no start time")
            return accessor.invoke(record) as Instant
        }
    }
}
