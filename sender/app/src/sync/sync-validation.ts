import type { StagingContext, ChangeItem } from "../outbox/stage-outbox-batch.ts";
import type { SenderSyncOptions, TriggerRequest, SnapshotRead, ChangeRead, SyncConditions } from "./coordinate-sender-sync.ts";

const RECORD_TYPE = /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PERMISSION = new Set(["read-permission-missing", "history-permission-missing", "read-denied"]);
const READ_FAILURE = new Set(["health-connect-unavailable", "health-connect-io-failure", "health-connect-request-rejected",
  "unexpected-read-failure", "source-contract-violation"]);
const PROJECTION_FAILURE = new Set(["unsupported-record-type", "non-finite-value", "undeclared-constant", "undeclared-variant",
  "unrepresentable-text", "unrepresentable-instant", "unrepresentable-offset"]);
const EXPIRED = new Set(["changes-token-expired", "changes-token-too-old", "clock-behind-changes-token"]);
const POSITION = new Set(["malformed-position", "foreign-position"]);
const PATH = /^\/(?:[A-Za-z_][A-Za-z0-9_-]*|0|[1-9][0-9]*)(?:\/(?:[A-Za-z_][A-Za-z0-9_-]*|0|[1-9][0-9]*))*$/;

function object(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === "object"; }
function positive(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value > 0; }
function recordType(value: unknown): value is string {
  return typeof value === "string" && value.length <= 64 && RECORD_TYPE.exec(value)?.[0] === value;
}
function methods(value: unknown, names: readonly string[]): boolean {
  return object(value) && names.every((name) => typeof value[name] === "function");
}
function bytes(value: unknown): value is Uint8Array { return value instanceof Uint8Array && value.length > 0; }

export function checkOptions(options: SenderSyncOptions): void {
  if (!object(options) || !methods(options.database, ["execute", "query", "transaction", "close"]) ||
      !methods(options.cipher, ["seal"]) || !methods(options.sources, ["issue", "readSnapshotPage", "readChangePage"]) ||
      !Array.isArray(options.recordTypes) || !object(options.projection) ||
      typeof options.projection.implementationVersion !== "string" || options.projection.implementationVersion.length === 0 ||
      !object(options.projection.schemaVersions) ||
      (options.concurrency !== undefined && !positive(options.concurrency)) ||
      (options.quietChangeReads !== undefined && typeof options.quietChangeReads !== "boolean") ||
      [options.context, options.conditions, options.backgroundScheduling, options.now, options.monotonicNow, options.randomUUID]
        .some((fn) => typeof fn !== "function")) throw new TypeError("Invalid Sender sync options");
  const seen = new Set<string>();
  for (const type of options.recordTypes) {
    if (!recordType(type) || seen.has(type) || !Object.prototype.hasOwnProperty.call(options.projection.schemaVersions, type) ||
        !positive(options.projection.schemaVersions[type])) throw new TypeError("Invalid managed record type");
    seen.add(type);
  }
}

export function checkTrigger(request: TriggerRequest, managed: ReadonlySet<string>): void {
  if (!object(request) || !["background", "foreground", "manual"].includes(request.kind) ||
      (request.budgetMs !== undefined && !positive(request.budgetMs)) ||
      (request.deadlineMs !== undefined && !Number.isSafeInteger(request.deadlineMs)) ||
      (request.priority !== undefined && (request.kind !== "manual" || !Array.isArray(request.priority) ||
        Array.from(request.priority).some((type) => !managed.has(type))))) throw new TypeError("Invalid sync trigger");
}

export function checkInstant(value: number): void {
  if (!Number.isSafeInteger(value) || value < -62_167_219_200_000 || value >= 253_402_300_800_000) {
    throw new TypeError("Invalid protocol instant");
  }
}

export function checkConditions(value: unknown): asserts value is SyncConditions {
  if (!object(value) || Array.isArray(value) ||
      ["collectionEnabled", "secureLock", "deliveryBlocked", "storagePressure"]
        .some((field) => typeof value[field] !== "boolean")) throw new TypeError("Invalid sync conditions");
}

export function checkContext(context: StagingContext, types: readonly string[], versions: Readonly<Record<string, number>>): void {
  if (!object(context) || typeof context.installationId !== "string" || UUID.exec(context.installationId)?.[0] !== context.installationId ||
      typeof context.keyAlias !== "string" || context.keyAlias.length === 0 || !object(context.generation)) {
    throw new TypeError("Invalid staging context");
  }
  const generation = context.generation;
  if (!positive(generation.generation) || !positive(generation.maxRecordCount) || !positive(generation.maxMessageBytes) ||
      generation.maxMessageBytes < 4096 || !object(generation.projectionSchemas) ||
      ![Object.prototype, null].includes(Object.getPrototypeOf(generation.projectionSchemas))) throw new TypeError("Invalid generation");
  for (const [type, schemas] of Object.entries(generation.projectionSchemas)) {
    if (!recordType(type) || !Array.isArray(schemas) || Array.from(schemas).some((schema) => !positive(schema))) throw new TypeError("Invalid schemas");
  }
  for (const type of types) {
    if (!Object.prototype.hasOwnProperty.call(generation.projectionSchemas, type) || !generation.projectionSchemas[type].includes(versions[type])) {
      throw new TypeError("Unsupported managed schema");
    }
  }
}

function sourceIdentity(value: unknown): value is string {
  if (typeof value !== "string") return false;
  let length = 0;
  for (const character of value) {
    const point = character.codePointAt(0)!;
    if (point < 0x20 || (point >= 0xd800 && point <= 0xdfff)) return false;
    length++;
  }
  return length > 0 && length <= 256;
}

/** Treat bridge payloads as untrusted and retain only contract fields. */
export function validateRead(value: unknown, source: "snapshot" | "change", schema: number): SnapshotRead | ChangeRead {
  const failed = { outcome: "read-failed", failure: "source-contract-violation" } as const;
  try {
    if (!object(value)) return failed;
    if (value.outcome === "page") {
      if (value.projectionSchemaVersion !== schema) return failed;
      if (source === "snapshot") {
        if (!Array.isArray(value.projections) || !Array.from(value.projections).every(bytes) || !(value.nextPosition instanceof Uint8Array) ||
            typeof value.exhausted !== "boolean") return failed;
        return { outcome: "page", projectionSchemaVersion: schema, projections: value.projections.map((item) => new Uint8Array(item)),
          nextPosition: new Uint8Array(value.nextPosition), exhausted: value.exhausted };
      }
      if (!Array.isArray(value.items) || typeof value.nextChangesToken !== "string" || value.nextChangesToken.length === 0 ||
          typeof value.nextUnreadSinceMs !== "number" || !Number.isSafeInteger(value.nextUnreadSinceMs) || typeof value.hasMore !== "boolean") return failed;
      const items: ChangeItem[] = [];
      for (const item of value.items) {
        if (!object(item)) return failed;
        if (item.kind === "projection" && bytes(item.bytes)) items.push({ kind: "projection", bytes: new Uint8Array(item.bytes) });
        else if (item.kind === "retraction" && sourceIdentity(item.sourceIdentity)) items.push({ kind: "retraction", sourceIdentity: item.sourceIdentity });
        else return failed;
      }
      return { outcome: "page", projectionSchemaVersion: schema, items, nextChangesToken: value.nextChangesToken,
        nextUnreadSinceMs: value.nextUnreadSinceMs, hasMore: value.hasMore };
    }
    if (typeof value.failure !== "string") return failed;
    if (value.outcome === "projection-refused" && PROJECTION_FAILURE.has(value.failure) && typeof value.path === "string" && (value.path === "" || PATH.exec(value.path)?.[0] === value.path)) {
      return { outcome: "projection-refused", failure: value.failure, path: value.path };
    }
    if (value.outcome === "not-permitted" && PERMISSION.has(value.failure)) return { outcome: "not-permitted", failure: value.failure };
    if (value.outcome === "read-failed" && (READ_FAILURE.has(value.failure) ||
        (source === "snapshot" && ["source-order-violation", "boundary-group-too-large"].includes(value.failure)))) {
      return { outcome: "read-failed", failure: value.failure };
    }
    if (source === "snapshot" && value.outcome === "invalid-position" && POSITION.has(value.failure)) {
      return { outcome: "invalid-position", failure: value.failure };
    }
    if (source === "change" && value.outcome === "token-expired" && EXPIRED.has(value.failure)) {
      return { outcome: "token-expired", failure: value.failure };
    }
    return failed;
  } catch { return failed; }
}

/** UTF-8 for the position digest, without depending on a host encoding API. */
export function utf8(text: string): Uint8Array {
  const result: number[] = [];
  for (const character of text) {
    let point = character.codePointAt(0)!;
    if (point >= 0xd800 && point <= 0xdfff) point = 0xfffd;
    if (point < 0x80) result.push(point);
    else if (point < 0x800) result.push(0xc0 | (point >> 6), 0x80 | (point & 0x3f));
    else if (point < 0x10000) result.push(0xe0 | (point >> 12), 0x80 | ((point >> 6) & 0x3f), 0x80 | (point & 0x3f));
    else result.push(0xf0 | (point >> 18), 0x80 | ((point >> 12) & 0x3f), 0x80 | ((point >> 6) & 0x3f), 0x80 | (point & 0x3f));
  }
  return new Uint8Array(result);
}
