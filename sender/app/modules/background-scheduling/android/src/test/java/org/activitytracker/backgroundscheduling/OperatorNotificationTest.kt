package org.activitytracker.backgroundscheduling

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class OperatorNotificationTest {
    @Test fun restrictedUsesFixedHealthDataFreeText() {
        assertEquals(OperatorNotification(
            1001, "Background work restricted", "Background work is restricted. Open the Sender.",
        ), operatorNotification("restricted"))
    }

    @Test fun unregisteredUsesFixedHealthDataFreeText() {
        assertEquals(OperatorNotification(
            1002, "Background polling not registered", "Background polling is not registered. Open the Sender.",
        ), operatorNotification("unregistered"))
    }

    @Test fun conditionsHaveStableDistinctIds() {
        assertEquals(operatorNotification("restricted").id, operatorNotification("restricted").id)
        assertEquals(operatorNotification("unregistered").id, operatorNotification("unregistered").id)
        assertNotEquals(operatorNotification("restricted").id, operatorNotification("unregistered").id)
    }

    @Test fun deliveryFailuresUseFixedHealthDataFreeText() {
        val failures = listOf(
            "malformed-message", "noncanonical-message", "unsupported-wire-protocol",
            "unsupported-source-projection-schema", "record-count-limit-exceeded",
            "uncompressed-byte-limit-exceeded", "sequence-content-mismatch", "sequence-out-of-order",
            "fragment-order-mismatch", "completion-identity-mismatch", "completion-kind-mismatch",
            "completion-count-mismatch", "completion-chain-mismatch", "completion-sequence-gap",
            "head-unreadable", "head-invalid",
        )
        for (failure in failures) {
            assertEquals(OperatorNotification(
                1003, "Delivery blocked", "Delivery blocked: $failure. Open the Sender.",
            ), operatorNotification(failure))
            assertNotEquals(operatorNotification("restricted").id, operatorNotification(failure).id)
            assertNotEquals(operatorNotification("unregistered").id, operatorNotification(failure).id)
        }
    }

    @Test fun operatorConditionsUseFixedHealthDataFreeTextAndDistinctIds() {
        val expected = mapOf(
            "permission-missing" to OperatorNotification(1010, "Permission required", "Sender condition: permission-missing. Open the Sender."),
            "screen-lock-removed" to OperatorNotification(1011, "Screen lock required", "Sender condition: screen-lock-removed. Open the Sender."),
            "storage-pressure" to OperatorNotification(1012, "Storage space required", "Sender condition: storage-pressure. Open the Sender."),
            "record-too-large" to OperatorNotification(1013, "Sender action required", "Sender condition: record-too-large. Open the Sender."),
            "projection-refused" to OperatorNotification(1014, "Sender action required", "Sender condition: projection-refused. Open the Sender."),
            "installation-key-unusable" to OperatorNotification(1015, "Installation key unusable", "Sender condition: installation-key-unusable. Open the Sender."),
            "enrollment-refused" to OperatorNotification(1016, "Enrollment refused", "Sender condition: enrollment-refused. Open the Sender."),
            "drain-complete" to OperatorNotification(1017, "Drain complete", "Sender condition: drain-complete. Open the Sender."),
        )
        val ids = mutableSetOf(1001, 1002, 1003)
        for ((condition, notification) in expected) {
            assertEquals(notification, operatorNotification(condition))
            assertEquals(true, ids.add(notification.id))
        }
    }

    @Test fun rejectsEveryUnsupportedConditionWithoutEchoingInput() {
        for (condition in listOf("", "available", "Restricted", "restricted ", "private health data")) {
            val failure = assertThrows(IllegalArgumentException::class.java) { operatorNotification(condition) }
            assertEquals("Unsupported scheduling notification condition", failure.message)
        }
    }
}
