package org.activitytracker.installationkey

enum class KeyFailure(val wire: String) {
    MISSING_KEY("missing-key"),
    UNUSABLE_KEY("unusable-key"),
    KEYSTORE_UNAVAILABLE("keystore-unavailable"),
    AUTHENTICATION_FAILED("authentication-failed"),
    UNSUPPORTED_FORMAT("unsupported-format"),
    KEY_EXISTS("key-exists"),
}

enum class KeyStatus(val wire: String) {
    PRESENT("present"), MISSING("missing"), UNUSABLE("unusable"), UNAVAILABLE("unavailable"),
}

sealed class KeyResult<out T> {
    data class Success<T>(val value: T) : KeyResult<T>()
    data class Failure(val reason: KeyFailure) : KeyResult<Nothing>()
}

data class KeyCreation(val strongBox: Boolean)

/** Mechanisms only: aliases, AAD, and lifecycle decisions belong to the caller. */
interface InstallationKeyStore {
    fun create(alias: String): KeyResult<KeyCreation>
    fun keyStatus(alias: String): KeyStatus
    fun seal(alias: String, plaintext: ByteArray, aad: ByteArray): KeyResult<ByteArray>
    fun open(alias: String, sealed: ByteArray, aad: ByteArray): KeyResult<ByteArray>
    fun delete(alias: String): KeyResult<Unit>
}
