// Wire Protocol Version 1 for Sender ingress (`ingest-protocol@1`).
//
// This Interface is declared before its implementation so the acceptance pack
// can name it. Every function throws until the unit is implemented against the
// frozen pack. Field-level rules live in protocol/v1/README.md. The module runs
// under Node for tests and Hermes in the Sender, so it must not import Node
// built-in modules.

export type Message = Record<string, unknown>;

/** A named failure from the `ingest-protocol` Governed Scope contract. */
export class ProtocolRefusal extends Error {
  readonly failure: string;

  constructor(failure: string) {
    super(failure);
    this.name = "ProtocolRefusal";
    this.failure = failure;
  }
}

/** One projected source page and the limits of the generation it is staged under. */
export interface PageInput {
  sender_installation: string;
  capability_generation: number;
  record_type: string;
  projection_schema_version: number;
  cycle_kind: "initial-snapshot" | "reconciliation-snapshot" | "change-cycle";
  cycle_id: string;
  source_page_index: number;
  first_sequence: number;
  previous_chain: string;
  items: Message[];
  max_record_count: number;
  max_message_bytes: number;
}

export interface Partition {
  fragments: Uint8Array[];
  next_chain: string;
  next_sequence: number;
}

/** The identity of the Outbox Message a Portal response answers. */
export interface SentMessage {
  sender_installation: string;
  sequence: number;
  message_sha256: string;
}

export type ResponseOutcome =
  | { outcome: "accepted" }
  | { outcome: "retryable"; failure: string }
  | { outcome: "block"; failure: string };

function notImplemented(): never {
  throw new Error("ingest-protocol@1 is not implemented");
}

/** Return the canonical bytes of a valid message value. */
export function encodeMessage(value: Message): Uint8Array {
  return notImplemented();
}

/** Return the validated message value, or throw ProtocolRefusal. */
export function decodeMessage(bytes: Uint8Array): Message {
  return notImplemented();
}

/** Return the lowercase hexadecimal SHA-256 identity of exact message bytes. */
export function messageSha256(bytes: Uint8Array): string {
  return notImplemented();
}

/** Return the fingerprint an enrollment confirmation must carry. */
export function installationFingerprint(senderInstallation: string): string {
  return notImplemented();
}

/** Return the starting fragment-chain value of a cycle. */
export function chainStart(cycleId: string): string {
  return notImplemented();
}

/** Return the fragment-chain value after one fragment. */
export function chainNext(previousChain: string, messageSha256: string): string {
  return notImplemented();
}

/** Partition one page into fragments, or throw ProtocolRefusal for an indivisible record. */
export function partitionPage(page: PageInput): Partition {
  return notImplemented();
}

/** Interpret the Portal's response to one sent Outbox Message. */
export function interpretResponse(sent: SentMessage, response: Uint8Array): ResponseOutcome {
  return notImplemented();
}
