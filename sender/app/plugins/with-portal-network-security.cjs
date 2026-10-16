const { withAndroidManifest, withDangerousMod } = require("expo/config-plugins");
const { mkdir, writeFile } = require("node:fs/promises");
const path = require("node:path");

// Operator decision 2026-10-16: HTTP is permitted only for Tailscale
// MagicDNS names. Android's domain matching includes ts.net itself.
const networkSecurityConfig = `<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="false" />
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="true">ts.net</domain>
    </domain-config>
</network-security-config>
`;

// Development variants must reach Metro over HTTP; release keeps the
// Operator's policy through the strict main resource above.
const developmentNetworkSecurityConfig = networkSecurityConfig.replace(
  '<base-config cleartextTrafficPermitted="false" />',
  '<base-config cleartextTrafficPermitted="true" />',
);

module.exports = function withPortalNetworkSecurity(config) {
  config = withAndroidManifest(config, (config) => {
    const application = config.modResults.manifest.application?.[0];
    if (!application) throw new Error("Portal network security requires an Android application");
    application.$["android:networkSecurityConfig"] = "@xml/network_security_config";
    return config;
  });
  return withDangerousMod(config, ["android", async (config) => {
    for (const sourceSet of ["main", "debug", "debugOptimized"]) {
      const directory = path.join(config.modRequest.platformProjectRoot, "app/src", sourceSet, "res/xml");
      await mkdir(directory, { recursive: true });
      await writeFile(path.join(directory, "network_security_config.xml"),
        sourceSet === "main" ? networkSecurityConfig : developmentNetworkSecurityConfig, "utf8");
    }
    return config;
  }]);
};
