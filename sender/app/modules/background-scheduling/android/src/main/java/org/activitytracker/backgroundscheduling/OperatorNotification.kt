package org.activitytracker.backgroundscheduling

internal const val OPERATOR_ACTION_CHANNEL_ID = "sender-operator-action"
internal const val OPERATOR_ACTION_CHANNEL_NAME = "Operator action required"

internal data class OperatorNotification(val id: Int, val title: String, val body: String)

/** Fixed text only: never include health data or caller-provided text. */
internal fun operatorNotification(condition: String): OperatorNotification = when (condition) {
    "restricted" -> OperatorNotification(
        1001, "Background work restricted", "Background work is restricted. Open the Sender.",
    )
    "unregistered" -> OperatorNotification(
        1002, "Background polling not registered", "Background polling is not registered. Open the Sender.",
    )
    else -> throw IllegalArgumentException("Unsupported scheduling notification condition")
}
