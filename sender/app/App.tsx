import { Button, ScrollView, StyleSheet, Switch, Text, TextInput, View } from "react-native";
import { useSenderScreen } from "./src/screens/use-sender-screen.ts";

export default function App() {
  const screen = useSenderScreen();
  const { model, busy } = screen;
  return (
    <ScrollView contentContainerStyle={styles.container} keyboardShouldPersistTaps="handled">
      <Text style={styles.title}>Activity Tracker Sender</Text>
      <Text accessibilityRole="header" style={styles.heading}>{model.title}</Text>
      {screen.notice && <Text accessibilityRole="alert">{screen.notice}</Text>}
      {model.showOffer && <View style={styles.section}>
        <Text>Enter the Portal URL and enrollment code.</Text>
        <TextInput accessibilityLabel="Portal URL" placeholder="https://portal.example" value={screen.portalUrl}
          onChangeText={screen.setPortalUrl} autoCapitalize="none" autoCorrect={false} keyboardType="url"
          editable={!busy} style={styles.input} />
        <TextInput accessibilityLabel="Enrollment code" placeholder="Enrollment code" value={screen.enrollmentCode}
          onChangeText={screen.setEnrollmentCode} autoCapitalize="none" autoCorrect={false} secureTextEntry
          editable={!busy} style={styles.input} />
        <Button title="Enroll" disabled={busy} onPress={screen.enroll} />
      </View>}
      {model.fingerprint && <View style={styles.section}>
        <Text>Compare this fingerprint with the Portal:</Text>
        <Text selectable style={styles.fingerprint}>{model.fingerprint}</Text>
      </View>}
      {model.lifecycleText && <Text>{model.lifecycleText}</Text>}
      {model.refusal && <Text accessibilityRole="alert">{model.refusal}</Text>}
      {model.actions.includes("confirm") && <Button title="Retry confirmation" disabled={busy} onPress={screen.confirm} />}
      {model.actions.includes("beginDrain") && <Button title="Begin draining" disabled={busy} onPress={screen.beginDrain} />}
      {model.finalSequence && <Text selectable>{model.finalSequence}</Text>}
      {model.showPortalRetired && <View style={styles.section}>
        <Text>I have retired this installation in the Portal.</Text>
        <Switch accessibilityLabel="Installation retired in Portal" value={screen.portalRetired}
          disabled={busy} onValueChange={screen.setPortalRetired} />
        <Button title="Unlink" disabled={busy || !model.actions.includes("unlink")} onPress={screen.unlink} />
      </View>}
      <View style={styles.section}>
        <Text accessibilityRole="header" style={styles.heading}>Health Connect access</Text>
        <Text>{model.permissionText}</Text>
        <Button title="Request read, history and background access" disabled={busy || !model.canRequestPermissions}
          onPress={screen.requestPermissions} />
      </View>
      <View style={styles.section}>
        <Text accessibilityRole="header" style={styles.heading}>Sender status</Text>
        {model.conditionTexts.length === 0 && <Text>No active Operator conditions.</Text>}
        {model.conditionTexts.map((text) => <Text key={text}>{text}</Text>)}
        <Text>{model.syncText}</Text>
        <Text>{model.failureText}</Text>
        {model.failureTime && <Text>{model.failureTime}</Text>}
        <Button title="Sync now" disabled={busy} onPress={screen.syncNow} />
        <Button title="Refresh status" disabled={busy} onPress={screen.refresh} />
      </View>
      {model.showReset && <View style={styles.section}>
        <Text accessibilityRole="header" style={styles.heading}>Destructive reset</Text>
        <Text>Reset destroys this installation and its queued messages. I acknowledge possible loss of unsent data.</Text>
        <Switch accessibilityLabel="I acknowledge possible loss of unsent data" value={screen.acknowledgedLoss}
          disabled={busy} onValueChange={screen.setAcknowledgedLoss} />
        <Button title="Reset destructively" color="#a32121" disabled={busy || !model.actions.includes("reset")}
          onPress={screen.reset} />
      </View>}
    </ScrollView>
  );
}
const styles = StyleSheet.create({
  container: { padding: 24, paddingTop: 56, gap: 16 },
  title: { fontSize: 24, fontWeight: "bold" },
  heading: { fontSize: 20, fontWeight: "600" },
  section: { gap: 12, paddingVertical: 12 },
  input: { borderWidth: 1, borderColor: "#777", borderRadius: 4, padding: 12, color: "#111", backgroundColor: "#fff" },
  fingerprint: { fontSize: 18, fontWeight: "600" },
});
