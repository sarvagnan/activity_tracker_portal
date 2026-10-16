import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import type { AndroidManifest, ExportedConfig } from "expo/config-plugins";
import appConfig from "../app.config.ts";

const require = createRequire(import.meta.url);
const { XML } = require("expo/config-plugins");
const withPortalNetworkSecurity: (config: ExportedConfig) => ExportedConfig =
  require("../plugins/with-portal-network-security.cjs");

function fixture(projectRoot: string) {
  const base = { name: "Synthetic Sender", slug: "synthetic-sender" };
  const config = withPortalNetworkSecurity(base);
  const request = { projectRoot, platformProjectRoot: join(projectRoot, "android"),
    platform: "android" as const, introspect: false };
  return {
    async manifest(manifest: AndroidManifest) {
      return (await config.mods!.android!.manifest!({
        ...config, modResults: manifest, modRawConfig: base, modRequest: { ...request, modName: "manifest" },
      })).modResults;
    },
    async resources(sourceSet = "main") {
      await config.mods!.android!.dangerous!({
        ...config, modResults: {}, modRawConfig: base, modRequest: { ...request, modName: "dangerous" },
      });
      return readFile(join(request.platformProjectRoot, "app/src", sourceSet, "res/xml/network_security_config.xml"), "utf8");
    },
  };
}

test("registered Portal network plugin allows cleartext only for ts.net and its subdomains", { timeout: 5_000 }, async (t) => {
  assert.ok(appConfig.expo.plugins.includes("./plugins/with-portal-network-security.cjs"));
  const directory = await mkdtemp(join(tmpdir(), "sender-network-plugin-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const f = fixture(directory);
  const xml = await f.resources();
  assert.deepEqual(await XML.parseXMLAsync(xml), {
    "network-security-config": {
      "base-config": [{ $: { cleartextTrafficPermitted: "false" } }],
      "domain-config": [{ $: { cleartextTrafficPermitted: "true" },
        domain: [{ _: "ts.net", $: { includeSubdomains: "true" } }],
      }],
    },
  });
  // Repeated prebuilds replace stale resource contents deterministically.
  await writeFile(join(directory, "android/app/src/main/res/xml/network_security_config.xml"), "stale", "utf8");
  assert.equal(await f.resources(), xml);
});

for (const sourceSet of ["debug", "debugOptimized"]) {
  test(`Portal network ${sourceSet} resource allows Metro HTTP and retains ts.net`, { timeout: 5_000 }, async (t) => {
    const directory = await mkdtemp(join(tmpdir(), "sender-network-plugin-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    const f = fixture(directory);
    const main = await f.resources();
    const xml = await f.resources(sourceSet);
    assert.deepEqual(await XML.parseXMLAsync(xml), {
      "network-security-config": {
        "base-config": [{ $: { cleartextTrafficPermitted: "true" } }],
        "domain-config": [{ $: { cleartextTrafficPermitted: "true" },
          domain: [{ _: "ts.net", $: { includeSubdomains: "true" } }],
        }],
      },
    });
    assert.equal(xml, main.replace(
      '<base-config cleartextTrafficPermitted="false" />',
      '<base-config cleartextTrafficPermitted="true" />',
    ));
    await writeFile(join(directory, "android/app/src", sourceSet, "res/xml/network_security_config.xml"), "stale", "utf8");
    assert.equal(await f.resources(sourceSet), xml);
    assert.equal(await f.resources(), main);
  });
}

test("Portal network manifest attribute is idempotent and preserves existing application settings", { timeout: 5_000 }, async () => {
  const f = fixture("/synthetic-sender");
  const original: AndroidManifest = { manifest: {
    $: { "xmlns:android": "http://schemas.android.com/apk/res/android" }, queries: [],
    application: [{ $: { "android:name": ".MainApplication", "android:allowBackup": "false" },
      activity: [{ $: { "android:name": ".MainActivity" } }],
    }],
  } };
  const once = await f.manifest(original);
  const snapshot = structuredClone(once);
  assert.deepEqual(await f.manifest(once), snapshot);
  assert.deepEqual(once.manifest.application![0], {
    $: { "android:name": ".MainApplication", "android:allowBackup": "false",
      "android:networkSecurityConfig": "@xml/network_security_config" },
    activity: [{ $: { "android:name": ".MainActivity" } }],
  });
  assert.equal(XML.format(once).match(/android:networkSecurityConfig=/g)?.length, 1);
  assert.equal(once.manifest.application![0].$["android:usesCleartextTraffic"], undefined);
});

test("Portal network plugin refuses a manifest without an application", { timeout: 5_000 }, async () => {
  await assert.rejects(fixture("/synthetic-sender").manifest({ manifest: {
    $: { "xmlns:android": "http://schemas.android.com/apk/res/android" }, queries: [],
  } }), /requires an Android application/);
});
