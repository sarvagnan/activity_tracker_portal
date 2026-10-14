// Synthetic reader and issuer seams for coordinate-sender-sync@1. Records, positions, and tokens are
// opaque synthetic strings; nothing here models Health Connect beyond what the coordinator relies on:
// pages from a saved position, a change log behind tokens, and scripted or held results.
import type { ChangeItem } from "../../src/outbox/stage-outbox-batch.ts";
import type { IssuedToken } from "../../src/snapshot/begin-initial-snapshot.ts";
import type { ChangeRead, ChangeReadRequest, SnapshotRead, SnapshotReadRequest, SyncSources } from "../../src/sync/coordinate-sender-sync.ts";

export type SourceMethod = "issue" | "snapshot" | "change";

export interface SourceCall {
  readonly method: SourceMethod;
  readonly recordType: string;
  readonly request: SnapshotReadRequest | ChangeReadRequest | null;
}

type Scripted = unknown | ((request: SnapshotReadRequest | ChangeReadRequest | null) => unknown);

interface Gate {
  readonly method: SourceMethod;
  readonly recordType: string | undefined;
  readonly arrived: Promise<SourceCall>;
  readonly arrive: (call: SourceCall) => void;
  readonly released: Promise<void>;
}

const encoder = new TextEncoder();
const decoder = new TextDecoder();
export const utf8 = (text: string) => encoder.encode(text);

/** Wall and monotonic clocks the test moves; source calls may consume monotonic time. */
export class SyntheticClock {
  wall: number;
  mono: number;
  constructor(wall: number, mono = 1_000) { this.wall = wall; this.mono = mono; }
  readonly now = () => this.wall;
  readonly monotonicNow = () => this.mono;
  advance(ms: number): void { this.wall += ms; this.mono += ms; }
}

export class SyntheticSources implements SyncSources {
  pageSize = 2;
  schemaVersion = 1;
  /** Monotonic and wall time each source call consumes before it answers. */
  callCostMs = 0;
  readonly calls: SourceCall[] = [];
  inFlight = 0;
  maxInFlight = 0;
  maxInFlightPerType = 0;
  /** Called synchronously as each source call starts, before it yields. */
  onCall: ((call: SourceCall) => void | Promise<void>) | null = null;
  private readonly clock: SyntheticClock;
  private readonly records = new Map<string, Map<string, number>>();
  private readonly logs = new Map<string, { kind: "upsert" | "delete"; id: string; version: number }[]>();
  private readonly epochs = new Map<string, number>();
  private readonly scripts: { method: SourceMethod; recordType: string | undefined; result: Scripted; skip: number }[] = [];
  private readonly gates: Gate[] = [];
  private readonly perType = new Map<string, number>();

  constructor(clock: SyntheticClock) { this.clock = clock; }

  /** Insert or update synthetic records, logging each change. */
  put(recordType: string, ...ids: string[]): void {
    const records = this.table(recordType);
    for (const id of ids) {
      const version = (records.get(id) ?? 0) + 1;
      records.set(id, version);
      this.log(recordType).push({ kind: "upsert", id, version });
    }
  }

  /** Insert `count` new records named `<prefix>-0001` and onward. */
  seed(recordType: string, count: number, prefix = "rec"): string[] {
    const start = this.table(recordType).size;
    const ids = Array.from({ length: count }, (_, i) => `${prefix}-${String(start + i + 1).padStart(4, "0")}`);
    this.put(recordType, ...ids);
    return ids;
  }

  remove(recordType: string, ...ids: string[]): void {
    for (const id of ids) {
      this.table(recordType).delete(id);
      this.log(recordType).push({ kind: "delete", id, version: 0 });
    }
  }

  /** Make every token issued so far for the type read as expired; later tokens are valid. */
  expire(recordType: string): void {
    this.epochs.set(recordType, (this.epochs.get(recordType) ?? 0) + 1);
  }

  /** The token for a position in the type's change log under its current epoch. */
  token(recordType: string, index: number): string {
    return `synthetic-token:${recordType}:${this.epochs.get(recordType) ?? 0}:${index}`;
  }

  /** After `skip` matching calls, the next one answers `result`, or what `result(request)` returns or throws. */
  next(method: SourceMethod, recordType: string | undefined, result: Scripted, skip = 0): void {
    this.scripts.push({ method, recordType, result, skip });
  }

  /** The next matching call waits until released; it is answered from the data as it then stands. */
  hold(method: SourceMethod, recordType?: string): { arrived: Promise<SourceCall>; release: () => void } {
    let arrive!: (call: SourceCall) => void;
    let release!: () => void;
    const arrived = new Promise<SourceCall>((resolve) => { arrive = resolve; });
    const released = new Promise<void>((resolve) => { release = resolve; });
    this.gates.push({ method, recordType, arrived, arrive, released });
    return { arrived, release };
  }

  callsOf(method: SourceMethod, recordType?: string): SourceCall[] {
    return this.calls.filter((call) => call.method === method && (recordType === undefined || call.recordType === recordType));
  }

  projection(recordType: string, id: string, version: number): Uint8Array {
    return utf8(`synthetic:${recordType}:${id}:v${version}`);
  }

  async issue(recordType: string): Promise<IssuedToken> {
    return this.answer({ method: "issue", recordType, request: null }, () => ({
      outcome: "issued", changesToken: this.token(recordType, this.log(recordType).length), unreadSinceMs: this.clock.wall,
    })) as Promise<IssuedToken>;
  }

  async readSnapshotPage(request: SnapshotReadRequest): Promise<SnapshotRead> {
    const copy = { ...request, position: request.position === null ? null : new Uint8Array(request.position) };
    return this.answer({ method: "snapshot", recordType: request.recordType, request: copy }, () => {
      let offset = 0;
      if (copy.position !== null) {
        const match = /^synthetic-position:([0-9a-f-]{36}):(0|[1-9][0-9]*)$/.exec(decoder.decode(copy.position));
        if (!match) return { outcome: "invalid-position", failure: "malformed-position" };
        if (match[1] !== copy.snapshotId) return { outcome: "invalid-position", failure: "foreign-position" };
        offset = Number(match[2]);
      }
      const entries = [...this.table(copy.recordType)].slice(offset, offset + this.pageSize);
      const next = offset + entries.length;
      return {
        outcome: "page", projectionSchemaVersion: this.schemaVersion,
        projections: entries.map(([id, version]) => this.projection(copy.recordType, id, version)),
        nextPosition: utf8(`synthetic-position:${copy.snapshotId}:${next}`), exhausted: next >= this.table(copy.recordType).size,
      };
    }) as Promise<SnapshotRead>;
  }

  async readChangePage(request: ChangeReadRequest): Promise<ChangeRead> {
    const copy = { ...request };
    return this.answer({ method: "change", recordType: request.recordType, request: copy }, () => {
      const match = /^synthetic-token:([a-z0-9-]+):(0|[1-9][0-9]*):(0|[1-9][0-9]*)$/.exec(copy.changesToken);
      if (!match || match[1] !== copy.recordType || Number(match[2]) !== (this.epochs.get(copy.recordType) ?? 0)) {
        return { outcome: "token-expired", failure: "changes-token-expired" };
      }
      const log = this.log(copy.recordType);
      const index = Number(match[3]);
      const entries = log.slice(index, index + this.pageSize);
      const next = index + entries.length;
      const items: ChangeItem[] = entries.map((entry) => entry.kind === "delete"
        ? { kind: "retraction", sourceIdentity: entry.id }
        : { kind: "projection", bytes: this.projection(copy.recordType, entry.id, entry.version) });
      const hasMore = next < log.length;
      return {
        outcome: "page", projectionSchemaVersion: this.schemaVersion, items,
        nextChangesToken: this.token(copy.recordType, next), nextUnreadSinceMs: hasMore ? copy.unreadSinceMs : this.clock.wall, hasMore,
      };
    }) as Promise<ChangeRead>;
  }

  private async answer(call: SourceCall, compute: () => unknown): Promise<unknown> {
    this.calls.push(call);
    this.inFlight++;
    this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);
    const typeCount = (this.perType.get(call.recordType) ?? 0) + 1;
    this.perType.set(call.recordType, typeCount);
    this.maxInFlightPerType = Math.max(this.maxInFlightPerType, typeCount);
    try {
      await this.onCall?.(call);
      const gateIndex = this.gates.findIndex((gate) => gate.method === call.method && (gate.recordType === undefined || gate.recordType === call.recordType));
      if (gateIndex >= 0) {
        const [gate] = this.gates.splice(gateIndex, 1);
        gate.arrive(call);
        await gate.released;
      }
      // Yield a macrotask so concurrent turns genuinely overlap.
      await new Promise<void>((resolve) => setImmediate(resolve));
      this.clock.advance(this.callCostMs);
      const scriptIndex = this.scripts.findIndex((script) => script.method === call.method && (script.recordType === undefined || script.recordType === call.recordType));
      if (scriptIndex >= 0 && this.scripts[scriptIndex].skip > 0) {
        this.scripts[scriptIndex].skip--;
      } else if (scriptIndex >= 0) {
        const [script] = this.scripts.splice(scriptIndex, 1);
        return typeof script.result === "function" ? await (script.result as (request: unknown) => unknown)(call.request) : script.result;
      }
      return compute();
    } finally {
      this.inFlight--;
      this.perType.set(call.recordType, (this.perType.get(call.recordType) ?? 1) - 1);
    }
  }

  private table(recordType: string): Map<string, number> {
    let table = this.records.get(recordType);
    if (!table) this.records.set(recordType, table = new Map());
    return table;
  }

  private log(recordType: string) {
    let log = this.logs.get(recordType);
    if (!log) this.logs.set(recordType, log = []);
    return log;
  }
}
