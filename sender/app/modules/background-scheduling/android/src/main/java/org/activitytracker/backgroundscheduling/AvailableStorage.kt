package org.activitytracker.backgroundscheduling

import android.os.StatFs
import java.io.File

/** Query the filesystem containing app-private ciphertext; errors reach the fail-closed shell. */
internal fun availableStorageBytes(
    filesDirectory: File,
    availableBytes: (String) -> Long = { path -> StatFs(path).availableBytes },
): Double = availableBytes(filesDirectory.absolutePath).toDouble()
