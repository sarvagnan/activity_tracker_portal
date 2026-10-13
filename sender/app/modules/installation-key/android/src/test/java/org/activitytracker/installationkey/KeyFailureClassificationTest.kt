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
import org.junit.Assert.*
import org.junit.Test

class KeyFailureClassificationTest {
    private fun classified(error: Exception, expected: KeyFailure) {
        assertEquals(error.javaClass.name, expected, classifyKeyFailure(error))
        assertEquals(KeyResult.Failure(expected), keyOperation<Unit> { throw error })
    }

    @Test fun permanentlyInvalidatedKeyIsUnusable() = classified(KeyPermanentlyInvalidatedException(), KeyFailure.UNUSABLE_KEY)
    @Test fun unrecoverableKeyIsUnusable() = classified(UnrecoverableKeyException(), KeyFailure.UNUSABLE_KEY)
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
