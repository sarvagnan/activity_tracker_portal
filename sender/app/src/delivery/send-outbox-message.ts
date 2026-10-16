import { decodeMessage, interpretResponse, messageSha256 } from "@activity-tracker-portal/ingest-protocol";
import type { InstallationCipher } from "../installation-cipher.ts";
import { outboxAad } from "../outbox/stage-outbox-batch.ts";
import { storageLane } from "../storage/storage-lane.ts";
import type { OutboxKind } from "../outbox/stage-outbox-batch.ts";
import type { DatabaseRow, SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";

/** The Portal route that receives Outbox Messages (send-outbox-message@1). */
export const OUTBOX_ROUTE = "/v1/outbox-messages";
/** How long the transport waits for the Portal's answer before reporting no response. */
export const DELIVERY_TIMEOUT_MS = 60_000;
/** The longest response body the unit decodes; every protocol answer is far shorter. */
export const MAX_RESPONSE_BYTES = 4096;

/** The installation an Outbox Sender delivers for, as manage-sender-enrollment supplies it. */
export interface DeliveryContext {
  readonly installationId: string;
  readonly keyAlias: string;
  /** The enrollment offer's `portal_url`: a scheme, host, and optional port. */
  readonly portalUrl: string;
}

/** One HTTP request; the shell's adapter performs it exactly as given. */
export interface OutboxRequest {
  readonly method: "POST";
  readonly url: string;
  readonly headers: { readonly "content-type": "application/json" };
  /** The head's exact uncompressed message bytes. */
  readonly body: Uint8Array;
  readonly timeoutMs: number;
  /** The adapter reads at most this many body bytes plus one. */
  readonly maxResponseBytes: number;
}

export type TransportResult =
  | { readonly outcome: "response"; readonly body: Uint8Array }
  | { readonly outcome: "failed" };

/** Performs one request; a timeout or network failure is `failed`. */
export interface OutboxTransport {
  send(request: OutboxRequest): Promise<TransportResult>;
}

/** Raises one health-data-free local notification naming a head block's failure. */
export interface DeliveryNotifier {
  notify(failure: string): Promise<void>;
}

export interface OutboxSenderOptions {
  readonly database: SenderDatabase;
  readonly cipher: InstallationCipher;
  readonly transport: OutboxTransport;
  readonly notifier: DeliveryNotifier;
  readonly now: () => number;
  readonly context: DeliveryContext;
}

export type RetryableFailure =
  | "transport-failed"
  | "receipt-mismatch"
  | "temporarily-unavailable"
  | "storage-reserve-exhausted"
  | "keystore-unavailable";

export type StorageFailed = { readonly outcome: "storage-failed" };

export type SendResult =
  | { readonly outcome: "empty" }
  | { readonly outcome: "delivered"; readonly sequence: number; readonly kind: OutboxKind }
  | { readonly outcome: "retryable"; readonly sequence: number; readonly failure: RetryableFailure }
  /** The Portal's Delivery Block failure, `head-unreadable`, or `head-invalid`. */
  | { readonly outcome: "blocked"; readonly sequence: number; readonly failure: string }
  | { readonly outcome: "cipher-failed"; readonly sequence: number; readonly failure: "missing-key" | "unusable-key" }
  | StorageFailed;

/** The installation's persistent Operator-action-required head block. */
export interface HeadBlock {
  readonly sequence: number;
  readonly origin: "portal" | "sender";
  readonly failure: string;
  /** `null` when the head could not be opened. */
  readonly messageSha256: string | null;
  readonly firstSeenMs: number;
  readonly lastSeenMs: number;
  readonly count: number;
}

export interface OutboxSender {
  /** One delivery attempt for the installation's oldest Outbox Message. */
  sendNext(): Promise<SendResult>;
  /** The installation's head block, or `null`. */
  headBlock(): Promise<HeadBlock | null | StorageFailed>;
}

/** Sender schema version 7: this unit's head-block state. */
export const deliveryMigration: Migration = Object.freeze({
  version: 7,
  async apply(transaction: SenderDatabase): Promise<void> {
    await transaction.execute(`CREATE TABLE outbox_head_block (
  installation_id TEXT PRIMARY KEY NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  origin TEXT NOT NULL CHECK (origin IN ('portal', 'sender')),
  failure TEXT NOT NULL,
  message_sha256 TEXT,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  count INTEGER NOT NULL CHECK (count > 0),
  notified INTEGER NOT NULL CHECK (notified IN (0, 1)),
  CHECK ((origin = 'sender') = (failure IN ('head-unreadable', 'head-invalid'))),
  CHECK ((message_sha256 IS NULL) = (failure = 'head-unreadable'))) STRICT`);
  },
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PORTAL_URL = /^https?:\/\/[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$/;
const OUTBOX_KINDS: ReadonlySet<string> = new Set([
  "capability-acknowledgement", "ingest-batch", "snapshot-completion", "change-completion", "sync-checkpoint",
]);

type BlockAnswer = Pick<HeadBlock, "origin" | "failure" | "messageSha256">;

function hasMethods(value: unknown, methods: readonly string[]): boolean {
  return typeof value === "object" && value !== null &&
    methods.every((method) => typeof (value as Record<string, unknown>)[method] === "function");
}

function headBlockFrom(row: DatabaseRow): HeadBlock {
  return {
    sequence: row.sequence as number, origin: row.origin as HeadBlock["origin"], failure: row.failure as string,
    messageSha256: row.message_sha256 as string | null, firstSeenMs: row.first_seen_ms as number,
    lastSeenMs: row.last_seen_ms as number, count: row.count as number,
  };
}

export function createOutboxSender(options: OutboxSenderOptions): OutboxSender {
  if (typeof options !== "object" || options === null ||
      !hasMethods(options.database, ["execute", "query", "transaction", "close"]) ||
      !hasMethods(options.cipher, ["open"]) || !hasMethods(options.transport, ["send"]) ||
      !hasMethods(options.notifier, ["notify"]) || typeof options.now !== "function" ||
      typeof options.context !== "object" || options.context === null ||
      typeof options.context.installationId !== "string" || !UUID.test(options.context.installationId) ||
      typeof options.context.keyAlias !== "string" || options.context.keyAlias.length === 0 ||
      typeof options.context.portalUrl !== "string" || !PORTAL_URL.test(options.context.portalUrl)) {
    throw new TypeError("Invalid Outbox Sender options");
  }
  const { database, cipher, transport, notifier, now } = options;
  const { installationId, keyAlias, portalUrl } = options.context;
  const lane = storageLane(database);
  let attempts: Promise<void> = Promise.resolve();

  function serialize<T>(operation: () => Promise<T>): Promise<T> {
    const result = attempts.then(operation);
    attempts = result.then(() => undefined, () => undefined);
    return result;
  }

  async function recordBlock(sequence: number, answer: BlockAnswer, seenAtMs: number): Promise<void> {
    await lane(() => database.transaction(async (transaction) => {
      const [head] = await transaction.query(
        "SELECT sequence FROM outbox_messages WHERE installation_id = ? AND sequence = ?", [installationId, sequence],
      );
      // Another instance's receipt makes a late answer observational only.
      if (!head) return;
      const [previous] = await transaction.query(
        "SELECT sequence, first_seen_ms, count, notified FROM outbox_head_block WHERE installation_id = ?", [installationId],
      );
      const sameHead = previous?.sequence === sequence;
      await transaction.execute(`INSERT INTO outbox_head_block
        (installation_id, sequence, origin, failure, message_sha256, first_seen_ms, last_seen_ms, count, notified)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (installation_id) DO UPDATE SET
          sequence = excluded.sequence, origin = excluded.origin, failure = excluded.failure,
          message_sha256 = excluded.message_sha256, first_seen_ms = excluded.first_seen_ms,
          last_seen_ms = excluded.last_seen_ms, count = excluded.count, notified = excluded.notified`,
      [installationId, sequence, answer.origin, answer.failure, answer.messageSha256,
        sameHead ? previous.first_seen_ms : seenAtMs, seenAtMs,
        sameHead ? (previous.count as number) + 1 : 1, sameHead ? previous.notified : 0]);
    }));
  }

  async function notifyPending(sequence: number): Promise<void> {
    const [pending] = await lane(() => database.query(
      "SELECT failure, first_seen_ms FROM outbox_head_block WHERE installation_id = ? AND sequence = ? AND notified = 0",
      [installationId, sequence],
    ));
    if (!pending) return;
    // Keep the attempt queued, but let staging use storage while notification settles.
    try { await notifier.notify(pending.failure as string); }
    catch { return; }
    await lane(() => database.execute(
      `UPDATE outbox_head_block SET notified = 1
        WHERE installation_id = ? AND sequence = ? AND first_seen_ms = ? AND notified = 0`,
      [installationId, sequence, pending.first_seen_ms],
    ));
  }

  async function attempt(seenAtMs: number): Promise<SendResult> {
    try {
      const [row] = await lane(() => database.query(
        "SELECT sequence, kind, sealed, message_bytes FROM outbox_messages WHERE installation_id = ? ORDER BY sequence LIMIT 1",
        [installationId],
      ));
      if (!row) return { outcome: "empty" };
      const sequence = row.sequence as number;
      const kind = row.kind as OutboxKind;
      let result: SendResult;
      let blocked: BlockAnswer | undefined;
      let opened: Awaited<ReturnType<InstallationCipher["open"]>> | undefined;
      try { opened = await cipher.open(keyAlias, row.sealed as Uint8Array, outboxAad(installationId, sequence)); }
      catch { /* A throwing cipher is an unavailable Keystore. */ }
      if (opened?.status === "error" && (opened.reason === "missing-key" || opened.reason === "unusable-key")) {
        result = { outcome: "cipher-failed", sequence, failure: opened.reason };
      } else if (opened?.status === "error" && (opened.reason === "authentication-failed" || opened.reason === "unsupported-format")) {
        blocked = { origin: "sender", failure: "head-unreadable", messageSha256: null };
        result = { outcome: "blocked", sequence, failure: blocked.failure };
      } else if (opened?.status !== "ok" || !(opened.value instanceof Uint8Array)) {
        result = { outcome: "retryable", sequence, failure: "keystore-unavailable" };
      } else {
        const body = opened.value;
        const digest = messageSha256(body);
        let valid = false;
        try {
          const message = decodeMessage(body);
          valid = typeof message.kind === "string" && OUTBOX_KINDS.has(message.kind) && message.kind === kind &&
            message.sender_installation === installationId && message.sequence === sequence && body.length === row.message_bytes;
        } catch { /* Noncanonical or undecodable plaintext is not its row's message. */ }
        if (!valid) {
          blocked = { origin: "sender", failure: "head-invalid", messageSha256: digest };
          result = { outcome: "blocked", sequence, failure: blocked.failure };
        } else {
          let response: TransportResult | undefined;
          try {
            response = await transport.send({
              method: "POST", url: portalUrl + OUTBOX_ROUTE, headers: { "content-type": "application/json" },
              body, timeoutMs: DELIVERY_TIMEOUT_MS, maxResponseBytes: MAX_RESPONSE_BYTES,
            });
          } catch { /* Unknown outcomes keep the identical message for retry. */ }
          if (response?.outcome !== "response" || !(response.body instanceof Uint8Array)) {
            result = { outcome: "retryable", sequence, failure: "transport-failed" };
          } else {
            let answer: ReturnType<typeof interpretResponse> = { outcome: "retryable", failure: "receipt-mismatch" };
            if (response.body.length <= MAX_RESPONSE_BYTES) {
              try {
                answer = interpretResponse({ sender_installation: installationId, sequence, message_sha256: digest }, response.body);
              } catch { /* An unreadable answer cannot establish a receipt or block. */ }
            }
            if (answer.outcome === "accepted") {
              await lane(() => database.transaction(async (transaction) => {
                await transaction.execute("DELETE FROM outbox_messages WHERE installation_id = ? AND sequence = ?", [installationId, sequence]);
                await transaction.execute("DELETE FROM outbox_head_block WHERE installation_id = ? AND sequence = ?", [installationId, sequence]);
              }));
              return { outcome: "delivered", sequence, kind };
            }
            if (answer.outcome === "block") {
              blocked = { origin: "portal", failure: answer.failure, messageSha256: digest };
              result = { outcome: "blocked", sequence, failure: answer.failure };
            } else {
              result = { outcome: "retryable", sequence, failure: answer.failure as RetryableFailure };
            }
          }
        }
      }
      if (blocked) await recordBlock(sequence, blocked, seenAtMs);
      await notifyPending(sequence);
      return result;
    } catch { return { outcome: "storage-failed" }; }
  }

  return {
    async sendNext() {
      const seenAtMs = now();
      if (!Number.isSafeInteger(seenAtMs)) throw new TypeError("Delivery time must be a safe integer");
      return serialize(() => attempt(seenAtMs));
    },
    async headBlock() {
      try {
        const [row] = await lane(() => database.query("SELECT * FROM outbox_head_block WHERE installation_id = ?", [installationId]));
        return row ? headBlockFrom(row) : null;
      } catch { return { outcome: "storage-failed" } as const; }
    },
  };
}
