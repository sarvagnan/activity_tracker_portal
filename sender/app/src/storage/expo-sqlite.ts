// Production-only boundary. Plain Node suites import database.ts instead.
import { openDatabaseAsync } from "expo-sqlite";
import { configuredDatabase } from "./connection.ts";
import type { DatabaseRow, SenderDatabase } from "./database.ts";

export async function openSenderDatabase(name: string): Promise<SenderDatabase> {
  // A dedicated connection prevents Expo's shared connection cache from joining
  // an unrelated caller to our explicitly owned transaction.
  const database = await openDatabaseAsync(name, { useNewConnection: true });
  return configuredDatabase({
    async execute(sql, parameters) { await database.runAsync(sql, [...parameters]); },
    async query(sql, parameters) { return database.getAllAsync<DatabaseRow>(sql, [...parameters]); },
    async close() { await database.closeAsync(); },
  });
}
