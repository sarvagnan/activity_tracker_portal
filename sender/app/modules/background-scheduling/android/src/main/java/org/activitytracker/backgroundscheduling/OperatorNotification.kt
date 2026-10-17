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
    "malformed-message" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: malformed-message. Open the Sender.",
    )
    "noncanonical-message" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: noncanonical-message. Open the Sender.",
    )
    "unsupported-wire-protocol" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: unsupported-wire-protocol. Open the Sender.",
    )
    "unsupported-source-projection-schema" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: unsupported-source-projection-schema. Open the Sender.",
    )
    "record-count-limit-exceeded" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: record-count-limit-exceeded. Open the Sender.",
    )
    "uncompressed-byte-limit-exceeded" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: uncompressed-byte-limit-exceeded. Open the Sender.",
    )
    "sequence-content-mismatch" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: sequence-content-mismatch. Open the Sender.",
    )
    "sequence-out-of-order" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: sequence-out-of-order. Open the Sender.",
    )
    "fragment-order-mismatch" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: fragment-order-mismatch. Open the Sender.",
    )
    "completion-identity-mismatch" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: completion-identity-mismatch. Open the Sender.",
    )
    "completion-kind-mismatch" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: completion-kind-mismatch. Open the Sender.",
    )
    "completion-count-mismatch" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: completion-count-mismatch. Open the Sender.",
    )
    "completion-chain-mismatch" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: completion-chain-mismatch. Open the Sender.",
    )
    "completion-sequence-gap" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: completion-sequence-gap. Open the Sender.",
    )
    "head-unreadable" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: head-unreadable. Open the Sender.",
    )
    "head-invalid" -> OperatorNotification(
        1003, "Delivery blocked", "Delivery blocked: head-invalid. Open the Sender.",
    )
    "permission-missing" -> OperatorNotification(
        1010, "Permission required", "Sender condition: permission-missing. Open the Sender.",
    )
    "screen-lock-removed" -> OperatorNotification(
        1011, "Screen lock required", "Sender condition: screen-lock-removed. Open the Sender.",
    )
    "storage-pressure" -> OperatorNotification(
        1012, "Storage space required", "Sender condition: storage-pressure. Open the Sender.",
    )
    "record-too-large" -> OperatorNotification(
        1013, "Sender action required", "Sender condition: record-too-large. Open the Sender.",
    )
    "projection-refused" -> OperatorNotification(
        1014, "Sender action required", "Sender condition: projection-refused. Open the Sender.",
    )
    "installation-key-unusable" -> OperatorNotification(
        1015, "Installation key unusable", "Sender condition: installation-key-unusable. Open the Sender.",
    )
    "enrollment-refused" -> OperatorNotification(
        1016, "Enrollment refused", "Sender condition: enrollment-refused. Open the Sender.",
    )
    "drain-complete" -> OperatorNotification(
        1017, "Drain complete", "Sender condition: drain-complete. Open the Sender.",
    )
    else -> throw IllegalArgumentException("Unsupported scheduling notification condition")
}
