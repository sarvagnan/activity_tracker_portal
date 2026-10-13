package org.activitytracker.installationkey

import android.app.KeyguardManager
import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/** Android-only, non-exportable AES key. No collection or screen-lock policy. */
class AndroidInstallationKeyStore internal constructor(
    private val store: () -> KeyStore,
    private val generate: (String) -> KeyCreation,
) : InstallationKeyStore {
    constructor() : this(
        { KeyStore.getInstance("AndroidKeyStore").apply { load(null) } },
        ::generateInstallationKey,
    )

    // API 34 AOSP: keystore/java/android/security/keystore2/AndroidKeyStoreSpi.java,
    // https://android.googlesource.com/platform/frameworks/base/+/android-14.0.0_r1/keystore/java/android/security/keystore2/AndroidKeyStoreSpi.java
    // engineGetKey, delegates to AndroidKeyStoreProvider.loadAndroidKeyStoreKeyFromKeystore.
    // For key entries, only KEY_NOT_FOUND returns null; other backend errors propagate
    // (usually wrapped in UnrecoverableKeyException). Never use engineContainsAlias:
    // its getKeyMetadata catches backend errors and returns null on uncertainty.
    // https://android.googlesource.com/platform/frameworks/base/+/android-14.0.0_r1/keystore/java/android/security/keystore2/AndroidKeyStoreProvider.java
    // getKey also returns null for certificate-only entries, which this key primitive
    // does not create. This lookup is for installation key entries, not certificates.
    private fun lookup(store: KeyStore, alias: String) = store.getKey(alias, null)

    private fun key(alias: String): SecretKey? {
        val key = lookup(store(), alias) ?: return null
        return requireInstallationKey(key)
    }

    @Synchronized
    override fun create(alias: String): KeyResult<KeyCreation> = keyOperation {
        if (lookup(store(), alias) != null) return@keyOperation KeyResult.Failure(KeyFailure.KEY_EXISTS)
        KeyResult.Success(generate(alias))
    }

    @Synchronized
    override fun keyStatus(alias: String): KeyStatus {
        val result = keyOperation {
            val key = key(alias) ?: return@keyOperation KeyResult.Failure(KeyFailure.MISSING_KEY)
            // Retrieval alone need not reveal a permanently invalidated key.
            Cipher.getInstance("AES/GCM/NoPadding").init(Cipher.ENCRYPT_MODE, key)
            KeyResult.Success(Unit)
        }
        return when (result) {
            is KeyResult.Success -> KeyStatus.PRESENT
            is KeyResult.Failure -> when (result.reason) {
                KeyFailure.MISSING_KEY -> KeyStatus.MISSING
                KeyFailure.UNUSABLE_KEY -> KeyStatus.UNUSABLE
                else -> KeyStatus.UNAVAILABLE
            }
        }
    }

    @Synchronized
    override fun seal(alias: String, plaintext: ByteArray, aad: ByteArray): KeyResult<ByteArray> = keyOperation {
        val key = key(alias) ?: return@keyOperation KeyResult.Failure(KeyFailure.MISSING_KEY)
        KeyResult.Success(SealedBoxV1.seal(key, plaintext, aad))
    }

    @Synchronized
    override fun open(alias: String, sealed: ByteArray, aad: ByteArray): KeyResult<ByteArray> {
        SealedBoxV1.formatFailure(sealed)?.let { return KeyResult.Failure(it) }
        return keyOperation {
            val key = key(alias) ?: return@keyOperation KeyResult.Failure(KeyFailure.MISSING_KEY)
            KeyResult.Success(SealedBoxV1.open(key, sealed, aad))
        }
    }

    @Synchronized
    override fun delete(alias: String): KeyResult<Unit> = keyOperation {
        val store = store()
        if (lookup(store, alias) == null) return@keyOperation KeyResult.Failure(KeyFailure.MISSING_KEY)
        store.deleteEntry(alias)
        KeyResult.Success(Unit)
    }
}

fun isDeviceSecure(context: Context): Boolean =
    context.getSystemService(KeyguardManager::class.java).isDeviceSecure

private fun generateInstallationKey(alias: String): KeyCreation {
    val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
    generator.init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
        .setKeySize(256)
        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
        .setRandomizedEncryptionRequired(true)
        .setUserAuthenticationRequired(false)
        .setUnlockedDeviceRequired(false)
        .setIsStrongBoxBacked(false)
        .build())
    generator.generateKey()
    return KeyCreation(strongBox = false)
}
