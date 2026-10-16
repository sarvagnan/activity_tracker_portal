package org.activitytracker.healthconnect

import org.activitytracker.healthconnect.projection.PROJECTION_SCHEMA_VERSIONS

/** Native projection capabilities, independent of Health Connect availability or permissions. */
internal fun projectionSchemaVersions(): Map<String, Int> = PROJECTION_SCHEMA_VERSIONS
