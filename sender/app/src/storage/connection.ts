import { checkValue } from "./database.ts";
import type { DatabaseRow, DatabaseValue, SenderDatabase } from "./database.ts";

/** Driver stays private to the adapter; no raw connection escapes to callers. */
export interface DatabaseDriver {
  execute(sql: string, parameters: readonly DatabaseValue[]): Promise<void>;
  query(sql: string, parameters: readonly DatabaseValue[]): Promise<DatabaseRow[]>;
  close(): Promise<void>;
}

export async function configuredDatabase(driver: DatabaseDriver): Promise<SenderDatabase> {
  try {
    await driver.execute("PRAGMA journal_mode=DELETE", []);
    await driver.execute("PRAGMA synchronous=FULL", []);
    await driver.execute("PRAGMA foreign_keys=ON", []);
    const [journal] = await driver.query("PRAGMA journal_mode", []);
    const [synchronous] = await driver.query("PRAGMA synchronous", []);
    const [foreignKeys] = await driver.query("PRAGMA foreign_keys", []);
    if (journal?.journal_mode !== "delete" || synchronous?.synchronous !== 2 || foreignKeys?.foreign_keys !== 1) {
      throw new Error("Required SQLite settings unavailable");
    }
  } catch (error) {
    await driver.close();
    throw error;
  }

  let busy = false;
  let closed = false;
  let poisoned = false;
  function acquire(allowPoisoned = false) {
    if (closed) throw new Error("Database is closed");
    if (poisoned && !allowPoisoned) {
      throw new Error("Database connection is unusable after a failed rollback; close it");
    }
    if (busy) throw new Error("Database operation already active; transactions cannot nest");
    busy = true;
  }
  async function execute(sql: string, parameters: readonly DatabaseValue[] = []) {
    parameters.forEach(checkValue);
    await driver.execute(sql, parameters);
  }
  async function query(sql: string, parameters: readonly DatabaseValue[] = []) {
    parameters.forEach(checkValue);
    const rows = await driver.query(sql, parameters);
    for (const row of rows) Object.values(row).forEach(checkValue);
    return rows;
  }
  return {
    async execute(sql, parameters) {
      acquire();
      try { await execute(sql, parameters); } finally { busy = false; }
    },
    async query(sql, parameters) {
      acquire();
      try { return await query(sql, parameters); } finally { busy = false; }
    },
    async transaction(fn) {
      acquire();
      let live = true;
      const requireLive = () => { if (!live) throw new Error("Transaction has ended"); };
      const transaction: SenderDatabase = {
        async execute(sql, parameters) { requireLive(); await execute(sql, parameters); },
        async query(sql, parameters) { requireLive(); return await query(sql, parameters); },
        async transaction() { throw new Error("Nested transactions are a programming error"); },
        async close() { throw new Error("Cannot close a transaction handle"); },
      };
      try {
        await execute("BEGIN IMMEDIATE");
        try {
          const value = await fn(transaction);
          live = false;
          await execute("COMMIT");
          return value;
        } catch (error) {
          live = false;
          try { await execute("ROLLBACK"); } catch {
            // The driver may still be inside the abandoned transaction. Only close
            // is safe; preserve the original callback/commit error for the caller.
            poisoned = true;
          }
          throw error;
        }
      } finally { live = false; busy = false; }
    },
    async close() {
      acquire(true);
      try { await driver.close(); closed = true; } finally { busy = false; }
    },
  };
}
