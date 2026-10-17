import { useEffect, useRef, useState } from "react";
import { AppState } from "react-native";
import { expoSender } from "../shell/expo-sender.ts";
import { startForegroundLifecycle } from "../shell/foreground-lifecycle.ts";
import { requestSenderNotificationPermission } from "../background/react-native-notification-permission.ts";
import { nativeHealthPermissions, SENDER_HEALTH_PERMISSIONS } from "../native-health-permissions.ts";
import type { SenderShell, ShellRunResult } from "../shell/compose-sender.ts";
import type { ShellFailure } from "../shell/failure-observation.ts";
import { permissionState, resetAcknowledgement, senderScreenModel, typedOffer } from "./sender-screen-model.ts";
import type { ConditionsState, EnrollmentState, PermissionState } from "./sender-screen-model.ts";

/** Foreground controller: screens only render the model and forward gestures. */
export function useSenderScreen() {
  const [portalUrl, setPortalUrl] = useState("");
  const [enrollmentCode, setEnrollmentCode] = useState("");
  const [acknowledgedLoss, setAcknowledgedLoss] = useState(false);
  const [portalRetired, setPortalRetired] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<{
    status: EnrollmentState | undefined; conditions: ConditionsState; failure: ShellFailure | null; sync: ShellRunResult | null;
  }>({ status: undefined, conditions: [], failure: null, sync: null });
  const [permission, setPermission] = useState<PermissionState>("unknown");
  const mounted = useRef(true);
  const identity = useRef<string | null | undefined>(undefined);

  async function refresh(shell: SenderShell) {
    const status = await shell.status();
    const conditions = await shell.conditions();
    if (!mounted.current) return;
    const nextIdentity = status && !("outcome" in status) ? status.installationId : null;
    if (nextIdentity !== identity.current) {
      identity.current = nextIdentity;
      setAcknowledgedLoss(false);
      setPortalRetired(false);
    }
    setSnapshot({ status, conditions, failure: shell.latestFailure(), sync: shell.latestSyncResult() });
  }
  async function checkPermissions(request = false) {
    try {
      const native = nativeHealthPermissions();
      const sdk = await native.sdkStatus();
      const granted = sdk !== 3 ? [] : request
        ? await native.requestPermissions(SENDER_HEALTH_PERMISSIONS) : await native.grantedPermissions();
      if (mounted.current) setPermission(permissionState(sdk, granted, SENDER_HEALTH_PERMISSIONS));
    } catch { if (mounted.current) setPermission("failed"); }
  }
  useEffect(() => {
    mounted.current = true;
    let permissionRequested = false;
    const stop = startForegroundLifecycle(AppState, async () => {
      if (!permissionRequested) {
        permissionRequested = true;
        try { await requestSenderNotificationPermission(); } catch { /* Existing scheduling retries later. */ }
      }
      const shell = await expoSender();
      try { await shell.foreground(); }
      finally { await refresh(shell); await checkPermissions(); }
    }, () => { if (mounted.current) setNotice("Sender could not start sync."); });
    return () => { mounted.current = false; stop(); };
  }, []);

  async function run(action: (shell: SenderShell) => Promise<unknown>) {
    setBusy(true);
    setNotice(null);
    try {
      const shell = await expoSender();
      try { await action(shell); }
      finally { await refresh(shell); }
    } catch { if (mounted.current) setNotice("The Sender operation could not finish. Try again."); }
    finally { if (mounted.current) setBusy(false); }
  }
  const model = senderScreenModel({ ...snapshot, permission, acknowledgedLoss, portalRetired });
  return {
    model, busy, notice, portalUrl, enrollmentCode, acknowledgedLoss, portalRetired,
    setPortalUrl, setEnrollmentCode, setAcknowledgedLoss, setPortalRetired,
    enroll() {
      const validation = typedOffer(portalUrl, enrollmentCode);
      if (!validation.offer) { setNotice(validation.error); return; }
      void run(async (shell) => {
        try { return await shell.enroll(validation.offer); }
        finally { if (mounted.current) setEnrollmentCode(""); }
      });
    },
    confirm: () => { void run((shell) => shell.confirm()); },
    beginDrain: () => { void run((shell) => shell.beginDrain()); },
    unlink: () => { if (model.actions.includes("unlink")) void run((shell) => shell.unlink()); },
    reset() {
      const acknowledgement = resetAcknowledgement(acknowledgedLoss);
      if (acknowledgement) void run(async (shell) => {
        try { return await shell.reset(acknowledgement); }
        finally { if (mounted.current) setAcknowledgedLoss(false); }
      });
    },
    syncNow: () => { void run((shell) => shell.manualSync()); },
    refresh: () => { void run(async () => { await checkPermissions(); }); },
    requestPermissions: () => { void run(async (shell) => { await checkPermissions(true); await shell.observeConditions(); }); },
  };
}
