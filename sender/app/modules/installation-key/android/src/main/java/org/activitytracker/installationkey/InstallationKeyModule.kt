package org.activitytracker.installationkey

import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

class InstallationKeyModule : Module() {
    private val keys: InstallationKeyStore by lazy { AndroidInstallationKeyStore() }

    override fun definition() = ModuleDefinition {
        Name("ActivityTrackerInstallationKey")
        AsyncFunction("create") { alias: String -> keys.create(alias).bridge() }
        AsyncFunction("keyStatus") { alias: String -> keys.keyStatus(alias).wire }
        // Expo copies Uint8Array arguments into JVM byte arrays before async dispatch.
        // Never read live JSI handles from this background queue.
        AsyncFunction("seal") { alias: String, plaintext: ByteArray, aad: ByteArray ->
            keys.seal(alias, plaintext, aad).bridge()
        }
        AsyncFunction("open") { alias: String, sealed: ByteArray, aad: ByteArray ->
            keys.open(alias, sealed, aad).bridge()
        }
        AsyncFunction("delete") { alias: String -> keys.delete(alias).bridge() }
        AsyncFunction("isDeviceSecure") {
            isDeviceSecure(checkNotNull(appContext.reactContext))
        }
    }
}

// Expo converts ByteArray values in the map to JavaScript Uint8Array values.
private fun KeyResult<*>.bridge(): Map<String, Any?> = when (this) {
    is KeyResult.Failure -> mapOf("status" to "error", "reason" to reason.wire)
    is KeyResult.Success -> mapOf("status" to "ok", "value" to when (value) {
        is KeyCreation -> mapOf("strongBox" to value.strongBox)
        is Unit -> null
        else -> value
    })
}
