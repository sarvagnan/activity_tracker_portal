import type { SenderDatabase } from "../storage/database.ts";

type Lane = <T>(operation: () => Promise<T>) => Promise<T>;
const lanes = new WeakMap<SenderDatabase, Lane>();

/** Shared by this coordinator's public reads, retries, and concurrent turns. */
export function storageLane(database: SenderDatabase): Lane {
  let lane = lanes.get(database);
  if (!lane) {
    let tail: Promise<unknown> = Promise.resolve();
    lane = <T>(operation: () => Promise<T>): Promise<T> => {
      const result = tail.then(operation);
      tail = result.then(() => undefined, () => undefined);
      return result;
    };
    lanes.set(database, lane);
  }
  return lane;
}

/** Keep database TypeErrors distinct from caller validation errors. */
export function storageDatabase(database: SenderDatabase): SenderDatabase {
  async function operation<T>(call: () => Promise<T>): Promise<T> {
    try { return await call(); }
    catch (error) {
      if (error instanceof TypeError) throw new Error("Sender storage failed");
      throw error;
    }
  }
  return {
    execute: (sql, parameters) => operation(() => database.execute(sql, parameters)),
    query: (sql, parameters) => operation(() => database.query(sql, parameters)),
    async transaction(fn) {
      let callbackFailed = false;
      let callbackError: unknown;
      try {
        return await database.transaction(async (tx) => {
          try { return await fn(storageDatabase(tx)); }
          catch (error) { callbackFailed = true; callbackError = error; throw error; }
        });
      } catch (error) {
        // A caller error inside the transaction is not a database failure.
        if (callbackFailed && error === callbackError) throw error;
        return operation(() => Promise.reject(error));
      }
    },
    close: () => operation(() => database.close()),
  };
}

/** Serialize each top-level begin operation, without locking its source wait. */
export function serializedDatabase(database: SenderDatabase): SenderDatabase {
  const lane = storageLane(database);
  database = storageDatabase(database);
  return {
    execute: (sql, parameters) => lane(() => database.execute(sql, parameters)),
    query: (sql, parameters) => lane(() => database.query(sql, parameters)),
    transaction: (fn) => lane(() => database.transaction(fn)),
    close: () => lane(() => database.close()),
  };
}
