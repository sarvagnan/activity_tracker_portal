package org.activitytracker.installationkey

import java.io.InputStream
import java.io.OutputStream
import java.security.Key
import java.security.KeyStore
import java.security.KeyStoreSpi
import java.security.Provider
import java.security.UnrecoverableKeyException
import java.security.cert.Certificate
import java.util.Date
import java.util.Enumeration
import javax.crypto.spec.SecretKeySpec
import org.junit.Assert.*
import org.junit.Test

/** Real Java KeyStore dispatch with a synthetic SPI; no Android backend is needed. */
class AndroidKeyLookupTest {
    private class LookupSpi : KeyStoreSpi() {
        val keys = mutableMapOf<String, Key>()
        var failure: Exception? = null
        override fun engineGetKey(alias: String, password: CharArray?): Key? {
            failure?.let { throw it }
            return keys[alias]
        }
        override fun engineDeleteEntry(alias: String) { keys.remove(alias) }
        override fun engineLoad(stream: InputStream?, password: CharArray?) {}
        private fun unexpected(): Nothing = throw AssertionError("Only error-preserving key lookup is allowed")
        override fun engineContainsAlias(alias: String): Boolean = unexpected()
        override fun engineAliases(): Enumeration<String> = unexpected()
        override fun engineSize(): Int = unexpected()
        override fun engineIsKeyEntry(alias: String): Boolean = unexpected()
        override fun engineIsCertificateEntry(alias: String): Boolean = unexpected()
        override fun engineGetCertificateAlias(cert: Certificate): String? = unexpected()
        override fun engineGetCertificate(alias: String): Certificate? = unexpected()
        override fun engineGetCertificateChain(alias: String): Array<Certificate>? = unexpected()
        override fun engineGetCreationDate(alias: String): Date? = unexpected()
        override fun engineSetCertificateEntry(alias: String, cert: Certificate) = unexpected()
        override fun engineSetKeyEntry(alias: String, key: Key, password: CharArray?, chain: Array<out Certificate>?) = unexpected()
        override fun engineSetKeyEntry(alias: String, key: ByteArray, chain: Array<out Certificate>?) = unexpected()
        override fun engineStore(stream: OutputStream?, password: CharArray?) = unexpected()
    }

    private class Store(spi: LookupSpi) : KeyStore(
        spi, object : Provider("synthetic", 1.0, "Synthetic lookup test") {}, "synthetic",
    ) { init { load(null) } }

    @Test
    fun lookupFailureNeverAuthorizesGenerationOrDeletionAndRecoveryKeepsCiphertextReadable() {
        val spi = LookupSpi()
        val backend = Store(spi)
        var generated = 0
        val store = AndroidInstallationKeyStore({ backend }) { alias ->
            generated++
            spi.keys[alias] = SecretKeySpec(ByteArray(32) { 17 }, "AES")
            KeyCreation(false)
        }
        val alias = "synthetic-installation"
        val plaintext = byteArrayOf(1, 2, 3)
        val aad = byteArrayOf(4)
        assertEquals(KeyResult.Success(KeyCreation(false)), store.create(alias))
        val sealed = (store.seal(alias, plaintext, aad) as KeyResult.Success).value
        val retained = spi.keys[alias]
        for (failure in listOf(
            UnrecoverableKeyException().apply { initCause(java.io.IOException("synthetic outage")) },
            UnrecoverableKeyException(),
        )) {
            spi.failure = failure
            val unavailable = KeyResult.Failure(KeyFailure.KEYSTORE_UNAVAILABLE)
            assertEquals(unavailable, store.create(alias))
            assertEquals(unavailable, store.create("synthetic-absent"))
            assertEquals(KeyStatus.UNAVAILABLE, store.keyStatus(alias))
            assertEquals(unavailable, store.seal(alias, plaintext, aad))
            assertEquals(unavailable, store.open(alias, sealed, aad))
            assertEquals(unavailable, store.delete(alias))
            assertEquals(KeyResult.Failure(KeyFailure.UNSUPPORTED_FORMAT), store.open(alias, byteArrayOf(2), aad))
            assertEquals(1, generated)
            assertSame(retained, spi.keys[alias])
        }
        spi.failure = UnrecoverableKeyException().apply {
            initCause(java.security.ProviderException(android.security.keystore.KeyPermanentlyInvalidatedException()))
        }
        assertEquals(KeyStatus.UNUSABLE, store.keyStatus(alias))
        assertEquals(KeyResult.Failure(KeyFailure.UNUSABLE_KEY), store.seal(alias, plaintext, aad))
        assertEquals(KeyResult.Failure(KeyFailure.UNUSABLE_KEY), store.create(alias))
        assertEquals(1, generated)
        assertSame(retained, spi.keys[alias])
        spi.failure = null
        assertEquals(KeyResult.Failure(KeyFailure.KEY_EXISTS), store.create(alias))
        assertEquals(KeyStatus.PRESENT, store.keyStatus(alias))
        assertArrayEquals(plaintext, (store.open(alias, sealed, aad) as KeyResult.Success).value)
        assertEquals(KeyStatus.MISSING, store.keyStatus("synthetic-absent"))
        assertEquals(KeyResult.Failure(KeyFailure.MISSING_KEY), store.delete("synthetic-absent"))
        assertEquals(KeyResult.Success(Unit), store.delete(alias))
        assertEquals(KeyStatus.MISSING, store.keyStatus(alias))
    }

    @Test
    fun observedWrongKeyTypeCannotBeReplacedButCanBeExplicitlyDeleted() {
        val spi = LookupSpi()
        val backend = Store(spi)
        val store = AndroidInstallationKeyStore({ backend }) { throw AssertionError("Must not generate") }
        val alias = "synthetic-wrong-key"
        spi.keys[alias] = SecretKeySpec(ByteArray(32), "HmacSHA256")
        assertEquals(KeyResult.Failure(KeyFailure.KEY_EXISTS), store.create(alias))
        assertEquals(KeyStatus.UNUSABLE, store.keyStatus(alias))
        assertEquals(KeyResult.Failure(KeyFailure.UNUSABLE_KEY), store.seal(alias, byteArrayOf(), byteArrayOf()))
        assertEquals(KeyResult.Success(Unit), store.delete(alias))
    }
}
