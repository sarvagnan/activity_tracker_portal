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

    @Test fun rejectsEveryUnsupportedConditionWithoutEchoingInput() {
        for (condition in listOf("", "available", "Restricted", "restricted ", "private health data")) {
            val failure = assertThrows(IllegalArgumentException::class.java) { operatorNotification(condition) }
            assertEquals("Unsupported scheduling notification condition", failure.message)
        }
    }
}
