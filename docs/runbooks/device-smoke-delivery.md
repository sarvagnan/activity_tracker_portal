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
- Android SDK platform-tools (`adb`) under `~/Library/Android/sdk`.
- The Tailscale app, signed in, with MagicDNS on.

**On the phone**
- Android 14 or later, with Health Connect.
- Some data already in Health Connect from another app, such as Google Fit, Fitbit, or Samsung Health. More types and a longer history make a better test.
- A secure screen lock: a PIN, pattern, or password. The Sender refuses to enroll without one.
- The Tailscale app, signed in to the same tailnet as the Mac.
- Developer options enabled, with USB debugging on. A USB cable.

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

## 2. Build and install the Sender

Still in terminal D:

```sh
fnm exec --using=26.11.1 -- npm run prebuild --prefix sender
fnm exec --using=26.11.1 -- npm run build:android --prefix sender
```

The first build downloads the pinned JDK and Android packages, and takes several minutes. It produces `sender/app/android/app/build/outputs/apk/debug/app-debug.apk`.

Connect the phone over USB and accept the debugging prompt on the phone. Then:

```sh
adb devices
adb install -r sender/app/android/app/build/outputs/apk/debug/app-debug.apk
adb reverse tcp:8081 tcp:8081
```

`adb devices` must list the phone as `device`, not `unauthorized`.

`adb reverse` lets the phone reach Metro on the Mac. Repeat it whenever you reconnect the cable.

In terminal A, start Metro and leave it running:

```sh
fnm exec --using=26.11.1 -- npm run start --prefix sender/app
```

The development build loads its JavaScript from Metro, including for background tasks. Keep the phone connected and Metro running for the whole smoke test.

## 3. Find the Mac's Tailscale name

In terminal D:

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin)["Self"]; print(s["DNSName"].rstrip(".").lower()); print(s["TailscaleIPs"][0])'
```

The first line is `<portal-name>`, for example `my-mac.tail1234.ts.net`. The second is `<tailscale-ip>`, for example `100.101.102.103`.

The Portal URL is `http://<portal-name>:8765`:
- all lowercase;
- no path and no trailing slash.

The Sender accepts plain `http` only for `ts.net` names.

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
   - If the development launcher appears, choose the Metro server at `http://localhost:8081`.
   - Allow notifications when asked.
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

## 7. Initial snapshot

1. Note the time, then tap **Sync now**.
2. Watch terminal C. Each delivered message logs one `method=POST route=outbox … status=200 … ended=complete` line, and each line's `body=<k>#1` shows a first attempt.
3. **Interrupt once.** While messages are still arriving, kill the app from terminal D:
   ```sh
   adb shell am force-stop org.activitytracker.sender
   ```
   Reopen the app and tap **Sync now**.
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
3. **Expected,** with the Portal stopped and the report printed again:
   - that type shows a `change-cycle` batch with at least one projection, after the new record;
   - and one with at least one retraction, after the deletion.

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

1. Press the phone's home button, leaving the app in the background. Keep the cable connected and Metro running.
2. The Sender asks Android for a background run at least every 60 minutes, and Android may defer it. Either wait, or force the job from terminal D:
   ```sh
   adb shell dumpsys jobscheduler | grep -i -A4 org.activitytracker.sender
   adb shell cmd jobscheduler run -f org.activitytracker.sender <job-id>
   ```
   `<job-id>` is the number after `#` on the line naming `org.activitytracker.sender`, for its `androidx.work` job. If forcing does not run anything, wait for Android instead.
3. **Expected:**
   - Terminal C logs outbox deliveries while the app stays in the background.
   - The next report shows a Sync Checkpoint with trigger `background`, and the installation's background scheduling status.

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
   - In `status`, the new installation is the one in state `pending` whose fingerprint matches the phone. The list is ordered by installation id, not by age, so it need not be the last one.
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
5. Stop Metro, the Portal, and the proxy.
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
| The development launcher shows no app | Metro is not running, or the cable was reconnected without `adb reverse tcp:8081 tcp:8081`. |
| `The Portal is running: stop portal serve, then retry.` | Stop the Portal in terminal B, then rerun the helper command. |
