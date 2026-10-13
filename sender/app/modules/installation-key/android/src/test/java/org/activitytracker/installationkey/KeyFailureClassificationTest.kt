package org.activitytracker.installationkey

import android.security.keystore.KeyPermanentlyInvalidatedException
import java.io.IOException
import java.security.GeneralSecurityException
import java.security.InvalidAlgorithmParameterException
import java.security.InvalidKeyException
import java.security.KeyStoreException
import java.security.NoSuchAlgorithmException
import java.security.ProviderException
import java.security.UnrecoverableKeyException
import javax.crypto.AEADBadTagException
import javax.crypto.BadPaddingException
import javax.crypto.spec.SecretKeySpec
import org.junit.Assert.*
import org.junit.Test

class KeyFailureClassificationTest {
    private fun classified(error: Exception, expected: KeyFailure) {
        assertEquals(error.javaClass.name, expected, classifyKeyFailure(error))
        assertEquals(KeyResult.Failure(expected), keyOperation<Unit> { throw error })
    }

    @Test fun permanentlyInvalidatedKeyIsUnusable() = classified(KeyPermanentlyInvalidatedException(), KeyFailure.UNUSABLE_KEY)
    @Test fun causeLessUnrecoverableKeyIsUnavailable() = classified(UnrecoverableKeyException(), KeyFailure.KEYSTORE_UNAVAILABLE)
    @Test fun genericSecurityFailureIsUnavailable() = classified(GeneralSecurityException(), KeyFailure.KEYSTORE_UNAVAILABLE)
    @Test fun keyStoreFailureIsUnavailable() = classified(KeyStoreException(), KeyFailure.KEYSTORE_UNAVAILABLE)
    @Test fun invalidKeyIsUnavailableUnlessPermanentlyInvalidated() = classified(InvalidKeyException(), KeyFailure.KEYSTORE_UNAVAILABLE)
    @Test fun invalidParametersAreUnavailable() = classified(InvalidAlgorithmParameterException(), KeyFailure.KEYSTORE_UNAVAILABLE)
    @Test fun missingAlgorithmIsUnavailable() = classified(NoSuchAlgorithmException(), KeyFailure.KEYSTORE_UNAVAILABLE)
    @Test fun providerFailureIsUnavailable() = classified(ProviderException(), KeyFailure.KEYSTORE_UNAVAILABLE)
    @Test fun ioFailureIsUnavailable() = classified(IOException(), KeyFailure.KEYSTORE_UNAVAILABLE)
    @Test fun badTagIsAuthenticationFailed() = classified(AEADBadTagException(), KeyFailure.AUTHENTICATION_FAILED)
    @Test fun badPaddingIsAuthenticationFailed() = classified(BadPaddingException(), KeyFailure.AUTHENTICATION_FAILED)

    @Test
    fun wrappedTransientBackendFailureIsUnavailable() {
        val backend = GeneralSecurityException("synthetic backend error")
        val error = UnrecoverableKeyException().apply { initCause(backend) }
        classified(error, KeyFailure.KEYSTORE_UNAVAILABLE)
        // Android SDK hides KeyStoreException constructors. Inject its extracted
        // public numeric code at the same cause-chain node used in production.
        assertEquals(KeyFailure.KEYSTORE_UNAVAILABLE, classifyKeyFailure(error) {
            if (it === backend) android.security.KeyStoreException.ERROR_INTERNAL_SYSTEM_ERROR else null
        })
    }

    @Test
    fun wrappedCorruptedBackendKeyIsUnusable() {
        val backend = GeneralSecurityException("synthetic corrupted key")
        val error = UnrecoverableKeyException().apply { initCause(backend) }
        assertEquals(KeyFailure.UNUSABLE_KEY, classifyKeyFailure(error) {
            if (it === backend) android.security.KeyStoreException.ERROR_KEY_CORRUPTED else null
        })
    }

    @Test
    fun onlyTheDocumentedCorruptedPublicCodeIsPermanent() {
        for (code in -1..18) assertEquals(code == 7, isPermanentKeyStoreCode(code))
    }

    @Test
    fun nestedPermanentlyInvalidatedKeyIsUnusable() {
        val error = UnrecoverableKeyException().apply {
            initCause(ProviderException(InvalidKeyException(KeyPermanentlyInvalidatedException())))
        }
        classified(error, KeyFailure.UNUSABLE_KEY)
    }

    @Test
    fun cyclicAmbiguousCausesTerminateAndStayUnavailable() {
        val first = UnrecoverableKeyException()
        val second = ProviderException()
        first.initCause(second)
        second.initCause(first)
        classified(first, KeyFailure.KEYSTORE_UNAVAILABLE)
    }

    @Test
    fun anObservedNonAesEntryIsUnusable() {
        val error = try {
            requireInstallationKey(SecretKeySpec(ByteArray(32), "HmacSHA256"))
            fail("Expected a wrong-key failure")
            return
        } catch (error: InvalidInstallationKeyException) { error }
        classified(error, KeyFailure.UNUSABLE_KEY)
    }

    @Test
    fun programmingErrorsPropagateWithoutClassification() {
        val error = IllegalStateException("synthetic programming error")
        assertNull(classifyKeyFailure(error))
        try {
            keyOperation<Unit> { throw error }
            fail("Expected the original programming error")
        } catch (actual: IllegalStateException) {
            assertSame(error, actual)
        }
    }

    @Test
    fun bridgeWireStringsDistinguishUnavailableAndUnusable() {
        assertEquals("keystore-unavailable", KeyFailure.KEYSTORE_UNAVAILABLE.wire)
        assertEquals("unusable-key", KeyFailure.UNUSABLE_KEY.wire)
        assertEquals("unavailable", KeyStatus.UNAVAILABLE.wire)
        assertEquals("unusable", KeyStatus.UNUSABLE.wire)
    }
}
