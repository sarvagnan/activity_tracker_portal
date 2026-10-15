package org.activitytracker.backgroundscheduling

import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Test

class BackgroundRestrictionTest {
    @Test fun eitherRestrictionBlocksBackgroundWork() {
        for (activity in listOf(false, true)) {
            for (bucket in listOf(5, 10, 20, 30, 40, 45)) {
                assertEquals(activity || bucket == 45, backgroundRestricted({ activity }, { bucket }, 45))
            }
        }
    }

    @Test fun activityManagerFailureEscapes() {
        val failure = IllegalStateException("unreadable activity restriction")
        assertSame(failure, assertThrows(IllegalStateException::class.java) {
            backgroundRestricted({ throw failure }, { 10 }, 45)
        })
    }

    @Test fun standbyBucketFailureEscapesEvenWhenActivityIsRestricted() {
        val failure = SecurityException("unreadable standby bucket")
        for (activity in listOf(false, true)) {
            assertSame(failure, assertThrows(SecurityException::class.java) {
                backgroundRestricted({ activity }, { throw failure }, 45)
            })
        }
    }
}
