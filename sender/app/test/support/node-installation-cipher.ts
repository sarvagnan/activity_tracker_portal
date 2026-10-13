import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import type { CipherResult, InstallationCipher, InstallationKeyStatus } from "../../src/installation-cipher.ts";

/** Synthetic CI substitute. Inject entropy only to reproduce committed vectors. */
export class NodeInstallationCipher implements InstallationCipher {
  private readonly keys = new Map<string, Uint8Array | null>();
  private readonly keyBytes: () => Uint8Array;
  private readonly ivBytes: () => Uint8Array;
  private readonly deviceSecure: boolean;
  private unavailable = false;

  /** Toggle a transient provider outage without changing any key. */
  setUnavailable(unavailable: boolean): void { this.unavailable = unavailable; }

  constructor(options: { keyBytes?: () => Uint8Array; ivBytes?: () => Uint8Array; deviceSecure?: boolean } = {}) {
    this.keyBytes = options.keyBytes ?? (() => randomBytes(32));
    this.ivBytes = options.ivBytes ?? (() => randomBytes(12));
    this.deviceSecure = options.deviceSecure ?? false;
  }
  async create(alias: string): ReturnType<InstallationCipher["create"]> {
    if (this.unavailable) return { status: "error", reason: "keystore-unavailable" };
    if (this.keys.has(alias)) return { status: "error", reason: "key-exists" };
    const key = this.keyBytes();
    if (key.length !== 32) throw new Error("Synthetic key must be 32 bytes");
    this.keys.set(alias, new Uint8Array(key));
    return { status: "ok", value: { strongBox: false } };
  }
  async keyStatus(alias: string): Promise<InstallationKeyStatus> {
    if (this.unavailable) return "unavailable";
    return !this.keys.has(alias) ? "missing" : this.keys.get(alias) === null ? "unusable" : "present";
  }
  /** Simulates an invalidated key without a device. */
  invalidate(alias: string): void {
    if (!this.keys.has(alias)) throw new Error("Cannot invalidate a missing synthetic key");
    this.keys.set(alias, null);
  }
  async seal(alias: string, plaintext: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>> {
    if (this.unavailable) return { status: "error", reason: "keystore-unavailable" };
    const key = this.keys.get(alias);
    if (key === undefined) return { status: "error", reason: "missing-key" };
    if (key === null) return { status: "error", reason: "unusable-key" };
    const iv = this.ivBytes();
    if (iv.length !== 12) throw new Error("Synthetic IV must be 12 bytes");
    const cipher = createCipheriv("aes-256-gcm", key, iv, { authTagLength: 16 });
    cipher.setAAD(aad);
    return { status: "ok", value: new Uint8Array(Buffer.concat([
      Buffer.from([1]), iv, cipher.update(plaintext), cipher.final(), cipher.getAuthTag(),
    ])) };
  }
  async open(alias: string, sealed: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>> {
    if (sealed.length === 0) return { status: "error", reason: "authentication-failed" };
    if (sealed[0] !== 1) return { status: "error", reason: "unsupported-format" };
    if (sealed.length < 29) return { status: "error", reason: "authentication-failed" };
    if (this.unavailable) return { status: "error", reason: "keystore-unavailable" };
    const key = this.keys.get(alias);
    if (key === undefined) return { status: "error", reason: "missing-key" };
    if (key === null) return { status: "error", reason: "unusable-key" };
    const cipher = createDecipheriv("aes-256-gcm", key, sealed.subarray(1, 13), { authTagLength: 16 });
    cipher.setAAD(aad);
    cipher.setAuthTag(sealed.subarray(-16));
    try {
      return { status: "ok", value: new Uint8Array(Buffer.concat([cipher.update(sealed.subarray(13, -16)), cipher.final()])) };
    } catch {
      return { status: "error", reason: "authentication-failed" };
    }
  }
  async delete(alias: string): ReturnType<InstallationCipher["delete"]> {
    if (this.unavailable) return { status: "error", reason: "keystore-unavailable" };
    if (!this.keys.delete(alias)) return { status: "error", reason: "missing-key" };
    return { status: "ok", value: null };
  }
  async isDeviceSecure(): Promise<boolean> { return this.deviceSecure; }
}
