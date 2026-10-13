package org.activitytracker.installationkey

import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import expo.modules.kotlin.typedarray.Uint8Array

class InstallationKeyModule : Module() {
    private val keys: InstallationKeyStore by lazy { AndroidInstallationKeyStore() }

    override fun definition() = ModuleDefinition {
        Name("ActivityTrackerInstallationKey")
        AsyncFunction("create") { alias: String -> keys.create(alias).bridge() }
        AsyncFunction("keyStatus") { alias: String -> keys.keyStatus(alias).wire }
        AsyncFunction("seal") { alias: String, plaintext: Uint8Array, aad: Uint8Array ->
            keys.seal(alias, plaintext.bytes(), aad.bytes()).bridge()
        }
        AsyncFunction("open") { alias: String, sealed: Uint8Array, aad: Uint8Array ->
            keys.open(alias, sealed.bytes(), aad.bytes()).bridge()
        }
        AsyncFunction("delete") { alias: String -> keys.delete(alias).bridge() }
        AsyncFunction("isDeviceSecure") {
            isDeviceSecure(checkNotNull(appContext.reactContext))
        }
    }
}

private fun Uint8Array.bytes(): ByteArray = ByteArray(byteLength).also { read(it, 0, byteLength) }

// Expo converts ByteArray values in the map to JavaScript Uint8Array values.
private fun KeyResult<*>.bridge(): Map<String, Any?> = when (this) {
    is KeyResult.Failure -> mapOf("status" to "error", "reason" to reason.wire)
    is KeyResult.Success -> mapOf("status" to "ok", "value" to when (value) {
        is KeyCreation -> mapOf("strongBox" to value.strongBox)
        is Unit -> null
        else -> value
    })
}
