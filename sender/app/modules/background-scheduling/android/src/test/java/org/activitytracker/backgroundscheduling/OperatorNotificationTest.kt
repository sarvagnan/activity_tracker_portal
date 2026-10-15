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

    @Test fun rejectsEveryUnsupportedConditionWithoutEchoingInput() {
        for (condition in listOf("", "available", "Restricted", "restricted ", "private health data")) {
            val failure = assertThrows(IllegalArgumentException::class.java) { operatorNotification(condition) }
            assertEquals("Unsupported scheduling notification condition", failure.message)
        }
    }
}
