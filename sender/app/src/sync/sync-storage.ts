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

/** Serialize each top-level begin operation, without locking its source wait. */
export function serializedDatabase(database: SenderDatabase): SenderDatabase {
  const lane = storageLane(database);
  return {
    execute: (sql, parameters) => lane(() => database.execute(sql, parameters)),
    query: (sql, parameters) => lane(() => database.query(sql, parameters)),
    transaction: (fn) => lane(() => database.transaction(fn)),
    close: () => lane(() => database.close()),
  };
}
