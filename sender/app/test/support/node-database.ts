import { DatabaseSync } from "node:sqlite";
import { configuredDatabase } from "../../src/storage/connection.ts";
import { checkValue } from "../../src/storage/database.ts";
import type { DatabaseRow, DatabaseValue, SenderDatabase } from "../../src/storage/database.ts";

// Expo Android binds whole JavaScript numbers as int64, whereas Node defaults
// number parameters to REAL. Bind checked integers as BigInts to match Expo.
const bindings = (parameters: readonly DatabaseValue[]) =>
  parameters.map((value) => typeof value === "number" ? BigInt(value) : value);

/** Real file-backed SQLite; :memory: cannot provide the required DELETE journal and is rejected on open. */
export async function openNodeDatabase(
  path: string,
  configure?: (database: DatabaseSync) => void,
): Promise<SenderDatabase> {
  const database = new DatabaseSync(path);
  configure?.(database);
  return configuredDatabase({
    async execute(sql, parameters) { database.prepare(sql).run(...bindings(parameters)); },
    async query(sql, parameters) {
      const statement = database.prepare(sql);
      statement.setReadBigInts(true);
      return statement.all(...bindings(parameters)).map((row) => {
        const result: DatabaseRow = {};
        for (const [column, raw] of Object.entries(row)) {
          const value = typeof raw === "bigint" ? Number(raw) : raw;
          checkValue(value);
          result[column] = value;
        }
        return result;
      });
    },
    async close() { database.close(); },
  });
}
