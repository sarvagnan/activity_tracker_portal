import assert from "node:assert/strict";
import { test } from "node:test";
import { chainStart } from "@activity-tracker-portal/ingest-protocol";

// Durable modules live in src/ and use the same Node type-stripping seam.
test("the Sender imports the frozen protocol package through the workspace", () => {
  assert.equal(
    chainStart("synthetic-cycle"),
    "1a87f86d705a08d342d9409ad68d7ea1211bd85d17337999b538020af73d1eb3",
  );
});
