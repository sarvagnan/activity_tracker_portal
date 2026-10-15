package org.activitytracker.backgroundscheduling

import android.Manifest
import android.app.ActivityManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

class BackgroundSchedulingModule : Module() {
    override fun definition() = ModuleDefinition {
        Name("ActivityTrackerBackgroundScheduling")
        AsyncFunction("isBackgroundRestricted") {
            val context = checkNotNull(appContext.reactContext)
            backgroundRestricted(
                activityRestricted = {
                    checkNotNull(context.getSystemService(ActivityManager::class.java)).isBackgroundRestricted
                },
                standbyBucket = {
                    checkNotNull(context.getSystemService(UsageStatsManager::class.java)).appStandbyBucket
                },
                restrictedBucket = UsageStatsManager.STANDBY_BUCKET_RESTRICTED,
            )
        }
        AsyncFunction("notificationsEnabled") {
            notificationsEnabled(checkNotNull(appContext.reactContext))
        }
        AsyncFunction("postOperatorNotification") { condition: String ->
            // Validate before touching Android; callers cannot supply notification content.
            val message = operatorNotification(condition)
            val context = checkNotNull(appContext.reactContext)
            val manager = checkNotNull(context.getSystemService(NotificationManager::class.java))
            manager.createNotificationChannel(NotificationChannel(
                OPERATOR_ACTION_CHANNEL_ID, OPERATOR_ACTION_CHANNEL_NAME, NotificationManager.IMPORTANCE_DEFAULT,
            ))
            check(notificationsEnabled(context)) { "Sender notifications are not enabled" }
            check(manager.getNotificationChannel(OPERATOR_ACTION_CHANNEL_ID).importance != NotificationManager.IMPORTANCE_NONE) {
                "Sender Operator notification channel is disabled"
            }
            val launch = checkNotNull(context.packageManager.getLaunchIntentForPackage(context.packageName)) {
                "Sender launch activity is unavailable"
            }
            val intent = PendingIntent.getActivity(
                context, message.id, launch, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val notification = NotificationCompat.Builder(context, OPERATOR_ACTION_CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_operator_action)
                .setContentTitle(message.title)
                .setContentText(message.body)
                .setStyle(NotificationCompat.BigTextStyle().bigText(message.body))
                .setContentIntent(intent)
                .setAutoCancel(true)
                .build()
            // A fixed ID per condition replaces a previous onset instead of stacking.
            // Permission races and platform errors escape to the polling module for retry.
            NotificationManagerCompat.from(context).notify(message.id, notification)
        }
    }

    private fun notificationsEnabled(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED &&
            NotificationManagerCompat.from(context).areNotificationsEnabled()
}
