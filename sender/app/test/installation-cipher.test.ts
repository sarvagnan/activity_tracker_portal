import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { URL } from "node:url";
import type { CipherResult, InstallationCipher } from "../src/installation-cipher.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const vector = JSON.parse(readFileSync(new URL("./fixtures/sealed-box-v1.json", import.meta.url), "utf8")) as Record<string, string>;
const bytes = (field: string) => new Uint8Array(Buffer.from(vector[field], "hex"));
const alias = "synthetic-installation";
function fixedCipher() {
  return new NodeInstallationCipher({ keyBytes: () => bytes("key_hex"), ivBytes: () => bytes("iv_hex") });
}
function success<T>(result: CipherResult<T>): T {
  assert.equal(result.status, "ok");
  if (result.status !== "ok") throw new Error("Expected success");
  return result.value;
}

test("Node substitute produces and opens the shared Kotlin/Node v1 vector", async () => {
  const cipher: InstallationCipher = fixedCipher();
  assert.deepEqual(await cipher.create(alias), { status: "ok", value: { strongBox: false } });
  assert.deepEqual(success(await cipher.seal(alias, bytes("plaintext_hex"), bytes("aad_hex"))), bytes("sealed_hex"));
  assert.deepEqual(success(await cipher.open(alias, bytes("sealed_hex"), bytes("aad_hex"))), bytes("plaintext_hex"));
});

test("key creation never replaces an existing key, and delete is explicit", async () => {
  const cipher = fixedCipher();
  assert.equal(await cipher.keyStatus(alias), "missing");
  assert.deepEqual(await cipher.delete(alias), { status: "error", reason: "missing-key" });
  await cipher.create(alias);
  assert.equal(await cipher.keyStatus(alias), "present");
  assert.deepEqual(await cipher.create(alias), { status: "error", reason: "key-exists" });
  assert.deepEqual(success(await cipher.open(alias, bytes("sealed_hex"), bytes("aad_hex"))), bytes("plaintext_hex"));
  assert.deepEqual(await cipher.delete(alias), { status: "ok", value: null });
  assert.equal(await cipher.keyStatus(alias), "missing");
});

test("missing and unusable keys yield typed results without a device", async () => {
  const cipher = fixedCipher();
  for (const reason of ["missing-key", "unusable-key"] as const) {
    assert.deepEqual(await cipher.seal(alias, bytes("plaintext_hex"), bytes("aad_hex")), { status: "error", reason });
    assert.deepEqual(await cipher.open(alias, bytes("sealed_hex"), bytes("aad_hex")), { status: "error", reason });
    if (reason === "missing-key") { await cipher.create(alias); cipher.invalidate(alias); }
  }
  assert.equal(await cipher.keyStatus(alias), "unusable");
  assert.deepEqual(await cipher.create(alias), { status: "error", reason: "key-exists" });
  assert.deepEqual(await cipher.delete(alias), { status: "ok", value: null });
});

test("flipped ciphertext, IV, or tag bits and wrong AAD fail authentication", async () => {
  const cipher = fixedCipher();
  await cipher.create(alias);
  for (const index of [1, 13, bytes("sealed_hex").length - 1]) {
    const tampered = bytes("sealed_hex");
    tampered[index] ^= 1;
    assert.deepEqual(await cipher.open(alias, tampered, bytes("aad_hex")), { status: "error", reason: "authentication-failed" });
  }
  assert.deepEqual(await cipher.open(alias, bytes("sealed_hex"), new Uint8Array([99])), { status: "error", reason: "authentication-failed" });
});

test("unknown version is unsupported and every truncation fails authentication", async () => {
  const cipher = fixedCipher();
  await cipher.create(alias);
  const sealed = bytes("sealed_hex");
  for (let length = 0; length < sealed.length; length++) {
    assert.deepEqual(await cipher.open(alias, sealed.subarray(0, length), bytes("aad_hex")), { status: "error", reason: "authentication-failed" });
  }
  sealed[0] = 2;
  assert.deepEqual(await cipher.open(alias, sealed, bytes("aad_hex")), { status: "error", reason: "unsupported-format" });
});

test("production-like substitute uses randomized 12-byte IVs and supports empty plaintext and AAD", async () => {
  const cipher = new NodeInstallationCipher();
  await cipher.create(alias);
  const first = success(await cipher.seal(alias, new Uint8Array(), new Uint8Array()));
  const second = success(await cipher.seal(alias, new Uint8Array(), new Uint8Array()));
  assert.equal(first.length, 29);
  assert.equal(first[0], 1);
  assert.notDeepEqual(first.subarray(1, 13), second.subarray(1, 13));
  assert.deepEqual(success(await cipher.open(alias, first, new Uint8Array())), new Uint8Array());
});

test("binary subarrays preserve offsets and bytes through encryption", async () => {
  const cipher = new NodeInstallationCipher();
  await cipher.create(alias);
  const plaintext = new Uint8Array([99, 0, 128, 255, 99]).subarray(1, 4);
  const aad = new Uint8Array([99, 255, 0, 99]).subarray(1, 3);
  const sealed = success(await cipher.seal(alias, plaintext, aad));
  assert.deepEqual(success(await cipher.open(alias, sealed, aad)), plaintext);
});

test("device security substitute reports the observation without attaching policy", async () => {
  assert.equal(await new NodeInstallationCipher().isDeviceSecure(), false);
  assert.equal(await new NodeInstallationCipher({ deviceSecure: true }).isDeviceSecure(), true);
});

test("transient Keystore outages preserve keys and recover without replacement", async () => {
  let generated = 0;
  const cipher = new NodeInstallationCipher({ keyBytes: () => { generated++; return bytes("key_hex"); } });
  await cipher.create(alias);
  const sealed = success(await cipher.seal(alias, bytes("plaintext_hex"), bytes("aad_hex")));
  cipher.setUnavailable(true);
  assert.equal(await cipher.keyStatus(alias), "unavailable");
  const unavailable = { status: "error", reason: "keystore-unavailable" };
  assert.deepEqual(await cipher.create(alias), unavailable);
  assert.deepEqual(await cipher.create("synthetic-other"), unavailable);
  assert.deepEqual(await cipher.seal(alias, bytes("plaintext_hex"), bytes("aad_hex")), unavailable);
  assert.deepEqual(await cipher.open(alias, sealed, bytes("aad_hex")), unavailable);
  assert.deepEqual(await cipher.delete(alias), unavailable);
  assert.deepEqual(await cipher.open(alias, new Uint8Array([2]), bytes("aad_hex")), { status: "error", reason: "unsupported-format" });
  assert.deepEqual(await cipher.open(alias, new Uint8Array([1]), bytes("aad_hex")), { status: "error", reason: "authentication-failed" });
  cipher.setUnavailable(false);
  assert.equal(await cipher.keyStatus(alias), "present");
  assert.equal(await cipher.keyStatus("synthetic-other"), "missing");
  assert.equal(generated, 1);
  assert.deepEqual(await cipher.create(alias), { status: "error", reason: "key-exists" });
  assert.deepEqual(success(await cipher.open(alias, sealed, bytes("aad_hex"))), bytes("plaintext_hex"));
  assert.equal((await cipher.seal(alias, bytes("plaintext_hex"), bytes("aad_hex"))).status, "ok");
  cipher.invalidate(alias);
  cipher.setUnavailable(true);
  assert.equal(await cipher.keyStatus(alias), "unavailable");
  cipher.setUnavailable(false);
  assert.equal(await cipher.keyStatus(alias), "unusable");
  assert.deepEqual(await cipher.open(alias, sealed, bytes("aad_hex")), { status: "error", reason: "unusable-key" });
});
