package org.activitytracker.backgroundscheduling

import java.io.File
import org.junit.Assert.*
import org.junit.Test

class AvailableStorageTest {
    @Test
    fun probesTheAppFilesFilesystemWithoutTruncatingToInt() {
        val files = File("/synthetic/private/files")
        val result = availableStorageBytes(files) { path ->
            assertEquals(files.absolutePath, path)
            8_589_934_592L
        }
        assertEquals(8_589_934_592.0, result, 0.0)
    }

    @Test
    fun aFailedProbeEscapesToTheShell() {
        val failure = IllegalStateException("synthetic storage probe failure")
        try {
            availableStorageBytes(File("/synthetic/private/files")) { throw failure }
            fail("Expected the storage probe to fail")
        } catch (error: IllegalStateException) {
            assertSame(failure, error)
        }
    }
}
