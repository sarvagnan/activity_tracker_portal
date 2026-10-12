package org.activitytracker.healthconnect

import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

/** Native registration only; governed health operations are added in later units. */
class HealthConnectModule : Module() {
    override fun definition() = ModuleDefinition {
        Name("ActivityTrackerHealthConnect")
    }
}
