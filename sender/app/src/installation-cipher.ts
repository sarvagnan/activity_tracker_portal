export type InstallationKeyStatus = "present" | "missing" | "unusable" | "unavailable";
export type CipherFailure = "missing-key" | "unusable-key" | "keystore-unavailable" | "authentication-failed" | "unsupported-format";
export type CipherResult<T, Failure extends string = CipherFailure> =
  | { status: "ok"; value: T }
  | { status: "error"; reason: Failure };

/** No lifecycle or screen-lock policy: callers own aliases, AAD, and reactions. */
export interface InstallationCipher {
  create(alias: string): Promise<CipherResult<{ strongBox: boolean }, "key-exists" | "unusable-key" | "keystore-unavailable">>;
  keyStatus(alias: string): Promise<InstallationKeyStatus>;
  seal(alias: string, plaintext: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>>;
  open(alias: string, sealed: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>>;
  delete(alias: string): Promise<CipherResult<null, "missing-key" | "unusable-key" | "keystore-unavailable">>;
  isDeviceSecure(): Promise<boolean>;
}
