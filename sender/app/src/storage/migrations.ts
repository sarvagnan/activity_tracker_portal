import { enrollmentMigration } from "../enrollment/manage-sender-enrollment.ts";
import { deliveryMigration } from "../delivery/send-outbox-message.ts";
import { operatorConditionsMigration } from "../notifications/notify-operator-conditions.ts";
import { checkpointMigration } from "../outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../outbox/stage-sync-completion.ts";
import { outboxMigration } from "../outbox/stage-outbox-batch.ts";
import { schedulingMigration } from "../scheduling/schedule-background-polling.ts";
import { snapshotAttemptsMigration } from "../snapshot/begin-initial-snapshot.ts";
import { syncMigration } from "../sync/coordinate-sender-sync.ts";
import type { SenderDatabase } from "./database.ts";

export interface Migration {
  readonly version: number;
  readonly apply: (transaction: SenderDatabase) => void | Promise<void>;
}
export type MigrationResult =
  | { status: "ready"; version: number }
  | { status: "frozen"; version: number; reason: "migration-failed" | "newer-schema" };

/** Governed units append migrations: snapshot attempts, then the Sender Outbox and cycle progress, then cycle completions, then run checkpoints, then synchronization runs, ring, cycles, and blocks, then background scheduling state, then outbox head blocks, then enrollment state, then Operator conditions. */
export const senderMigrations: readonly Migration[] = Object.freeze([snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration, schedulingMigration, deliveryMigration, enrollmentMigration, operatorConditionsMigration]);

export async function migrate(
  database: SenderDatabase,
  migrations: readonly Migration[] = senderMigrations,
): Promise<MigrationResult> {
  Array.from(migrations).forEach((migration, index) => {
    if (!migration || migration.version !== index + 1 || typeof migration.apply !== "function") {
      throw new TypeError("Migrations must be contiguous and ordered from version 1");
    }
  });
  const [row] = await database.query("PRAGMA user_version");
  let version = row?.user_version;
  if (typeof version !== "number" || !Number.isSafeInteger(version) || version < 0) {
    throw new Error("Invalid SQLite schema version");
  }
  if (version > migrations.length) return { status: "frozen", version, reason: "newer-schema" };
  for (const migration of migrations.slice(version)) {
    try {
      await database.transaction(async (transaction) => {
        await migration.apply(transaction);
        // Validated versions are internal integers, not caller-provided SQL.
        await transaction.execute(`PRAGMA user_version=${migration.version}`);
      });
      version = migration.version;
    } catch {
      return { status: "frozen", version, reason: "migration-failed" };
    }
  }
  return { status: "ready", version };
}
