package org.activitytracker.healthconnect

import android.content.Context
import android.content.Intent
import androidx.health.connect.client.PermissionController
import expo.modules.kotlin.activityresult.AppContextActivityResultContract

/** Adapt the SDK contract to Expo's serializable activity-result input. */
internal class HealthPermissionContract : AppContextActivityResultContract<ArrayList<String>, Set<String>> {
    private val sdk = PermissionController.createRequestPermissionResultContract()

    override fun createIntent(context: Context, input: ArrayList<String>): Intent =
        sdk.createIntent(context, HealthPermissionRequests.filter(input))

    override fun parseResult(input: ArrayList<String>, resultCode: Int, intent: Intent?): Set<String> =
        HealthPermissionRequests.filter(sdk.parseResult(resultCode, intent)).intersect(input.toSet())
}
