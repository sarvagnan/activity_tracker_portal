// Production-only Expo boundary; test packs depend on InstallationCipher.
import { requireNativeModule } from "expo-modules-core";
import type { InstallationCipher } from "./installation-cipher.ts";

export function nativeInstallationCipher(): InstallationCipher {
  return requireNativeModule<InstallationCipher>("ActivityTrackerInstallationKey");
}
