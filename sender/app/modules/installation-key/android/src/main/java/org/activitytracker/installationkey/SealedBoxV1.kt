package org.activitytracker.installationkey

import android.security.KeyStoreException
import android.security.keystore.KeyPermanentlyInvalidatedException
import java.io.IOException
import java.security.GeneralSecurityException
import java.security.InvalidKeyException
import java.security.Key
import java.security.ProviderException
import javax.crypto.AEADBadTagException
import javax.crypto.BadPaddingException
import javax.crypto.Cipher
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** 0x01 || 12-byte IV || ciphertext || 16-byte tag; never contains AAD. */
internal object SealedBoxV1 {
    fun formatFailure(sealed: ByteArray): KeyFailure? = when {
        sealed.isEmpty() -> KeyFailure.AUTHENTICATION_FAILED
        sealed[0] != 1.toByte() -> KeyFailure.UNSUPPORTED_FORMAT
        sealed.size < 29 -> KeyFailure.AUTHENTICATION_FAILED
        else -> null
    }

    // Explicit IVs are only used by the JVM synthetic substitute. Android always
    // omits this parameter so the Keystore provider supplies the randomized IV.
    fun seal(key: SecretKey, plaintext: ByteArray, aad: ByteArray, testIv: ByteArray? = null): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        if (testIv == null) cipher.init(Cipher.ENCRYPT_MODE, key)
        else cipher.init(Cipher.ENCRYPT_MODE, key, GCMParameterSpec(128, testIv))
        check(cipher.iv.size == 12) { "Expected a 12-byte GCM IV" }
        check(cipher.parameters.getParameterSpec(GCMParameterSpec::class.java).tLen == 128) {
            "Expected a 128-bit GCM tag"
        }
        cipher.updateAAD(aad)
        return byteArrayOf(1) + cipher.iv + cipher.doFinal(plaintext)
    }

    fun open(key: SecretKey, sealed: ByteArray, aad: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, sealed.copyOfRange(1, 13)))
        cipher.updateAAD(aad)
        return cipher.doFinal(sealed.copyOfRange(13, sealed.size))
    }
}

/** An observed entry of the wrong type is definite, unlike failed retrieval. */
internal class InvalidInstallationKeyException : InvalidKeyException()

internal fun requireInstallationKey(key: Key): SecretKey {
    if (key !is SecretKey || key.algorithm != "AES") throw InvalidInstallationKeyException()
    return key
}

// API 34 AOSP: keystore/java/android/security/KeyStoreException.java,
// ERROR_KEY_CORRUPTED (7): the key is corrupted and could not be recovered.
// getNumericErrorCode() exposes public codes; getErrorCode() is hidden/internal.
// No other public code definitively establishes corruption of an existing key.
// https://android.googlesource.com/platform/frameworks/base/+/android-14.0.0_r1/keystore/java/android/security/KeyStoreException.java
internal fun isPermanentKeyStoreCode(code: Int): Boolean = code == KeyStoreException.ERROR_KEY_CORRUPTED

/** Shared classification; ambiguous failures never imply key retirement. */
internal fun classifyKeyFailure(
    error: Exception,
    numericCode: (Throwable) -> Int? = { (it as? KeyStoreException)?.numericErrorCode },
): KeyFailure? {
    val causes = mutableSetOf<Throwable>()
    var cause: Throwable? = error
    while (cause != null && causes.add(cause)) {
        if (cause is KeyPermanentlyInvalidatedException || cause is InvalidInstallationKeyException ||
            numericCode(cause)?.let(::isPermanentKeyStoreCode) == true) {
            return KeyFailure.UNUSABLE_KEY
        }
        cause = cause.cause
    }
    if (causes.any { it is AEADBadTagException || it is BadPaddingException }) {
        return KeyFailure.AUTHENTICATION_FAILED
    }
    if (causes.any { it is GeneralSecurityException || it is KeyStoreException ||
            it is ProviderException || it is IOException }) {
        return KeyFailure.KEYSTORE_UNAVAILABLE
    }
    return null
}

/** Expected provider/key failures stay typed and never expose exception text. */
internal fun <T> keyOperation(operation: () -> KeyResult<T>): KeyResult<T> = try {
    operation()
} catch (error: Exception) {
    val failure = classifyKeyFailure(error) ?: throw error
    KeyResult.Failure(failure)
}
