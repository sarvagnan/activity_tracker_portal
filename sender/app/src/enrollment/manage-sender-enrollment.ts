import { decodeMessage, encodeMessage, installationFingerprint } from "@activity-tracker-portal/ingest-protocol";
import { appendOutboxMessages, lastOutboxSequence, outboxBacklog, outboxHolds, outboxAad } from "../outbox/stage-outbox-batch.ts";
import { storageLane } from "../storage/storage-lane.ts";
import type { DeliveryContext } from "../delivery/send-outbox-message.ts";
import type { CipherFailure, InstallationCipher } from "../installation-cipher.ts";
import type { StagingContext } from "../outbox/stage-outbox-batch.ts";
import type { DatabaseRow, SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";

/** The Portal route that answers an enrollment confirmation (manage-sender-enrollment@1). */
export const CONFIRMATION_ROUTE = "/v1/enrollment-confirmation";
/** The Portal route that advertises the current capability generation. */
export const CAPABILITIES_ROUTE = "/v1/capabilities";
/** How long the transport waits for the Portal's answer before reporting no response. */
export const ENROLLMENT_TIMEOUT_MS = 30_000;
/** The longest answer body the unit decodes. */
export const MAX_ANSWER_BYTES = 65_536;

/** A scanned QR code's canonical enrollment-offer bytes, or a typed Portal URL and enrollment code. */
export type EnrollmentOffer =
  | { readonly kind: "scanned"; readonly bytes: Uint8Array }
  | { readonly kind: "typed"; readonly portalUrl: string; readonly enrollmentCode: string };

/** One HTTP request; the shell's adapter performs it exactly as given. */
export interface EnrollmentRequest {
  readonly method: "POST" | "GET";
  readonly url: string;
  readonly headers: { readonly "content-type": "application/json" } | Record<string, never>;
  /** The exact enrollment-confirmation bytes, or `null` for a capabilities request. */
  readonly body: Uint8Array | null;
  readonly timeoutMs: number;
  /** The adapter reads at most this many body bytes plus one. */
  readonly maxResponseBytes: number;
}

export type EnrollmentTransportResult =
  | { readonly outcome: "response"; readonly body: Uint8Array }
  | { readonly outcome: "failed" };

/** Performs one request; a timeout or network failure is `failed`. */
export interface EnrollmentTransport {
  send(request: EnrollmentRequest): Promise<EnrollmentTransportResult>;
}

export interface SenderEnrollmentOptions {
  readonly database: SenderDatabase;
  readonly cipher: InstallationCipher;
  readonly transport: EnrollmentTransport;
  readonly now: () => number;
  readonly randomUUID: () => string;
}

export type EnrollmentPhase = "confirming" | "refused" | "enrolled" | "draining";
export type PortalRefusal = "enrollment-refused" | "capabilities-unsupported";
export type EnrollmentRefusal =
  | "offer-invalid"
  | "already-enrolled"
  | "screen-lock-required"
  | "not-confirming"
  | "not-enrolled"
  | "not-confirmed"
  | "not-drained";

export interface EnrollmentStatus {
  readonly installationId: string;
  /** The fingerprint the Operator compares with the Portal's before confirming. */
  readonly fingerprint: string;
  readonly keyAlias: string;
  readonly portalUrl: string;
  readonly phase: EnrollmentPhase;
  /** The Portal's refusal while `refused`, otherwise `null`. */
  readonly refusal: PortalRefusal | null;
  /** Whether the enrollment acknowledgement's receipt has shown that the Operator confirmed the installation. */
  readonly confirmed: boolean;
  /** The adopted generation's staging context, or `null` before adoption. */
  readonly stagingContext: StagingContext | null;
  readonly deliveryContext: DeliveryContext;
  /** The installation's high-water mark while draining with an empty Outbox, otherwise `null`. */
  readonly finalSequence: number | null;
}

export type Refused = { readonly outcome: "refused"; readonly failure: EnrollmentRefusal };
/** The cipher's failure; a throwing or malformed cipher call reads as `keystore-unavailable`. */
export type CipherFailed = { readonly outcome: "cipher-failed"; readonly failure: CipherFailure | "key-exists" };
export type StorageFailed = { readonly outcome: "storage-failed" };

/** The outcome of one confirmation attempt. */
export type ConfirmationResult =
  | { readonly outcome: "enrolled"; readonly generation: number }
  | { readonly outcome: "portal-refused"; readonly failure: PortalRefusal }
  | { readonly outcome: "retryable"; readonly failure: "transport-failed" | "temporarily-unavailable" }
  | Refused
  | CipherFailed
  | StorageFailed;

export type RefreshResult =
  | { readonly outcome: "adopted"; readonly generation: number }
  | { readonly outcome: "current" | "incompatible" | "unavailable" | "draining" }
  | Refused
  | CipherFailed
  | StorageFailed;

export type DrainResult = { readonly outcome: "draining" } | Refused | StorageFailed;
export type DestructionResult = { readonly outcome: "destroyed" } | Refused | CipherFailed | StorageFailed;

export interface SenderEnrollment {
  /** Enroll from an offer, and make the first confirmation attempt. */
  enroll(offer: EnrollmentOffer): Promise<ConfirmationResult>;
  /** Repeat the confirmation attempt while confirming. */
  confirm(): Promise<ConfirmationResult>;
  /** Adopt a newer advertised generation before draining. */
  refreshCapabilities(): Promise<RefreshResult>;
  beginDrain(): Promise<DrainResult>;
  /** Destroy the installation after a complete drain, once the Operator says the Portal retired it. */
  unlink(): Promise<DestructionResult>;
  /** Destroy the installation in any phase, accepting that queued messages are lost. */
  reset(acknowledgement: { readonly acknowledgePossibleLoss: true }): Promise<DestructionResult>;
  status(): Promise<EnrollmentStatus | null | StorageFailed>;
}

/** Sender schema version 8: this unit's enrollment state. */
export const enrollmentMigration: Migration = Object.freeze({
  version: 8,
  async apply(transaction: SenderDatabase): Promise<void> {
    await transaction.execute(`CREATE TABLE sender_enrollment (
  singleton INTEGER PRIMARY KEY NOT NULL CHECK (singleton = 1),
  installation_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  key_alias TEXT NOT NULL,
  portal_url TEXT NOT NULL,
  confirmation BLOB NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('confirming', 'refused', 'enrolled', 'draining')),
  refusal TEXT CHECK (refusal IN ('enrollment-refused', 'capabilities-unsupported')),
  advertisement BLOB,
  generation INTEGER CHECK (generation > 0),
  acknowledgement_sequence INTEGER CHECK (acknowledgement_sequence > 0),
  enrolled_at_ms INTEGER NOT NULL,
  adopted_at_ms INTEGER,
  CHECK ((phase = 'refused') = (refusal IS NOT NULL)),
  CHECK ((phase IN ('enrolled', 'draining')) = (advertisement IS NOT NULL)),
  CHECK ((advertisement IS NULL) = (generation IS NULL)),
  CHECK ((advertisement IS NULL) = (acknowledgement_sequence IS NULL)),
  CHECK ((advertisement IS NULL) = (adopted_at_ms IS NULL))) STRICT`);
  },
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CIPHER_FAILURES: ReadonlySet<string> = new Set([
  "missing-key", "unusable-key", "keystore-unavailable", "authentication-failed", "unsupported-format",
]);

type Advertisement = {
  generation: number;
  wire_versions: number[];
  max_record_count: number;
  max_message_bytes: number;
  projection_schemas: { record_type: string; versions: number[] }[];
};

function hasMethods(value: unknown, methods: readonly string[]): boolean {
  return typeof value === "object" && value !== null &&
    methods.every((method) => typeof (value as Record<string, unknown>)[method] === "function");
}

function checkOffer(offer: EnrollmentOffer): void {
  if (typeof offer !== "object" || offer === null ||
      !(offer.kind === "scanned" && offer.bytes instanceof Uint8Array ||
        offer.kind === "typed" && typeof offer.portalUrl === "string" && typeof offer.enrollmentCode === "string")) {
    throw new TypeError("Invalid enrollment offer shape");
  }
}

function offerDetails(offer: EnrollmentOffer): { portalUrl: string; enrollmentCode: string } | null {
  try {
    // Apply the protocol's exact field rules to typed offers too; the Portal decides expiry.
    const message = offer.kind === "scanned" ? decodeMessage(offer.bytes) : decodeMessage(encodeMessage({
      wire_version: 1, kind: "enrollment-offer", portal_url: offer.portalUrl,
      enrollment_code: offer.enrollmentCode, expires_at: "2000-01-01T00:00:00.000Z",
    }));
    return message.kind === "enrollment-offer"
      ? { portalUrl: message.portal_url as string, enrollmentCode: message.enrollment_code as string } : null;
  } catch { return null; }
}

function decodeAnswer(bytes: Uint8Array): ReturnType<typeof decodeMessage> | null {
  if (bytes.length <= MAX_ANSWER_BYTES) {
    try { return decodeMessage(bytes); } catch { /* Undecodable answers establish no capabilities. */ }
  }
  return null;
}

const unavailableCipher = (): CipherFailed => ({ outcome: "cipher-failed", failure: "keystore-unavailable" });

export function createSenderEnrollment(options: SenderEnrollmentOptions): SenderEnrollment {
  if (typeof options !== "object" || options === null ||
      !hasMethods(options.database, ["execute", "query", "transaction", "close"]) ||
      !hasMethods(options.cipher, ["create", "seal", "delete", "isDeviceSecure"]) ||
      !hasMethods(options.transport, ["send"]) || typeof options.now !== "function" || typeof options.randomUUID !== "function") {
    throw new TypeError("Invalid Sender enrollment options");
  }
  const { database, cipher, transport, now, randomUUID } = options;
  const lane = storageLane(database);
  let actions: Promise<void> = Promise.resolve();

  function serialize<T>(operation: () => Promise<T>): Promise<T | StorageFailed> {
    // Caller validation precedes this boundary; storage TypeErrors are storage failures too.
    const result = actions.then(operation).catch((): StorageFailed => ({ outcome: "storage-failed" }));
    actions = result.then(() => undefined, () => undefined);
    return result;
  }

  function readTime(): number {
    const instant = now();
    if (!Number.isSafeInteger(instant)) throw new TypeError("Enrollment time must be a safe integer");
    return instant;
  }

  async function enrollmentRow(handle: SenderDatabase): Promise<DatabaseRow | null> {
    const [row] = await handle.query("SELECT * FROM sender_enrollment WHERE singleton = 1");
    return row ?? null;
  }

  async function send(request: EnrollmentRequest): Promise<Uint8Array | null> {
    let response: EnrollmentTransportResult | undefined;
    try { response = await transport.send(request); }
    catch { /* A throwing transport has no response. */ }
    return response?.outcome === "response" && response.body instanceof Uint8Array ? new Uint8Array(response.body) : null;
  }

  function adopt(mode: "confirm", row: DatabaseRow, bytes: Uint8Array, advertisement: Advertisement, instant: number): Promise<ConfirmationResult>;
  function adopt(mode: "refresh", row: DatabaseRow, bytes: Uint8Array, advertisement: Advertisement, instant: number): Promise<RefreshResult>;
  async function adopt(
    mode: "confirm" | "refresh", row: DatabaseRow, bytes: Uint8Array, advertisement: Advertisement, instant: number,
  ): Promise<ConfirmationResult | RefreshResult> {
    const installationId = row.installation_id as string;
    async function revalidate(handle: SenderDatabase): Promise<Refused | { outcome: "draining" | "current" } | null> {
      const current = await enrollmentRow(handle);
      if (mode === "confirm") {
        return current?.installation_id === installationId && current.phase === "confirming"
          ? null : { outcome: "refused", failure: "not-confirming" };
      }
      if (!current || current.installation_id !== installationId || current.generation === null || current.advertisement === null) {
        return { outcome: "refused", failure: "not-enrolled" };
      }
      if (current.phase === "draining") return { outcome: "draining" };
      if (current.phase !== "enrolled") return { outcome: "refused", failure: "not-enrolled" };
      return (current.generation as number) >= advertisement.generation ? { outcome: "current" } : null;
    }
    // Reset may already have deleted the old key while the Portal answer was pending.
    const prepared = await lane(async () => {
      const stale = await revalidate(database);
      return stale ?? await lastOutboxSequence(database, installationId);
    });
    if (typeof prepared !== "number") return prepared;
    const lastSequence = prepared;
    const sequence = lastSequence + 1;
    const acknowledgement = encodeMessage({
      wire_version: 1, kind: "capability-acknowledgement", sender_installation: installationId,
      sequence, capability_generation: advertisement.generation,
    });
    let sealed: Awaited<ReturnType<InstallationCipher["seal"]>> | undefined;
    try { sealed = await cipher.seal(row.key_alias as string, acknowledgement, outboxAad(installationId, sequence)); }
    catch { /* A throwing cipher is an unavailable Keystore. */ }
    if (sealed?.status === "error" && CIPHER_FAILURES.has(sealed.reason)) {
      return { outcome: "cipher-failed", failure: sealed.reason };
    }
    if (sealed?.status !== "ok" || !(sealed.value instanceof Uint8Array) || sealed.value.length === 0) return unavailableCipher();
    const ciphertext = sealed.value;
    return lane(() => database.transaction(async (transaction): Promise<ConfirmationResult | RefreshResult> => {
      // Sealing is asynchronous; identity, phase, and generation must still be eligible at commit.
      const stale = await revalidate(transaction);
      if (stale) return stale;
      if (!await appendOutboxMessages(transaction, installationId, lastSequence, [{
        kind: "capability-acknowledgement", sealed: ciphertext, messageBytes: acknowledgement.length,
      }])) throw new Error("Outbox high-water mark moved during adoption");
      await transaction.execute(`UPDATE sender_enrollment SET phase = 'enrolled', refusal = NULL,
        advertisement = ?, generation = ?, acknowledgement_sequence = COALESCE(acknowledgement_sequence, ?), adopted_at_ms = ?
        WHERE singleton = 1`, [bytes, advertisement.generation, sequence, instant]);
      return { outcome: mode === "confirm" ? "enrolled" : "adopted", generation: advertisement.generation };
    }));
  }

  async function attempt(row: DatabaseRow, instant: number): Promise<ConfirmationResult> {
    const bytes = await send({
      method: "POST", url: (row.portal_url as string) + CONFIRMATION_ROUTE, headers: { "content-type": "application/json" },
      body: new Uint8Array(row.confirmation as Uint8Array), timeoutMs: ENROLLMENT_TIMEOUT_MS, maxResponseBytes: MAX_ANSWER_BYTES,
    });
    if (bytes === null) return { outcome: "retryable", failure: "transport-failed" };
    const answer = decodeAnswer(bytes);
    if (answer?.kind === "retryable-failure") return { outcome: "retryable", failure: "temporarily-unavailable" };
    let failure: PortalRefusal = "enrollment-refused";
    if (answer?.kind === "capability-advertisement") {
      const advertisement = answer as unknown as Advertisement;
      if (advertisement.wire_versions.includes(1)) {
        return adopt("confirm", row, bytes, advertisement, instant);
      }
      failure = "capabilities-unsupported";
    }
    return lane(() => database.transaction(async (transaction): Promise<ConfirmationResult> => {
      const current = await enrollmentRow(transaction);
      if (current?.installation_id !== row.installation_id || current.phase !== "confirming") {
        return { outcome: "refused", failure: "not-confirming" };
      }
      await transaction.execute("UPDATE sender_enrollment SET phase = 'refused', refusal = ? WHERE singleton = 1", [failure]);
      return { outcome: "portal-refused", failure };
    }));
  }

  /** Called only with the shared lane held, including the Outbox observations. */
  async function statusFrom(handle: SenderDatabase, row: DatabaseRow): Promise<EnrollmentStatus> {
    const installationId = row.installation_id as string;
    const keyAlias = row.key_alias as string;
    const portalUrl = row.portal_url as string;
    let stagingContext: StagingContext | null = null;
    let confirmed = false;
    let finalSequence: number | null = null;
    if (row.advertisement !== null) {
      const advertisement = decodeMessage(row.advertisement as Uint8Array) as unknown as Advertisement;
      stagingContext = { installationId, keyAlias, generation: {
        generation: advertisement.generation, maxRecordCount: advertisement.max_record_count,
        maxMessageBytes: advertisement.max_message_bytes,
        projectionSchemas: Object.fromEntries(advertisement.projection_schemas.map((schema) => [schema.record_type, schema.versions])),
      } };
      const lastSequence = await lastOutboxSequence(handle, installationId);
      confirmed = lastSequence >= (row.acknowledgement_sequence as number) &&
        !await outboxHolds(handle, installationId, row.acknowledgement_sequence as number);
      if (row.phase === "draining" && (await outboxBacklog(handle, installationId)).messageCount === 0) {
        finalSequence = lastSequence;
      }
    }
    return {
      installationId, fingerprint: row.fingerprint as string, keyAlias, portalUrl, phase: row.phase as EnrollmentPhase,
      refusal: row.refusal as PortalRefusal | null, confirmed, stagingContext,
      deliveryContext: { installationId, keyAlias, portalUrl }, finalSequence,
    };
  }

  async function destroy(row: DatabaseRow | null): Promise<DestructionResult> {
    if (row) {
      let deleted: Awaited<ReturnType<InstallationCipher["delete"]>> | undefined;
      try { deleted = await cipher.delete(row.key_alias as string); }
      catch { /* A throwing cipher is an unavailable Keystore. */ }
      if (!(deleted?.status === "ok" && deleted.value === null ||
            deleted?.status === "error" && (deleted.reason === "missing-key" || deleted.reason === "unusable-key"))) {
        return unavailableCipher();
      }
    }
    await lane(() => database.transaction(async (transaction) => {
      // Key deletion (or waiting for the lane) may outlive the targeted installation.
      const current = await enrollmentRow(transaction);
      if (row ? current?.installation_id !== row.installation_id : current !== null) return;
      // Discover later units' tables without knowing their names or choosing a deletion order.
      await transaction.execute("PRAGMA defer_foreign_keys = ON");
      const tables = await transaction.query("SELECT name FROM sqlite_master WHERE type = 'table'");
      for (const table of tables) {
        const name = (table.name as string).replaceAll('"', '""');
        await transaction.execute(`DELETE FROM "${name}"`);
      }
    }));
    return { outcome: "destroyed" };
  }

  return {
    async enroll(offer) {
      checkOffer(offer);
      const instant = readTime();
      const installationId = randomUUID();
      if (typeof installationId !== "string" || !UUID.test(installationId)) throw new TypeError("Invalid installation UUID");
      const details = offerDetails(offer);
      return serialize(async (): Promise<ConfirmationResult> => {
        if (!details) return { outcome: "refused", failure: "offer-invalid" };
        if (await lane(() => enrollmentRow(database))) return { outcome: "refused", failure: "already-enrolled" };
        let secure: boolean;
        try { secure = await cipher.isDeviceSecure(); } catch { return unavailableCipher(); }
        if (secure !== true) return { outcome: "refused", failure: "screen-lock-required" };
        const fingerprint = installationFingerprint(installationId);
        const keyAlias = "activity-tracker-portal.installation." + installationId;
        let created: Awaited<ReturnType<InstallationCipher["create"]>> | undefined;
        try { created = await cipher.create(keyAlias); } catch { /* A throwing cipher is an unavailable Keystore. */ }
        if (created?.status !== "ok" || typeof created.value !== "object" || created.value === null ||
            typeof created.value.strongBox !== "boolean") {
          // Creation may have installed the key before failing or returning malformed metadata.
          // key-exists is different: that key predates this call and must be preserved.
          if (!(created?.status === "error" && created.reason === "key-exists")) {
            try { await cipher.delete(keyAlias); } catch { /* Cleanup cannot override the create failure. */ }
          }
          return created?.status === "error" && ["key-exists", "unusable-key", "keystore-unavailable"].includes(created.reason)
            ? { outcome: "cipher-failed", failure: created.reason } : unavailableCipher();
        }
        const confirmation = encodeMessage({
          wire_version: 1, kind: "enrollment-confirmation", enrollment_code: details.enrollmentCode,
          sender_installation: installationId, fingerprint,
        });
        try {
          await lane(() => database.transaction(async (transaction) => {
            await transaction.execute(`INSERT INTO sender_enrollment
              (singleton, installation_id, fingerprint, key_alias, portal_url, confirmation, phase, enrolled_at_ms)
              VALUES (1, ?, ?, ?, ?, ?, 'confirming', ?)`,
            [installationId, fingerprint, keyAlias, details.portalUrl, confirmation, instant]);
          }));
        } catch {
          try { await cipher.delete(keyAlias); } catch { /* An unused key cannot override the storage failure. */ }
          return { outcome: "storage-failed" };
        }
        return attempt({ installation_id: installationId, key_alias: keyAlias, portal_url: details.portalUrl, confirmation }, instant);
      });
    },
    async confirm() {
      const instant = readTime();
      return serialize(async (): Promise<ConfirmationResult> => {
        const row = await lane(() => enrollmentRow(database));
        return row?.phase === "confirming" ? attempt(row, instant) : { outcome: "refused", failure: "not-confirming" };
      });
    },
    async refreshCapabilities() {
      const instant = readTime();
      return serialize(async (): Promise<RefreshResult> => {
        const row = await lane(() => enrollmentRow(database));
        if (!row || row.advertisement === null) return { outcome: "refused", failure: "not-enrolled" };
        if (row.phase === "draining") return { outcome: "draining" };
        const bytes = await send({
          method: "GET", url: (row.portal_url as string) + CAPABILITIES_ROUTE, headers: {}, body: null,
          timeoutMs: ENROLLMENT_TIMEOUT_MS, maxResponseBytes: MAX_ANSWER_BYTES,
        });
        if (bytes === null) return { outcome: "unavailable" };
        const answer = decodeAnswer(bytes);
        if (answer?.kind !== "capability-advertisement") return { outcome: "unavailable" };
        const advertisement = answer as unknown as Advertisement;
        if (advertisement.generation <= (row.generation as number)) return { outcome: "current" };
        if (!advertisement.wire_versions.includes(1)) return { outcome: "incompatible" };
        return adopt("refresh", row, bytes, advertisement, instant);
      });
    },
    async beginDrain() {
      return serialize(() => lane(() => database.transaction(async (transaction): Promise<DrainResult> => {
        const row = await enrollmentRow(transaction);
        if (!row || row.advertisement === null) return { outcome: "refused", failure: "not-enrolled" };
        if (row.phase === "draining") return { outcome: "draining" };
        if (!(await statusFrom(transaction, row)).confirmed) return { outcome: "refused", failure: "not-confirmed" };
        await transaction.execute("UPDATE sender_enrollment SET phase = 'draining' WHERE singleton = 1");
        return { outcome: "draining" };
      })));
    },
    async unlink() {
      return serialize(async (): Promise<DestructionResult> => {
        const row = await lane(async () => {
          const current = await enrollmentRow(database);
          return current?.phase === "draining" && (await statusFrom(database, current)).finalSequence !== null ? current : null;
        });
        return row ? destroy(row) : { outcome: "refused", failure: "not-drained" };
      });
    },
    async reset(acknowledgement) {
      if (typeof acknowledgement !== "object" || acknowledgement === null || acknowledgement.acknowledgePossibleLoss !== true) {
        throw new TypeError("Reset requires acknowledgement of possible loss");
      }
      return serialize(async () => destroy(await lane(() => enrollmentRow(database))));
    },
    async status() {
      try {
        return await lane(async () => {
          const row = await enrollmentRow(database);
          return row ? statusFrom(database, row) : null;
        });
      } catch { return { outcome: "storage-failed" }; }
    },
  };
}
