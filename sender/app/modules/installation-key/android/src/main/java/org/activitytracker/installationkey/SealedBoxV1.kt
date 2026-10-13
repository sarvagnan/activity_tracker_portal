package org.activitytracker.installationkey

import android.security.keystore.KeyPermanentlyInvalidatedException
import java.io.IOException
import java.security.GeneralSecurityException
import java.security.ProviderException
import java.security.UnrecoverableKeyException
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

/** Shared classification; transient provider failures never imply key retirement. */
internal fun classifyKeyFailure(error: Exception): KeyFailure? = when (error) {
    is AEADBadTagException, is BadPaddingException -> KeyFailure.AUTHENTICATION_FAILED
    is KeyPermanentlyInvalidatedException, is UnrecoverableKeyException -> KeyFailure.UNUSABLE_KEY
    is GeneralSecurityException, is ProviderException, is IOException -> KeyFailure.KEYSTORE_UNAVAILABLE
    else -> null
}

/** Expected provider/key failures stay typed and never expose exception text. */
internal fun <T> keyOperation(operation: () -> KeyResult<T>): KeyResult<T> = try {
    operation()
} catch (error: Exception) {
    val failure = classifyKeyFailure(error) ?: throw error
    KeyResult.Failure(failure)
}
