package org.activitytracker.healthconnect

import org.activitytracker.healthconnect.projection.PROJECTION_SCHEMA_VERSIONS
import org.junit.Assert.*
import org.junit.Test

class ProjectionVersionsTest {
    @Test
    fun advertisesEveryNativeProjectionAtItsSchemaVersion() {
        val versions = projectionSchemaVersions()
        assertEquals(PROJECTION_SCHEMA_VERSIONS, versions)
        assertTrue(versions.isNotEmpty())
        assertEquals(1, versions["steps"])
        assertTrue(versions.values.all { it > 0 })
    }
}
