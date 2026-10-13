/** SQLite values stay exact through the protocol's Number.MAX_SAFE_INTEGER bound. */
export type DatabaseValue = null | string | number | Uint8Array;
export type DatabaseRow = Record<string, DatabaseValue>;

export interface SenderDatabase {
  execute(sql: string, parameters?: readonly DatabaseValue[]): Promise<void>;
  query(sql: string, parameters?: readonly DatabaseValue[]): Promise<DatabaseRow[]>;
  /** Use the callback handle, await its operations, and never nest transactions. */
  transaction<T>(fn: (transaction: SenderDatabase) => T | Promise<T>): Promise<T>;
  close(): Promise<void>;
}

export function checkValue(value: unknown): asserts value is DatabaseValue {
  if (value === null || typeof value === "string" || value instanceof Uint8Array) return;
  if (typeof value === "number" && Number.isSafeInteger(value)) return;
  throw new TypeError("SQLite values must be null, text, safe integers, or bytes");
}
