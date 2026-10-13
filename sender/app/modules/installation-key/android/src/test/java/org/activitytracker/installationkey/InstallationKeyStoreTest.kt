package org.activitytracker.installationkey

import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.*
import org.junit.Test

class InstallationKeyStoreTest {
    private val alias = "synthetic-installation"
    private val vectorPath = "sender/app/test/fixtures/sealed-box-v1.json"
    private val root = generateSequence(File(checkNotNull(System.getProperty("user.dir"))).absoluteFile) { it.parentFile }
        .first { File(it, vectorPath).isFile }
    private val vector = Json.parseToJsonElement(File(root, vectorPath).readText()).jsonObject
    private fun bytes(field: String): ByteArray = vector[field]!!.jsonPrimitive.content
        .chunked(2).map { it.toInt(16).toByte() }.toByteArray()
    private fun fixedStore() = SyntheticInstallationKeyStore({ bytes("key_hex") }, { bytes("iv_hex") })
    private fun opened(result: KeyResult<ByteArray>): ByteArray {
        assertTrue(result is KeyResult.Success)
        return (result as KeyResult.Success).value
    }
    private fun failure(reason: KeyFailure) = KeyResult.Failure(reason)

    @Test
    fun producesAndOpensTheSharedKotlinNodeVector() {
        val store: InstallationKeyStore = fixedStore()
        assertEquals(KeyResult.Success(KeyCreation(false)), store.create(alias))
        assertArrayEquals(bytes("sealed_hex"), opened(store.seal(alias, bytes("plaintext_hex"), bytes("aad_hex"))))
        assertArrayEquals(bytes("plaintext_hex"), opened(store.open(alias, bytes("sealed_hex"), bytes("aad_hex"))))
    }

    @Test
    fun createNeverReplacesAnExistingKeyAndDeleteIsExplicit() {
        val store = fixedStore()
        assertEquals(KeyStatus.MISSING, store.keyStatus(alias))
        assertEquals(failure(KeyFailure.MISSING_KEY), store.delete(alias))
        store.create(alias)
        assertEquals(KeyStatus.PRESENT, store.keyStatus(alias))
        assertEquals(failure(KeyFailure.KEY_EXISTS), store.create(alias))
        assertArrayEquals(bytes("plaintext_hex"), opened(store.open(alias, bytes("sealed_hex"), bytes("aad_hex"))))
        assertEquals(KeyResult.Success(Unit), store.delete(alias))
        assertEquals(KeyStatus.MISSING, store.keyStatus(alias))
    }

    @Test
    fun missingKeysYieldTypedFailures() {
        val store = fixedStore()
        assertEquals(failure(KeyFailure.MISSING_KEY), store.seal(alias, bytes("plaintext_hex"), bytes("aad_hex")))
        assertEquals(failure(KeyFailure.MISSING_KEY), store.open(alias, bytes("sealed_hex"), bytes("aad_hex")))
    }

    @Test
    fun unusableKeysAreReportedWithoutReplacingOrDeletingThem() {
        val store = fixedStore()
        store.create(alias)
        store.invalidate(alias)
        assertEquals(KeyStatus.UNUSABLE, store.keyStatus(alias))
        assertEquals(failure(KeyFailure.UNUSABLE_KEY), store.seal(alias, bytes("plaintext_hex"), bytes("aad_hex")))
        assertEquals(failure(KeyFailure.UNUSABLE_KEY), store.open(alias, bytes("sealed_hex"), bytes("aad_hex")))
        assertEquals(failure(KeyFailure.KEY_EXISTS), store.create(alias))
        assertEquals(KeyResult.Success(Unit), store.delete(alias))
    }

    @Test
    fun flippedIvCiphertextOrTagBitsFailAuthentication() {
        val store = fixedStore()
        store.create(alias)
        for (index in listOf(1, 13, bytes("sealed_hex").lastIndex)) {
            val sealed = bytes("sealed_hex")
            sealed[index] = (sealed[index].toInt() xor 1).toByte()
            assertEquals(failure(KeyFailure.AUTHENTICATION_FAILED), store.open(alias, sealed, bytes("aad_hex")))
        }
    }

    @Test
    fun wrongAadFailsAuthentication() {
        val store = fixedStore()
        store.create(alias)
        assertEquals(failure(KeyFailure.AUTHENTICATION_FAILED), store.open(alias, bytes("sealed_hex"), byteArrayOf(99)))
    }

    @Test
    fun unknownVersionIsUnsupported() {
        val store = fixedStore()
        store.create(alias)
        val sealed = bytes("sealed_hex")
        sealed[0] = 2
        assertEquals(failure(KeyFailure.UNSUPPORTED_FORMAT), store.open(alias, sealed, bytes("aad_hex")))
    }

    @Test
    fun everyTruncationFailsAuthentication() {
        val store = fixedStore()
        store.create(alias)
        val sealed = bytes("sealed_hex")
        for (length in 0 until sealed.size) {
            assertEquals(failure(KeyFailure.AUTHENTICATION_FAILED), store.open(alias, sealed.copyOf(length), bytes("aad_hex")))
        }
    }

    @Test
    fun randomizedIvAndEmptyPlaintextAndAadRoundTrip() {
        val store = SyntheticInstallationKeyStore()
        store.create(alias)
        val first = opened(store.seal(alias, byteArrayOf(), byteArrayOf()))
        val second = opened(store.seal(alias, byteArrayOf(), byteArrayOf()))
        assertEquals(29, first.size)
        assertEquals(1.toByte(), first[0])
        assertFalse(first.copyOfRange(1, 13).contentEquals(second.copyOfRange(1, 13)))
        assertArrayEquals(byteArrayOf(), opened(store.open(alias, first, byteArrayOf())))
    }

    @Test
    fun transientFailuresPreserveKeysAndRecoverWithoutReplacement() {
        var generated = 0
        val store = SyntheticInstallationKeyStore({ generated++; bytes("key_hex") }, { bytes("iv_hex") })
        store.create(alias)
        val sealed = opened(store.seal(alias, bytes("plaintext_hex"), bytes("aad_hex")))
        store.setUnavailable(true)
        val unavailable = failure(KeyFailure.KEYSTORE_UNAVAILABLE)
        assertEquals(KeyStatus.UNAVAILABLE, store.keyStatus(alias))
        assertEquals(unavailable, store.create(alias))
        assertEquals(unavailable, store.create("synthetic-other"))
        assertEquals(unavailable, store.seal(alias, bytes("plaintext_hex"), bytes("aad_hex")))
        assertEquals(unavailable, store.open(alias, sealed, bytes("aad_hex")))
        assertEquals(unavailable, store.delete(alias))
        assertEquals(failure(KeyFailure.UNSUPPORTED_FORMAT), store.open(alias, byteArrayOf(2), bytes("aad_hex")))
        assertEquals(failure(KeyFailure.AUTHENTICATION_FAILED), store.open(alias, byteArrayOf(1), bytes("aad_hex")))
        store.setUnavailable(false)
        assertEquals(KeyStatus.PRESENT, store.keyStatus(alias))
        assertEquals(KeyStatus.MISSING, store.keyStatus("synthetic-other"))
        assertEquals(1, generated)
        assertEquals(failure(KeyFailure.KEY_EXISTS), store.create(alias))
        assertArrayEquals(bytes("plaintext_hex"), opened(store.open(alias, sealed, bytes("aad_hex"))))
        assertArrayEquals(bytes("sealed_hex"), opened(store.seal(alias, bytes("plaintext_hex"), bytes("aad_hex"))))
        store.invalidate(alias)
        store.setUnavailable(true)
        assertEquals(KeyStatus.UNAVAILABLE, store.keyStatus(alias))
        store.setUnavailable(false)
        assertEquals(KeyStatus.UNUSABLE, store.keyStatus(alias))
        assertEquals(failure(KeyFailure.UNUSABLE_KEY), store.open(alias, sealed, bytes("aad_hex")))
    }
}
