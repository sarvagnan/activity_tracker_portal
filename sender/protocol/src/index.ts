// Wire Protocol Version 1 for Sender ingress (`ingest-protocol@1`).
//
// This module is pure: it encodes, decodes, partitions, and interprets messages
// without storage, network, or clock effects. Field-level rules live in
// protocol/v1/README.md, and the shared vectors under protocol/v1/vectors/ are
// the oracle. The module runs under Node for tests and Hermes in the Sender, so
// it must not import Node built-in modules.

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

const MAX_INTEGER = Number.MAX_SAFE_INTEGER;
const MIN_MESSAGE_BYTES = 4096;
const MAX_DEPTH = 32;

function malformed(): never {
  throw new ProtocolRefusal("malformed-message");
}

// UTF-8

function utf8Encode(text: string): Uint8Array {
  const bytes: number[] = [];
  for (let index = 0; index < text.length; index++) {
    let code = text.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdfff) {
      const low = text.charCodeAt(index + 1);
      if (code > 0xdbff || !(low >= 0xdc00 && low <= 0xdfff)) {
        malformed();
      }
      code = 0x10000 + ((code - 0xd800) << 10) + (low - 0xdc00);
      index++;
    }
    if (code < 0x80) {
      bytes.push(code);
    } else if (code < 0x800) {
      bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
    } else if (code < 0x10000) {
      bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    } else {
      bytes.push(0xf0 | (code >> 18), 0x80 | ((code >> 12) & 0x3f), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    }
  }
  return Uint8Array.from(bytes);
}

function utf8Decode(bytes: Uint8Array): string {
  const units: number[] = [];
  let index = 0;
  while (index < bytes.length) {
    const lead = bytes[index];
    if (lead < 0x80) {
      units.push(lead);
      index++;
      continue;
    }
    let continuation: number;
    let code: number;
    let minimum: number;
    if (lead >= 0xc2 && lead <= 0xdf) {
      [continuation, code, minimum] = [1, lead & 0x1f, 0x80];
    } else if (lead >= 0xe0 && lead <= 0xef) {
      [continuation, code, minimum] = [2, lead & 0x0f, 0x800];
    } else if (lead >= 0xf0 && lead <= 0xf4) {
      [continuation, code, minimum] = [3, lead & 0x07, 0x10000];
    } else {
      return malformed();
    }
    if (index + continuation >= bytes.length) {
      malformed();
    }
    for (let offset = 1; offset <= continuation; offset++) {
      const byte = bytes[index + offset];
      if ((byte & 0xc0) !== 0x80) {
        malformed();
      }
      code = (code << 6) | (byte & 0x3f);
    }
    if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) {
      malformed();
    }
    if (code >= 0x10000) {
      code -= 0x10000;
      units.push(0xd800 + (code >> 10), 0xdc00 + (code & 0x3ff));
    } else {
      units.push(code);
    }
    index += continuation + 1;
  }
  let text = "";
  for (let start = 0; start < units.length; start += 8192) {
    text += String.fromCharCode(...units.slice(start, start + 8192));
  }
  return text;
}

// SHA-256 and hexadecimal

const ROUND_CONSTANTS = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
  0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
  0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08,
  0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotateRight(value: number, bits: number): number {
  return (value >>> bits) | (value << (32 - bits));
}

function sha256(bytes: Uint8Array): Uint8Array {
  const padded = new Uint8Array(Math.ceil((bytes.length + 9) / 64) * 64);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(bytes.length / 0x20000000));
  view.setUint32(padded.length - 4, (bytes.length << 3) >>> 0);
  const state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const schedule = new Uint32Array(64);
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let round = 0; round < 16; round++) {
      schedule[round] = view.getUint32(offset + round * 4);
    }
    for (let round = 16; round < 64; round++) {
      const w15 = schedule[round - 15];
      const w2 = schedule[round - 2];
      const sigma0 = rotateRight(w15, 7) ^ rotateRight(w15, 18) ^ (w15 >>> 3);
      const sigma1 = rotateRight(w2, 17) ^ rotateRight(w2, 19) ^ (w2 >>> 10);
      schedule[round] = (schedule[round - 16] + sigma0 + schedule[round - 7] + sigma1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let round = 0; round < 64; round++) {
      const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choice = (e & f) ^ (~e & g);
      const first = (h + sum1 + choice + ROUND_CONSTANTS[round] + schedule[round]) >>> 0;
      const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const second = (sum0 + majority) >>> 0;
      [h, g, f, e, d, c, b, a] = [g, f, e, (d + first) >>> 0, c, b, a, (first + second) >>> 0];
    }
    [a, b, c, d, e, f, g, h].forEach((value, index) => {
      state[index] = (state[index] + value) >>> 0;
    });
  }
  const digest = new Uint8Array(32);
  const digestView = new DataView(digest.buffer);
  state.forEach((value, index) => digestView.setUint32(index * 4, value));
  return digest;
}

function toHex(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) {
    text += byte.toString(16).padStart(2, "0");
  }
  return text;
}

function fromSha256Hex(text: string): Uint8Array {
  if (!/^[0-9a-f]{64}$/.test(text)) {
    throw new TypeError("expected a lowercase hexadecimal SHA-256 value");
  }
  const bytes = new Uint8Array(32);
  for (let index = 0; index < 32; index++) {
    bytes[index] = parseInt(text.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}

// Strict JSON parsing (decoding step 1)

function parseJson(text: string): unknown {
  let index = 0;

  const skipWhitespace = (): void => {
    while (index < text.length) {
      const code = text.charCodeAt(index);
      if (code !== 0x20 && code !== 0x09 && code !== 0x0a && code !== 0x0d) {
        return;
      }
      index++;
    }
  };

  const hexUnit = (): number => {
    const digits = text.slice(index, index + 4);
    if (!/^[0-9A-Fa-f]{4}$/.test(digits)) {
      malformed();
    }
    index += 4;
    return parseInt(digits, 16);
  };

  const parseString = (): string => {
    index++;
    let result = "";
    let chunkStart = index;
    for (;;) {
      if (index >= text.length) {
        malformed();
      }
      const code = text.charCodeAt(index);
      if (code === 0x22) {
        result += text.slice(chunkStart, index);
        index++;
        return result;
      }
      if (code < 0x20) {
        malformed();
      }
      if (code !== 0x5c) {
        index++;
        continue;
      }
      result += text.slice(chunkStart, index);
      const escape = text[index + 1];
      index += 2;
      const simple: Record<string, string> = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
      if (escape !== undefined && Object.prototype.hasOwnProperty.call(simple, escape)) {
        result += simple[escape];
      } else if (escape === "u") {
        const unit = hexUnit();
        if (unit >= 0xd800 && unit <= 0xdbff) {
          if (text.slice(index, index + 2) !== "\\u") {
            malformed();
          }
          index += 2;
          const low = hexUnit();
          if (low < 0xdc00 || low > 0xdfff) {
            malformed();
          }
          result += String.fromCharCode(unit, low);
        } else if (unit >= 0xdc00 && unit <= 0xdfff) {
          malformed();
        } else {
          result += String.fromCharCode(unit);
        }
      } else {
        malformed();
      }
      chunkStart = index;
    }
  };

  const parseInteger = (): number => {
    const start = index;
    if (text[index] === "0") {
      index++;
    } else {
      while (index < text.length && text[index] >= "0" && text[index] <= "9") {
        index++;
      }
    }
    const following = text[index];
    if (following === "." || following === "e" || following === "E" || (following >= "0" && following <= "9")) {
      malformed();
    }
    const token = text.slice(start, index);
    if (token.length > 16 || (token.length === 16 && token > String(MAX_INTEGER))) {
      malformed();
    }
    return Number(token);
  };

  const literal = (word: string, value: unknown): unknown => {
    if (!text.startsWith(word, index)) {
      malformed();
    }
    index += word.length;
    return value;
  };

  const parseValue = (depth: number): unknown => {
    if (depth > MAX_DEPTH) {
      malformed();
    }
    skipWhitespace();
    const char = text[index];
    if (char === "{") {
      index++;
      const result: Message = {};
      skipWhitespace();
      if (text[index] === "}") {
        index++;
        return result;
      }
      for (;;) {
        skipWhitespace();
        if (text[index] !== '"') {
          malformed();
        }
        const key = parseString();
        if (Object.prototype.hasOwnProperty.call(result, key)) {
          malformed();
        }
        skipWhitespace();
        if (text[index] !== ":") {
          malformed();
        }
        index++;
        const value = parseValue(depth + 1);
        Object.defineProperty(result, key, { value, enumerable: true, writable: true, configurable: true });
        skipWhitespace();
        if (text[index] === ",") {
          index++;
        } else if (text[index] === "}") {
          index++;
          return result;
        } else {
          malformed();
        }
      }
    }
    if (char === "[") {
      index++;
      const result: unknown[] = [];
      skipWhitespace();
      if (text[index] === "]") {
        index++;
        return result;
      }
      for (;;) {
        result.push(parseValue(depth + 1));
        skipWhitespace();
        if (text[index] === ",") {
          index++;
        } else if (text[index] === "]") {
          index++;
          return result;
        } else {
          malformed();
        }
      }
    }
    if (char === '"') {
      return parseString();
    }
    if (char === "t") {
      return literal("true", true);
    }
    if (char === "f") {
      return literal("false", false);
    }
    if (char === "n") {
      return literal("null", null);
    }
    if (char !== undefined && char >= "0" && char <= "9") {
      return parseInteger();
    }
    return malformed();
  };

  const value = parseValue(0);
  skipWhitespace();
  if (index !== text.length) {
    malformed();
  }
  return value;
}

// Canonical encoding (RFC 8785 over the envelope value domain)

function canonicalText(value: unknown): string {
  if (typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalText).join(",")}]`;
  }
  const object = value as Message;
  const keys = Object.keys(object).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalText(object[key])}`).join(",")}}`;
}

// Field validators

type Validator = (value: unknown) => boolean;

function isObject(value: unknown): value is Message {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const isCount: Validator = (value) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const isPositive: Validator = (value) => isCount(value) && (value as number) >= 1;
const isBoolean: Validator = (value) => typeof value === "boolean";

function pattern(expression: RegExp): Validator {
  return (value) => typeof value === "string" && expression.test(value);
}

function oneOf(...choices: string[]): Validator {
  return (value) => typeof value === "string" && choices.includes(value);
}

const isUuid = pattern(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const isSha256 = pattern(/^[0-9a-f]{64}$/);
const isRecordType: Validator = (value) =>
  typeof value === "string" && value.length <= 64 && /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/.test(value);
const isBase64 = pattern(
  /^(?:(?:[A-Za-z0-9+/]{4})+|(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/][AQgw]==|[A-Za-z0-9+/]{2}[AEIMQUYcgkosw048]=))$/,
);
const isEnrollmentCode = pattern(/^[A-Z2-7]{16}$/);
const isPortalUrl = pattern(/^https?:\/\/[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$/);
const TIMESTAMP =
  /^([0-9]{4})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$/;

function daysInMonth(year: number, month: number): number {
  if (month === 2) {
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    return leap ? 29 : 28;
  }
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

const isTimestamp: Validator = (value) => {
  if (typeof value !== "string") {
    return false;
  }
  const match = TIMESTAMP.exec(value);
  return match !== null && Number(match[3]) <= daysInMonth(Number(match[1]), Number(match[2]));
};

const isSourceIdentity: Validator = (value) => {
  if (typeof value !== "string") {
    return false;
  }
  let length = 0;
  for (const char of value) {
    if ((char.codePointAt(0) as number) < 0x20) {
      return false;
    }
    length++;
  }
  return length >= 1 && length <= 256;
};

function isAscending(values: unknown[]): boolean {
  return values.every((value, index) => index === 0 || (values[index - 1] as number | string) < (value as number | string));
}

const isVersionList: Validator = (value) =>
  Array.isArray(value) && value.length > 0 && value.every(isPositive) && isAscending(value);

function hasOwn(object: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function hasFields(value: unknown, required: Record<string, Validator>, optional: Record<string, Validator> = {}): boolean {
  if (!isObject(value)) {
    return false;
  }
  const keys = Object.keys(value);
  if (!Object.keys(required).every((key) => hasOwn(value, key))) {
    return false;
  }
  if (!keys.every((key) => hasOwn(required, key) || hasOwn(optional, key))) {
    return false;
  }
  return keys.every((key) => (hasOwn(required, key) ? required[key] : optional[key])(value[key]));
}

const isProjectionSchemas: Validator = (value) =>
  Array.isArray(value) &&
  value.length > 0 &&
  value.every((item) => hasFields(item, { record_type: isRecordType, versions: isVersionList })) &&
  isAscending(value.map((item) => (item as Message).record_type));

const isItem: Validator = (value) => {
  if (!isObject(value)) {
    return false;
  }
  if (value.kind === "projection") {
    return hasFields(value, { kind: oneOf("projection"), data: isBase64 });
  }
  return hasFields(value, { kind: oneOf("retraction"), source_identity: isSourceIdentity });
};

const OUTCOMES = ["not-due", "no-change", "progressed", "completed", "deferred", "blocked", "not-permitted", "failed", "interrupted"];
const REASON_REQUIRED = ["deferred", "blocked", "failed"];
const REASON_OPTIONAL = ["progressed", "interrupted"];
const REASONS = [
  "collection-disabled",
  "projection-refusal",
  "record-too-large",
  "permission-missing",
  "delivery-blocked",
  "storage-pressure",
  "screen-lock-removed",
  "changes-token-expired",
  "time-budget-reached",
  "read-failed",
];

const isTypeStatus: Validator = (value) => {
  if (!hasFields(value, { record_type: isRecordType, outcome: oneOf(...OUTCOMES), source_data_staged: isBoolean }, { reason: oneOf(...REASONS) })) {
    return false;
  }
  const status = value as Message;
  if (REASON_REQUIRED.includes(status.outcome as string)) {
    return "reason" in status;
  }
  return !("reason" in status) || REASON_OPTIONAL.includes(status.outcome as string);
};

const isTypeStatuses: Validator = (value) =>
  Array.isArray(value) && value.every(isTypeStatus) && isAscending(value.map((item) => (item as Message).record_type));

const DELIVERY_BLOCK_FAILURES = [
  "malformed-message",
  "noncanonical-message",
  "unsupported-wire-protocol",
  "unsupported-source-projection-schema",
  "record-count-limit-exceeded",
  "uncompressed-byte-limit-exceeded",
  "sequence-content-mismatch",
  "sequence-out-of-order",
  "fragment-order-mismatch",
  "completion-identity-mismatch",
  "completion-kind-mismatch",
  "completion-count-mismatch",
  "completion-chain-mismatch",
  "completion-sequence-gap",
];

const COMMON: Record<string, Validator> = { wire_version: (value) => value === 1, kind: (value) => typeof value === "string" };
const OUTBOX: Record<string, Validator> = {
  ...COMMON,
  sender_installation: isUuid,
  sequence: isPositive,
  capability_generation: isPositive,
};
const COMPLETION: Record<string, Validator> = {
  ...OUTBOX,
  record_type: isRecordType,
  cycle_id: isUuid,
  page_count: isCount,
  fragment_count: isCount,
  record_count: isCount,
  final_chain: isSha256,
};

const KINDS: Record<string, [Record<string, Validator>, Record<string, Validator>]> = {
  "enrollment-offer": [{ ...COMMON, enrollment_code: isEnrollmentCode, portal_url: isPortalUrl, expires_at: isTimestamp }, {}],
  "enrollment-confirmation": [{ ...COMMON, enrollment_code: isEnrollmentCode, sender_installation: isUuid, fingerprint: isSha256 }, {}],
  "capability-advertisement": [
    {
      ...COMMON,
      generation: isPositive,
      wire_versions: isVersionList,
      projection_schemas: isProjectionSchemas,
      max_record_count: isPositive,
      max_message_bytes: (value) => isCount(value) && (value as number) >= MIN_MESSAGE_BYTES,
    },
    {},
  ],
  "capability-acknowledgement": [OUTBOX, {}],
  "ingest-batch": [
    {
      ...OUTBOX,
      record_type: isRecordType,
      projection_schema_version: isPositive,
      cycle_kind: oneOf("initial-snapshot", "reconciliation-snapshot", "change-cycle"),
      cycle_id: isUuid,
      source_page_index: isPositive,
      fragment_index: isPositive,
      previous_chain: isSha256,
      items: (value) => Array.isArray(value) && value.every(isItem),
    },
    {},
  ],
  "snapshot-completion": [{ ...COMPLETION, snapshot_kind: oneOf("initial", "reconciliation") }, {}],
  "change-completion": [COMPLETION, {}],
  "sync-checkpoint": [
    {
      ...OUTBOX,
      run_sequence: isPositive,
      trigger: oneOf("background", "foreground", "manual"),
      started_at: isTimestamp,
      ended_at: isTimestamp,
      background_scheduling: oneOf("available", "restricted", "unregistered"),
      record_types: isTypeStatuses,
      outbox_messages_before_checkpoint: isCount,
      outbox_bytes_before_checkpoint: isCount,
    },
    { elapsed_ms: isCount },
  ],
  "delivery-receipt": [{ ...COMMON, sender_installation: isUuid, sequence: isPositive, message_sha256: isSha256 }, {}],
  "retryable-failure": [{ ...COMMON, reason: oneOf("storage-reserve-exhausted", "temporarily-unavailable") }, {}],
  "delivery-block": [
    { ...COMMON, failure: oneOf(...DELIVERY_BLOCK_FAILURES), message_sha256: isSha256 },
    { sender_installation: isUuid, sequence: isPositive },
  ],
};

/** Apply decoding steps 2 to 4 to a parsed value. */
function validate(value: unknown): Message {
  if (!isObject(value) || typeof value.wire_version !== "number" || !Number.isInteger(value.wire_version)) {
    return malformed();
  }
  if (value.wire_version !== 1) {
    throw new ProtocolRefusal("unsupported-wire-protocol");
  }
  const kind = value.kind;
  if (typeof kind !== "string" || !Object.prototype.hasOwnProperty.call(KINDS, kind)) {
    return malformed();
  }
  const [required, optional] = KINDS[kind];
  if (!hasFields(value, required, optional)) {
    return malformed();
  }
  if (kind === "enrollment-confirmation" && value.fingerprint !== installationFingerprint(value.sender_installation as string)) {
    return malformed();
  }
  if (kind === "delivery-block" && "sender_installation" in value !== "sequence" in value) {
    return malformed();
  }
  return value;
}

function sameBytes(left: Uint8Array, right: Uint8Array): boolean {
  return left.length === right.length && left.every((byte, index) => byte === right[index]);
}

// Interface

/** Return the canonical bytes of a valid message value. */
export function encodeMessage(value: Message): Uint8Array {
  return utf8Encode(canonicalText(validate(value)));
}

/** Return the validated message value, or throw ProtocolRefusal. */
export function decodeMessage(bytes: Uint8Array): Message {
  const value = validate(parseJson(utf8Decode(bytes)));
  if (!sameBytes(utf8Encode(canonicalText(value)), bytes)) {
    throw new ProtocolRefusal("noncanonical-message");
  }
  return value;
}

/** Return the lowercase hexadecimal SHA-256 identity of exact message bytes. */
export function messageSha256(bytes: Uint8Array): string {
  return toHex(sha256(bytes));
}

/** Return the fingerprint an enrollment confirmation must carry. */
export function installationFingerprint(senderInstallation: string): string {
  return toHex(sha256(utf8Encode(senderInstallation)));
}

/** Return the starting fragment-chain value of a cycle. */
export function chainStart(cycleId: string): string {
  return toHex(sha256(utf8Encode(cycleId)));
}

/** Return the fragment-chain value after one fragment. */
export function chainNext(previousChain: string, messageSha256: string): string {
  const joined = new Uint8Array(64);
  joined.set(fromSha256Hex(previousChain));
  joined.set(fromSha256Hex(messageSha256), 32);
  return toHex(sha256(joined));
}

function fragmentValue(page: PageInput, sequence: number, fragmentIndex: number, previousChain: string, items: Message[]): Message {
  return {
    wire_version: 1,
    kind: "ingest-batch",
    sender_installation: page.sender_installation,
    sequence,
    capability_generation: page.capability_generation,
    record_type: page.record_type,
    projection_schema_version: page.projection_schema_version,
    cycle_kind: page.cycle_kind,
    cycle_id: page.cycle_id,
    source_page_index: page.source_page_index,
    fragment_index: fragmentIndex,
    previous_chain: previousChain,
    items,
  };
}

/** Partition one page into fragments, or throw ProtocolRefusal for an indivisible record. */
export function partitionPage(page: PageInput): Partition {
  const items = page.items;
  // An item's canonical bytes are the same inside or outside the items array,
  // so a fragment's size is its empty encoding plus its items and separators.
  const itemSizes = items.map((item) => utf8Encode(canonicalText(item)).length);
  const fragments: Uint8Array[] = [];
  let chain = page.previous_chain;
  let sequence = page.first_sequence;
  let next = 0;
  do {
    const fragmentIndex = fragments.length + 1;
    let size = encodeMessage(fragmentValue(page, sequence, fragmentIndex, chain, [])).length;
    let count = 0;
    while (next + count < items.length && count < page.max_record_count) {
      const grown = size + itemSizes[next + count] + (count === 0 ? 0 : 1);
      if (grown > page.max_message_bytes) {
        break;
      }
      size = grown;
      count++;
    }
    if (count === 0 && next < items.length) {
      throw new ProtocolRefusal("indivisible-record-limit-exceeded");
    }
    const bytes = encodeMessage(fragmentValue(page, sequence, fragmentIndex, chain, items.slice(next, next + count)));
    fragments.push(bytes);
    chain = chainNext(chain, messageSha256(bytes));
    sequence++;
    next += count;
  } while (next < items.length);
  return { fragments, next_chain: chain, next_sequence: sequence };
}

/** Interpret the Portal's response to one sent Outbox Message. */
export function interpretResponse(sent: SentMessage, response: Uint8Array): ResponseOutcome {
  const mismatch: ResponseOutcome = { outcome: "retryable", failure: "receipt-mismatch" };
  let message: Message;
  try {
    message = decodeMessage(response);
  } catch (error) {
    if (error instanceof ProtocolRefusal) {
      return mismatch;
    }
    throw error;
  }
  if (message.kind === "delivery-receipt") {
    const exact =
      message.sender_installation === sent.sender_installation &&
      message.sequence === sent.sequence &&
      message.message_sha256 === sent.message_sha256;
    return exact ? { outcome: "accepted" } : mismatch;
  }
  if (message.kind === "retryable-failure") {
    return { outcome: "retryable", failure: message.reason as string };
  }
  if (message.kind === "delivery-block") {
    const identified = !("sequence" in message) ||
      (message.sender_installation === sent.sender_installation && message.sequence === sent.sequence);
    if (message.message_sha256 === sent.message_sha256 && identified) {
      return { outcome: "block", failure: message.failure as string };
    }
  }
  return mismatch;
}
