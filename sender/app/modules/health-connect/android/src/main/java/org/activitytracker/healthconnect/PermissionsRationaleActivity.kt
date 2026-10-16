package org.activitytracker.healthconnect

import android.app.Activity
import android.os.Bundle
import android.widget.ScrollView
import android.widget.TextView

/** Static rationale only: no bridge, permission request, network, or storage access. */
class PermissionsRationaleActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Health data permissions"
        val explanation = TextView(this).apply {
            text = "Activity Tracker Sender reads the health record types you allow from Health Connect " +
                "to synchronize them with your enrolled Portal. History access permits older records; " +
                "background access permits syncing while the app is closed. " +
                "You control these permissions in Health Connect and can revoke them at any time. " +
                "This screen does not read or display health records."
            textSize = 18f
            val padding = (24 * resources.displayMetrics.density).toInt()
            setPadding(padding, padding, padding, padding)
        }
        setContentView(ScrollView(this).apply { addView(explanation) })
    }
}
