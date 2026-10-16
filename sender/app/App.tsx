import { useEffect, useState } from "react";
import { AppState, Button, StyleSheet, Text, View } from "react-native";
import { expoSender } from "./src/shell/expo-sender.ts";
import { startForegroundLifecycle } from "./src/shell/foreground-lifecycle.ts";
import { requestSenderNotificationPermission } from "./src/background/react-native-notification-permission.ts";
import type { ShellRunResult } from "./src/shell/compose-sender.ts";

function resultMessage(result: ShellRunResult): string {
  return result.outcome === "checkpointed" ? "Sync finished."
    : result.outcome === "skipped" ? "Sync waits for a confirmed enrollment."
    : "Sync could not finish.";
}

export default function App() {
  const [message, setMessage] = useState("Sender ready.");
  const [syncing, setSyncing] = useState(false);
  useEffect(() => {
    let mounted = true;
    let permissionRequested = false;
    const stop = startForegroundLifecycle(AppState, async () => {
      // Permission prompts belong to the foreground shell, never to TaskManager.
      if (!permissionRequested) {
        permissionRequested = true;
        try { await requestSenderNotificationPermission(); } catch { /* Scheduling retries notifications later. */ }
      }
      const result = await (await expoSender()).foreground();
      if (mounted) setMessage(resultMessage(result));
    }, () => { if (mounted) setMessage("Sender could not start sync."); });
    return () => { mounted = false; stop(); };
  }, []);

  async function syncNow() {
    setSyncing(true);
    try { setMessage(resultMessage(await (await expoSender()).manualSync())); }
    catch { setMessage("Sync could not finish."); }
    finally { setSyncing(false); }
  }

  return (
    <View style={styles.container}>
      <Text style={styles.title}>Activity Tracker Sender</Text>
      <Text>{message}</Text>
      <Button title="Sync now" disabled={syncing} onPress={() => { void syncNow(); }} />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24, gap: 12 },
  title: { fontSize: 22, marginBottom: 12 },
});
