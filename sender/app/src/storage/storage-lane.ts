import type { SenderDatabase } from "./database.ts";

type Lane = <T>(operation: () => Promise<T>) => Promise<T>;
const lanes = new WeakMap<SenderDatabase, Lane>();

/**
 * Every Sender unit sharing a SenderDatabase must serialize storage through this
 * lane because the 3ug storage seam allows one operation at a time. Only work
 * outside storage, such as source reads or notifications, may overlap.
 */
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
