import type { DeliveryContext } from "../delivery/send-outbox-message.ts";
import type { CipherFailure, InstallationCipher } from "../installation-cipher.ts";
import type { StagingContext } from "../outbox/stage-outbox-batch.ts";
import type { SenderDatabase } from "../storage/database.ts";
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
  apply(): void {
    throw new Error("manage-sender-enrollment@1 is not implemented");
  },
});

export function createSenderEnrollment(options: SenderEnrollmentOptions): SenderEnrollment {
  void options;
  throw new Error("manage-sender-enrollment@1 is not implemented");
}
