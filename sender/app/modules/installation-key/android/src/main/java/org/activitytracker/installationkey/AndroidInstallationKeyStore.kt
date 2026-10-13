package org.activitytracker.installationkey

import android.app.KeyguardManager
import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.security.UnrecoverableKeyException
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/** Android-only, non-exportable AES key. No collection or screen-lock policy. */
class AndroidInstallationKeyStore : InstallationKeyStore {
    private fun store(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun key(alias: String): SecretKey? {
        val store = store()
        if (!store.containsAlias(alias)) return null
        val key = store.getKey(alias, null)
        if (key !is SecretKey || key.algorithm != "AES") throw UnrecoverableKeyException()
        return key
    }

    @Synchronized
    override fun create(alias: String): KeyResult<KeyCreation> = keyOperation {
        if (store().containsAlias(alias)) return@keyOperation KeyResult.Failure(KeyFailure.KEY_EXISTS)
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
        KeyResult.Success(KeyCreation(strongBox = false))
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
        if (!store.containsAlias(alias)) return@keyOperation KeyResult.Failure(KeyFailure.MISSING_KEY)
        store.deleteEntry(alias)
        KeyResult.Success(Unit)
    }
}

fun isDeviceSecure(context: Context): Boolean =
    context.getSystemService(KeyguardManager::class.java).isDeviceSecure
