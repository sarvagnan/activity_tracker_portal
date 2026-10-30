# Device smoke delivery (kv7)

This runbook walks the Operator through the first physical-device smoke delivery. The Sender development build runs on an Android phone and delivers real Health Connect data to a development Portal on this Mac, over Tailscale. The run checks what CI cannot:
- real Health Connect record shapes, changes, and deletions;
- enrollment and the lifecycle screens;
- the Android `expo/fetch` transport under faults;
- background execution;
- recovery after the app is killed.

It is a smoke test, not the release qualification of ADR 0036. Its output is a **health-data-free report**: types, counts, versions, timings, and failures. Real health data never enters the repository, CI, Beads, or chat.

Plan for about two hours. The background step can add an hour of waiting.

## What you need

**On the Mac**
- A checkout of this repository at `origin/main`.
- `fnm` with Node 26.11.1, and `uv`.
- The Android SDK under `~/Library/Android/sdk`, with platform-tools (`adb`) and build-tools (`apksigner`).
- The Tailscale app, signed in, with MagicDNS on.

**On the phone**
- Android 14 or later, with Health Connect.
- Some data already in Health Connect from another app, such as Google Fit, Fitbit, or Samsung Health. More types and a longer history make a better test.
- A secure screen lock: a PIN, pattern, or password. The Sender refuses to enroll without one.
- The Tailscale app, signed in to the same tailnet as the Mac.
- Developer options enabled, with **Wireless debugging** available. Android 11 and later support it, though a device policy can turn it off.
- For the one-time `adb` pairing and later `adb` commands, the phone and the Mac on the same Wi-Fi network.

Everything runs over Tailscale and Wi-Fi, so no cable is needed. The background step is more realistic that way, because Android relaxes its background limits while a phone is charging. If wireless debugging is unavailable, use [USB instead](#using-usb-instead).

**Rules for the data**
- The Portal store lives in `~/activity-tracker-smoke`, outside the repository. Never copy anything from it into the repository, Beads, CI, or chat.
- Only the output of `portal_smoke.py report` goes into the smoke report.
- `portal_smoke.py status` shows installation ids and fingerprints. Use it only in your terminal.
- The proxy log records request sizes and timings, never bodies.

The commands below work in both fish and bash. Replace each `<…>` placeholder with your own value. Run every `uv run` and `npm` command from the root of the checkout you prepare in step 1.

## Terminals

You will keep four terminals open, all in the checkout root:

| Terminal | Runs |
|---|---|
| A | Metro, the development JavaScript server |
| B | The Portal |
| C | The fault proxy, which also logs every request |
| D | Helper commands |

## 1. Prepare the checkout

In terminal D:

```sh
cd ~/Documents/activity_tracker_portal
git fetch origin
git worktree add ../activity_tracker_portal-worktrees/smoke origin/main
cd ../activity_tracker_portal-worktrees/smoke
uv run --frozen portal runtime
fnm exec --using=26.11.1 -- npm ci --prefix sender
```

`portal runtime` must print the supported Python and SQLite versions and exit without an error.

## 2. Find the Mac's Tailscale name

In terminal D:

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin)["Self"]; print(s["DNSName"].rstrip(".").lower()); print(s["TailscaleIPs"][0])'
```

The first line is `<portal-name>`, for example `my-mac.tail1234.ts.net`. The second is `<tailscale-ip>`, for example `100.101.102.103`.

The phone reaches the Mac at this name for two things:
- **Metro**, at `http://<portal-name>:8081`.
- **The Portal**, at `http://<portal-name>:8765`:
  - all lowercase;
  - no path and no trailing slash.

  The Sender accepts plain `http` only for `ts.net` names.

## 3. Build, install, and connect the Sender

Still in terminal D, build and sign the development app:

```sh
fnm exec --using=26.11.1 -- npm run prebuild --prefix sender
fnm exec --using=26.11.1 -- npm run build:android --prefix sender
fnm exec --using=26.11.1 -- npm run sign:android --prefix sender
```

The first build downloads the pinned JDK and Android packages, and takes several minutes. It produces an unsigned `app-debug-unsigned.apk`, which Android refuses to install. `sign:android` signs it into `sender/app/android/app/build/outputs/apk/debug/app-debug.apk`. It uses a development key in `~/.config/activity-tracker/android-debug.keystore`, outside the repository. It creates the key on first use and reuses it for every later build, so later builds install as updates. Set `ACTIVITY_TRACKER_DEBUG_KEYSTORE` to keep the key elsewhere.

**Pair `adb` with the phone over Wi-Fi.** You do this once; the pairing survives reboots.
1. On the phone, open *Developer options → Wireless debugging*. On Pixels, Developer options are under *Settings → System*. Turn Wireless debugging on, and allow the current network.
2. Tap **Pair device with pairing code**. The phone shows an IP address and port, and a six-digit code.
3. In terminal D, run the following, then enter the code:
   ```sh
   adb pair <phone-ip>:<pairing-port>
   ```
4. Back on the Wireless debugging screen, read the **IP address & Port** at the top. It is a different port from the pairing port. Then connect:
   ```sh
   adb connect <phone-ip>:<port>
   adb devices
   ```
   `adb devices` must list `<phone-ip>:<port>` as `device`.

The connection port can change when Wireless debugging restarts, for example after a reboot or a Wi-Fi change. If `adb` loses the phone, read the current port and repeat `adb connect`. You don't need to pair again.

**Install a clean Sender:**

```sh
adb uninstall org.activitytracker.sender
adb install sender/app/android/app/build/outputs/apk/debug/app-debug.apk
```

`adb uninstall` starts the run from a clean Sender. It deletes any earlier development Sender's enrollment, queued messages, and Health Connect grants. It prints `Failure [DELETE_FAILED_INTERNAL_ERROR]` or `Unknown package` when no Sender is installed, which is fine.

**Start Metro.** In terminal A, start it and leave it running:

```sh
fnm exec --using=26.11.1 -- npm run start --prefix sender/app
```

Metro listens on every network interface, so the phone reaches it over Tailscale. If macOS asks whether `node` may accept incoming connections, allow it.

Check the path from the phone: open `http://<portal-name>:8081/status` in the phone's browser. It must show `packager-status:running`. If it does not load, check that Tailscale is connected on the phone.

The development build loads its JavaScript from Metro. Keep Metro running, and Tailscale connected on the phone, for the whole smoke test.
- **Location.** The phone can be anywhere. Metro gives the phone bundle URLs on the same host the phone used, so they also work over Tailscale away from the Mac's Wi-Fi.
- **The development build's limit.** The launcher keeps Metro's address in memory only, and the background task loader does not use it. A background run while the app's process is still alive uses the JavaScript already loaded. A background run that has to start the app from scratch, after Android killed its process, may fail to load any JavaScript. Step 10 tests only the first case; see the note there.

## 4. Create and start the Portal

In terminal D, create the smoke Portal's configuration and store directory:

```sh
uv run --frozen python tools/device_smoke/portal_smoke.py init --dir ~/activity-tracker-smoke --portal-url http://<portal-name>:8765
```

This advertises every projected Health Connect type at schema version 1, with batches of up to 500 records and 1,000,000 bytes. The Portal itself listens only on `127.0.0.1:8766`. The fault proxy is what the phone reaches.

In terminal B, start the Portal:

```sh
uv run --frozen portal serve --config ~/activity-tracker-smoke/portal.json
```

In terminal C, start the fault proxy on the Tailscale address:

```sh
uv run --frozen python tools/device_smoke/fault_proxy.py serve --listen <tailscale-ip>:8765 --upstream http://127.0.0.1:8766 --state-dir ~/activity-tracker-smoke/proxy
```

If macOS asks whether Python may accept incoming connections, allow it.

Each request the proxy handles logs one line in terminal C, and the same line in `~/activity-tracker-smoke/proxy/requests.log`:

```text
<UTC time> #<n> method=POST route=outbox mode=pass req_bytes=… upstream=200 status=200 resp_bytes_sent=… ended=complete ms=… body=3#1
```

| Field | Meaning |
|---|---|
| `route` | `outbox`, `enrollment`, `capabilities`, or `other`. Paths and query strings are never logged. |
| `mode` | The fault applied to this request, or `pass`. |
| `upstream` | The status the Portal returned. |
| `status` | The status the proxy sent to the phone, or `-` when it sent none. |
| `resp_bytes_sent` | Response body bytes the Mac's network stack accepted for the phone. This is not proof the phone read them: a few socket buffers can sit between them. |
| `ended` | `complete`, `client-closed` (the phone gave up), `dropped`, `timeout`, or `error`. |
| `body=<k>#<a>` | The proxy numbers each distinct `POST` body `k` in the order it first sees it, and `a` counts its attempts. Other methods log `body=-`. |

Check the whole path from the phone: in the phone's browser, open `http://<portal-name>:8765/v1/capabilities`.
- You should see a short JSON capability advertisement, and terminal C logs a `method=GET route=capabilities … status=200` line.
- If the page does not load, check that Tailscale is connected on the phone and that the proxy is listening on `<tailscale-ip>`.

### Helper commands and the running Portal

The helper writes to the store, and the Portal holds the store's only writer lock. Every `portal_smoke.py` command except `init` therefore needs the Portal stopped:
1. Press Ctrl-C in terminal B.
2. Run the helper command in terminal D.
3. Start the Portal again with the same `portal serve` command.

If you forget, the helper prints `The Portal is running: stop portal serve, then retry.` and changes nothing.

The proxy can keep running throughout. While the Portal is stopped, the phone sees the Portal as unreachable, and that is harmless.

## 5. Enroll the phone

1. Stop the Portal (terminal B, Ctrl-C).
2. Issue an enrollment code, in terminal D:
   ```sh
   uv run --frozen python tools/device_smoke/portal_smoke.py offer --config ~/activity-tracker-smoke/portal.json
   ```
   It prints the Portal URL, a 16-character code, and its expiry. **The code expires after 10 minutes.**
3. Start the Portal again (terminal B).
4. On the phone, open **Activity Tracker Sender**.
   - The development launcher appears the first time. Under **DEVELOPMENT SERVERS**, type `http://<portal-name>:8081` in the URL field and tap **Connect**.
   - If the launcher already lists a server it found on the network, expand **New development server** to reach the URL field.
   - The launcher lists the app under recently opened apps afterwards. Whenever Android has stopped the app, opening it shows the launcher again: tap the app under **Recently opened**.
   - Allow notifications when asked.
   - The first time, the development menu introduces itself in a sheet. Tap **Continue**, then close the menu.
5. The screen reads **Enroll Sender**. Type the Portal URL exactly as `offer` printed it, then the enrollment code, and tap **Enroll**.
6. **Expected:**
   - The title reads **Enrolled; awaiting confirmation**, and a 64-character fingerprint appears.
   - Terminal C logs `method=POST route=enrollment … status=200`.
   - If instead it reads **Confirming enrollment**, the Portal was unreachable: fix the connection and tap **Retry confirmation**.
7. Stop the Portal, then list installations:
   ```sh
   uv run --frozen python tools/device_smoke/portal_smoke.py status --config ~/activity-tracker-smoke/portal.json
   ```
   The new installation is the one in state `pending`. Compare its fingerprint with the phone's, all 64 characters, in groups of eight.
8. Only if they match, confirm it:
   ```sh
   uv run --frozen python tools/device_smoke/portal_smoke.py confirm --config ~/activity-tracker-smoke/portal.json --installation <installation-id> --fingerprint <fingerprint>
   ```
9. Start the Portal again, then tap **Sync now** on the phone.
   - **Expected:** the title changes to **Enrollment confirmed**, with "Collection is enabled".
   - The Sender learns of the confirmation when the Portal acknowledges its first message.
   - If the Sync result reads "Sync waits for a confirmed enrollment.", tap **Sync now** once more.

## 6. Grant Health Connect access

1. Tap **Request read, history and background access**, and allow everything on the Health Connect sheet.
2. **Expected:** "All requested Health Connect read, history, and background permissions are granted."
   - If the phone's Health Connect lacks the history or background feature, the text instead says a Health Connect update is needed. Record which message you saw.
3. Leave the access granted. Removing it is exercised later, in step 12.

Returning from the Health Connect sheet starts the initial snapshot by itself, because every return to the foreground starts a Sync. Note the time you returned, and go straight on to step 7.

## 7. Initial snapshot

1. Watch terminal C for the snapshot that started when you returned from the Health Connect sheet. If no outbox line appears within a minute, tap **Sync now**.
2. Watch terminal C. Each delivered message logs one `method=POST route=outbox … status=200 … ended=complete` line, and each line's `body=<k>#1` shows a first attempt.
3. **Interrupt once.** While messages are still arriving, kill the app from terminal D:
   ```sh
   adb shell am force-stop org.activitytracker.sender
   ```
   Without `adb`, use *Settings → Apps → Activity Tracker Sender → Force stop* instead.

   Reopen the app, through **Recently opened** if the launcher appears. Reopening starts a Sync by itself.
   - **Expected:** delivery resumes where it stopped. Any `#2` attempt is a message the Portal may already hold, and the Portal treats it as a safe repeat.
   - If the snapshot finishes too quickly to interrupt, skip this and use the stall case in step 9 instead.
4. Repeat **Sync now** until the Sync result reads **Sync finished.** and a tap delivers only one new outbox message. That message is the run's own Sync Checkpoint: every completed run stages one, so a caught-up Sync still delivers a single message. A large history takes several runs.
5. Note the time again.
6. Write down:
   - roughly how long the snapshot took;
   - how many outbox lines appeared;
   - whether the screen stayed responsive while it ran, including **Refresh status**.

   Status reads grow with the queue (Beads mfv), so a slow screen during a large snapshot is worth recording.
7. Stop the Portal and print the report:
   ```sh
   uv run --frozen python tools/device_smoke/portal_smoke.py report --config ~/activity-tracker-smoke/portal.json
   ```
   **Expected:**
   - Each type you hold has `initial-snapshot` batches and one initial snapshot completion.
   - Sync Checkpoints are present, including one reported `interrupted` if you killed the app during a run.
   - Gaps are 0, and there are no delivery blocks.

   Start the Portal again.

## 8. Changes and deletions

1. **A new record.** Add one in a source app: log a weight, or take a short walk so steps are recorded. Wait a minute for the app to write to Health Connect, then tap **Sync now**.
2. **A deletion.**
   1. In the Health Connect app, open *Data and access*, choose a type that has data, and delete one entry.
   2. Tap **Sync now**.
3. **Expected.** Print the report after each operation, with the Portal stopped:
   - **After the new record:** that type's `change-cycle` line shows `projections` of at least 1.
   - **After the deletion:** the deleted type's `change-cycle` line shows `retractions` of at least 1.

   The report counts projections and retractions separately for each cycle kind, so the initial snapshot's records never count toward the change cycle.

## 9. Transport faults

Each fault applies to the **next** outbox delivery only, then the proxy goes back to passing traffic through. Every completed Sync stages a Sync Checkpoint, so tapping **Sync now** always produces an outbox delivery to fault.

For each case below:
1. Set the fault in terminal D.
2. Tap **Sync now** and wait for the result.
3. Read the new line in terminal C, and the **Sender status** text on the phone.
4. Tap **Sync now** once more, with no fault set.

**After every case:** the second Sync must deliver the same message, shown as `status=200 … ended=complete … body=<k>#2`, and the Sync must finish. The final report must still show 0 gaps.

| Case | Command | Expected on the proxy | Expected on the phone |
|---|---|---|---|
| Truncated body | `uv run --frozen python tools/device_smoke/fault_proxy.py set-fault --state-dir ~/activity-tracker-smoke/proxy truncate` | `status=200`, with `resp_bytes_sent` below the receipt's length | A delivery failure, normally "The Portal could not be reached. Try again." |
| Dropped connection | `… set-fault … drop` | One line with `upstream=200 status=- resp_bytes_sent=0 ended=dropped`, and **no second line for the same `body=<k>`** until you tap **Sync now** again | A delivery failure |
| Stalled body | `… set-fault … stall` | `ended=client-closed` after about 60,000 ms, never the full 180 s | The Sync stays busy for about a minute, then shows a delivery failure |
| Oversized body | `… set-fault … oversize` | `ended=client-closed` with `resp_bytes_sent` far below 64 MiB, soon after the request | A delivery failure |
| Refused, large body | `… set-fault … refuse-large` | `status=300`, then `ended=client-closed` within a second or two, with `resp_bytes_sent` far below 64 MiB. The Sender must refuse a 3xx answer and cancel its body at once, without reading it | A delivery failure |
| Redirect | `… set-fault … redirect` | One `status=307` line that ends `client-closed` within a second or two, with `resp_bytes_sent` far below 64 MiB, and **no second line for the same `body=<k>`** until you tap **Sync now** again | A delivery failure |

Record each case's `resp_bytes_sent` and `ms`. Read them with two limits in mind:
- **What the numbers measure.** `resp_bytes_sent` counts what the Mac's network stack accepted, and some socket buffering always sits between the Mac and the phone. The installed `expo/fetch` also buffers some bytes natively before JavaScript reads them. So a few hundred KiB after a cancellation is acceptable. A download that runs on to tens of MiB, or a stream that ends `complete`, is a finding.
- **What the truncation case proves.** It gives the phone a response shorter than its declared length. The proxy's timing cannot show whether Android noticed before or after the Sender acquired its reader; either way the Sender must report a failure and redeliver.

The **native retry** check spans every case, and the dropped-connection and redirect cases target it directly. OkHttp, beneath `expo/fetch`, can silently retry a request whose connection fails, or follow a redirect. One tap must cause exactly one attempt: a body's attempt number rises by one per tap, never by two.

## 10. Background polling

1. Unplug the phone if it is charging, so it runs on battery. Press the home button, leaving the app in the background.
   - Do not swipe the app away or force-stop it.
   - Keep Metro running on the Mac, and Tailscale connected on the phone.
   - If a background run cannot reach the Mac, open *Settings → Apps → Tailscale → Battery* and choose **Unrestricted**, to reduce battery-related VPN interruptions. Leave the Sender's own battery setting at its default; that is the case being tested.
2. The Sender asks Android for a background run at least every 60 minutes, and Android may defer it. Either wait, or force the job from terminal D:
   ```sh
   adb shell dumpsys jobscheduler | grep -i -A4 org.activitytracker.sender
   adb shell cmd jobscheduler run -f org.activitytracker.sender <job-id>
   ```
   Find the line for the Sender's `androidx.work.impl.background.systemjob.SystemJobService` job. It looks like `JOB #u0a172/4: … org.activitytracker.sender/androidx.work…`, and `<job-id>` is the number **after the slash**, `4` in that example.

   WorkManager schedules the next run under a new job id after every run, so run the `dumpsys` command again before each forced run. `Could not find job` means the id is stale.

   If forcing does not run anything, wait for Android instead. On battery, Android may defer the run well past 60 minutes. That is the realistic case: record how long it took.
3. **Expected:**
   - Terminal C logs outbox deliveries while the app stays in the background.
   - The next report shows a Sync Checkpoint with trigger `background`, and the installation's background scheduling status.

**What this step does not cover.** It exercises a background run while the app's process is still alive. In a development build, a background run that starts the app from scratch, after Android killed its process, may fail to load its JavaScript at all, because the launcher keeps Metro's address in memory only. That is a limit of the development build, not of the Sender. Record whether any background run did not happen, and whether Android had killed the app before it. Cold-start background runs need a build with the JavaScript embedded, and are tracked in Beads 2rv.16.

## 11. Drain, retire, and unlink

1. On the phone, tap **Begin draining**. The title reads **Draining installation**, and collection stops.
2. Tap **Sync now** until **Final sequence: N** appears. That means every queued message has been delivered.
3. Stop the Portal, then retire the installation at that sequence:
   ```sh
   uv run --frozen python tools/device_smoke/portal_smoke.py drain --config ~/activity-tracker-smoke/portal.json --installation <installation-id>
   uv run --frozen python tools/device_smoke/portal_smoke.py retire --config ~/activity-tracker-smoke/portal.json --installation <installation-id> --final-sequence <N>
   ```
   **Expected:** `retire` succeeds.
   - `refused: drain-sequence-mismatch` means the Portal holds a different highest sequence than the phone reports. That is a finding.
   - `refused: drain-has-gaps` means the Portal is missing a sequence. That is also a finding.
4. Start the Portal again. On the phone, switch on **Installation retired in Portal** and tap **Unlink**.
   - **Expected:** the screen returns to **Enroll Sender**.

## 12. Re-enroll and destructive reset

1. Repeat step 5 to enroll and confirm a second installation, with a fresh code. Then tap **Sync now** once.
   - `status` and `report` number installations in enrollment order, so the new one is the last. Still pick it by its `pending` state and a fingerprint that matches the phone.
   - You need not wait for its whole initial snapshot.
2. **Optional:** in the phone's Health Connect settings, revoke one permission for the Sender, then tap **Refresh status**.
   - **Expected:** the access text reports missing access, and an Operator condition about Health Connect access appears after the next Sync.
   - Grant the permission again.
3. Switch on **I acknowledge possible loss of unsent data**, and tap **Reset destructively**.
   - **Expected:** the screen returns to **Enroll Sender**.
4. Stop the Portal, and retire the second installation destructively:
   ```sh
   uv run --frozen python tools/device_smoke/portal_smoke.py retire-destructively --config ~/activity-tracker-smoke/portal.json --installation <second-installation-id> --acknowledge-possible-loss
   ```

## 13. Report and clean up

1. With the Portal stopped, save the report:
   ```sh
   uv run --frozen python tools/device_smoke/portal_smoke.py report --config ~/activity-tracker-smoke/portal.json
   ```
2. Read it through before sharing it. It must contain no identifiers, URLs, record values, or record times.
3. Fill in the smoke report below, and add it to Beads kv7. You can hand both to Claude to record.
4. File every surprise as its own bead.
5. Stop Metro, the Portal, and the proxy, and turn off Wireless debugging on the phone.
6. The store holds real health data. When you no longer need it, delete it:
   ```sh
   rm -rf ~/activity-tracker-smoke
   ```

## Smoke report template

```markdown
## kv7 device smoke report — <date>

- Sender commit: <git rev-parse --short HEAD of the smoke checkout>
- Device: <model>, Android <version>, Health Connect <version, from Settings → Apps>
- Permission state after step 6: <granted | features-unavailable | …>

| Step | Result | Notes (no health data) |
|---|---|---|
| 5 Enroll and confirm | pass/fail | |
| 6 Health Connect access | pass/fail | |
| 7 Initial snapshot | pass/fail | duration, outbox lines, responsiveness |
| 7 Interrupted run | pass/fail/skipped | |
| 8 Change and deletion | pass/fail | |
| 9 Truncated body | pass/fail | resp_bytes_sent, ms |
| 9 Stalled body | pass/fail | ms until client-closed |
| 9 Oversized body | pass/fail | resp_bytes_sent |
| 9 Refused large body | pass/fail | resp_bytes_sent |
| 9 Dropped connection, no native retry | pass/fail | |
| 9 Redirect, no native retry | pass/fail | |
| 10 Background run | pass/fail/waited | forced or natural |
| 11 Drain, retire, unlink | pass/fail | |
| 12 Re-enroll and reset | pass/fail | |

### Portal report

<paste the output of portal_smoke.py report>

### Surprises filed

- <bead id>: <one line>
```

## Troubleshooting

| What you see | What it means |
|---|---|
| "Use an https Portal URL, or http only for a Tailscale ts.net name." | The URL is not `http://<name>.ts.net:<port>` or `https://…`. Copy it from `offer`. |
| "The Portal URL or enrollment code was refused." | The code was mistyped or has expired, or the URL is not in lowercase. Issue a new code. |
| "Set a secure device screen lock before enrolling." | Set a PIN, pattern, or password, then enroll again. |
| "The Portal could not be reached. Try again." outside a fault case | Tailscale is off on the phone, the Portal or proxy is stopped, or the macOS firewall is blocking Python. |
| The title stays **Enrolled; awaiting confirmation** | The Portal has not confirmed the installation, or the phone has not synced since. Run step 5.7 to 5.9 again. |
| "Sync waits for a confirmed enrollment." | Tap **Sync now** again after confirming. |
| The development launcher shows no app, or cannot connect | Metro is not running, the URL is not `http://<portal-name>:8081`, Tailscale is off on the phone, or the macOS firewall is blocking `node`. Check `http://<portal-name>:8081/status` in the phone's browser. |
| `adb devices` lists nothing, or `adb connect` fails | Wireless debugging restarted with a new port, or the phone left the Mac's Wi-Fi. Read the current **IP address & Port** and run `adb connect` again. |
| `The Portal is running: stop portal serve, then retry.` | Stop the Portal in terminal B, then rerun the helper command. |
| `adb install` fails with `INSTALL_PARSE_FAILED_NO_CERTIFICATES` | The APK is unsigned. Run `fnm exec --using=26.11.1 -- npm run sign:android --prefix sender`, then install `app-debug.apk`, not `app-debug-unsigned.apk`. |
| `adb install` fails with `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | The installed Sender was signed with another key. Run `adb uninstall org.activitytracker.sender` first. |

## Using USB instead

The cable replaces only the `adb` connection. Metro, the Portal, and the fault proxy are still reached over Tailscale, so the launcher URL and every other step stay the same.

1. Turn on **USB debugging** in Developer options.
2. Connect the cable, and accept the debugging prompt on the phone.
3. In step 3, replace the pairing and `adb connect` with:
   ```sh
   adb devices
   ```
   It must list the phone as `device`.

Unplug the cable before step 10, so the background run happens on battery. To force the job over USB, plug in only for the `cmd jobscheduler run` command, then unplug. Or wait for a natural run.

## Rehearsing on the Android emulator

You can rehearse the run on an Android Studio emulator before using the phone. Use Android 14 or later, with a Google APIs image that allows `adb root`. The steps stay the same, with these differences:

- **Reaching the Mac.** The emulator has no Tailscale and cannot reach the Mac's Tailscale address. It reaches the Mac's loopback at `10.0.2.2` instead. The Sender accepts plain `http` only for `ts.net` names, so map `<portal-name>` to `10.0.2.2` in the emulator's hosts file. The mapping lasts until the emulator restarts:
  ```sh
  adb root
  adb shell 'cp /system/etc/hosts /data/local/tmp/hosts && echo "10.0.2.2 <portal-name>" >> /data/local/tmp/hosts && chcon u:object_r:system_file:s0 /data/local/tmp/hosts && mount -o bind /data/local/tmp/hosts /system/etc/hosts'
  ```
- **The proxy address.** In step 4, start the fault proxy with `--listen 127.0.0.1:8765` instead of the Tailscale address.
- **Screen lock.** Set one with `adb shell locksettings set-pin <pin>`.
- **Health data.** The emulator's Health Connect starts empty. Without an app that writes synthetic records, every type reports no data, and step 8 cannot run.
- **Battery.** For step 10, simulate battery power with `adb shell dumpsys battery unplug`.

Afterwards, undo the changes:

```sh
adb shell umount /system/etc/hosts
adb shell locksettings clear --old <pin>
adb shell dumpsys battery reset
adb unroot
```
