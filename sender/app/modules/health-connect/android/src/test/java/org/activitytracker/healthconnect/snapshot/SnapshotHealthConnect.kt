package org.activitytracker.healthconnect.snapshot

import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.response.ReadRecordsResponse
import java.time.Duration
import java.time.Instant
import org.activitytracker.healthconnect.HealthConnectAccess

/**
 * Synthetic Health Connect for snapshot reads, with no device, Context, or real data.
 *
 * It reproduces the platform read semantics read-snapshot-page@1 relies on, from AOSP
 * HealthFitness (RecordHelper, TimeInstantRangeFilter, ReadRecordsRequestUsingFilters):
 * - records are ordered by start time, then by row; an update keeps its row;
 * - the range includes its start and excludes its end, and filters on start time;
 * - a missing start means 1970 and a missing end means one day past now;
 * - the record limit is 1 to 5000, with a next-page token whenever more records match;
 * - reading without the type's read permission is a SecurityException;
 * - without the history permission, records starting more than 30 days before the
 *   first grant are silently hidden.
 *
 * It rejects any page token, so a reader that relies on one fails.
 */
class SnapshotHealthConnect : HealthConnectAccess {
    sealed interface Call {
        data object Permissions : Call
        class Records(val request: ReadRecordsRequest<*>) : Call
    }

    private class Row(val row: Long, var record: Record)

    private val rows = mutableListOf<Row>()
    private var nextRow = 1L

    val calls = mutableListOf<Call>()
    val recordRequests: List<ReadRecordsRequest<*>> get() = calls.filterIsInstance<Call.Records>().map { it.request }

    var granted: Set<String> = emptySet()
    var firstGrant: Instant = Instant.parse("2026-01-01T00:00:00Z")
    var now: Instant = Instant.parse("2026-10-13T00:00:00Z")

    /** Thrown by the next permission or record read instead of answering. */
    var permissionFailure: Throwable? = null
    var recordFailure: Throwable? = null

    /** Runs inside a record read, after the call is recorded and before it is answered. */
    var duringRead: (read: Int) -> Unit = {}

    /** Runs after a record read is answered, before the reader's next call. */
    var afterRead: (read: Int) -> Unit = {}

    /** Replaces the answer to a record read, to model a provider that breaks its contract. */
    var respond: ((request: ReadRecordsRequest<*>, records: List<Record>, token: String?) -> Pair<List<Record>, String?>)? = null

    fun insert(vararg records: Record) = records.forEach { rows += Row(nextRow++, it) }

    fun insertAll(records: Iterable<Record>) = records.forEach { insert(it) }

    fun delete(id: String) {
        check(rows.removeIf { it.record.metadata.id == id }) { "no synthetic record $id" }
    }

    /** An update in place: same row, possibly a different start time. */
    fun replace(record: Record) {
        val row = rows.single { it.record.metadata.id == record.metadata.id }
        row.record = record
    }

    fun contains(id: String) = rows.any { it.record.metadata.id == id }

    override suspend fun getGrantedPermissions(): Set<String> {
        calls += Call.Permissions
        permissionFailure?.let { throw it }
        return granted.toSet()
    }

    override suspend fun getChangesToken(request: ChangesTokenRequest): String =
        throw AssertionError("a snapshot page never requests a changes token")

    @Suppress("UNCHECKED_CAST")
    override suspend fun <T : Record> readRecords(request: ReadRecordsRequest<T>): ReadRecordsResponse<T> {
        calls += Call.Records(request)
        val read = recordRequests.size
        duringRead(read)
        recordFailure?.let { throw it }
        if (request.pageToken != null) throw AssertionError("snapshot reads must not use Health Connect page tokens")
        if (!request.ascendingOrder) throw AssertionError("snapshot reads are ascending")
        val filter = request.timeRangeFilter
        if (filter.localStartTime != null || filter.localEndTime != null) {
            throw AssertionError("snapshot reads use an instant time range")
        }
        if (filter.startTime == null && filter.endTime == null) {
            throw IllegalArgumentException("Both start time and end time cannot be null.")
        }
        if (request.pageSize !in 1..HEALTH_CONNECT_READ_LIMIT) {
            throw IllegalArgumentException("Valid pageSize range is 1 - $HEALTH_CONNECT_READ_LIMIT")
        }
        if (HealthPermission.getReadPermission(request.recordType) !in granted) {
            throw SecurityException("synthetic read permission missing")
        }
        val start = filter.startTime ?: Instant.EPOCH
        val end = filter.endTime ?: now.plus(Duration.ofDays(1))
        val historyFloor =
            if (HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY in granted) null
            else firstGrant.minus(Duration.ofDays(30))
        val matching = rows
            .filter { request.recordType.isInstance(it.record) }
            .filter { request.dataOriginFilter.isEmpty() || it.record.metadata.dataOrigin in request.dataOriginFilter }
            .filter { startOf(it.record) >= start && startOf(it.record) < end }
            .filter { historyFloor == null || startOf(it.record) >= historyFloor }
            .sortedWith(compareBy<Row>({ startOf(it.record) }, { it.row }))
        val records: List<Record> = matching.take(request.pageSize).map { it.record }
        val token: String? = if (matching.size > request.pageSize) "synthetic-next-page-$read" else null
        val (answer, answerToken) = respond?.invoke(request, records, token) ?: (records to token)
        afterRead(read)
        return ReadRecordsResponse(answer as List<T>, answerToken)
    }

    companion object {
        /** `time` for instantaneous types, `startTime` for interval and series types; the SDK's shape interfaces are internal. */
        fun startOf(record: Record): Instant {
            val accessor = record.javaClass.methods.firstOrNull { it.name == "getTime" && it.parameterCount == 0 && it.returnType == Instant::class.java }
                ?: record.javaClass.methods.firstOrNull { it.name == "getStartTime" && it.parameterCount == 0 && it.returnType == Instant::class.java }
                ?: throw AssertionError("record ${record::class.simpleName} has no start time")
            return accessor.invoke(record) as Instant
        }
    }
}
