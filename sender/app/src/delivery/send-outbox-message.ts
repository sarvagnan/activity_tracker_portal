import type { InstallationCipher } from "../installation-cipher.ts";
import type { OutboxKind } from "../outbox/stage-outbox-batch.ts";
import type { SenderDatabase } from "../storage/database.ts";
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
  async apply(_transaction: SenderDatabase): Promise<void> {
    throw new Error("send-outbox-message@1 is not implemented");
  },
});

export function createOutboxSender(_options: OutboxSenderOptions): OutboxSender {
  throw new Error("send-outbox-message@1 is not implemented");
}
