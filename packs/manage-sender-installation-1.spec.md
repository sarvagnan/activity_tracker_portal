# Draft acceptance pack — `manage-sender-installation@1`

**Status:** Draft approved by the Operator on 2026-10-14, including the seven decisions below; the executable pack adds the refinements its tests pin down. Amended to `manage-sender-installation-pack/2` through deviation DEV-msi-01 (pack defect, Operator decision 2026-10-14; see Decision 6). Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `manage-sender-installation` Governed Scope contract in `governance/scopes/manage-sender-installation.1.json`. It is the Portal side of the Sender Installation lifecycle (ADR 0009, 0039, 0051). It is a Python module over the published `operational-store@1`, which owns the lifecycle state machine and its transactions, and `ingest-protocol@1`, which owns message bytes, fingerprints, and compatibility decisions. This unit decides when each transition may happen and answers the Sender.

On 2026-10-14 the Operator made two decisions about this scope:
- **Split by side.** The Sender's own lifecycle moves to a new scope, `manage-sender-enrollment`: its identity and key, the confirmation it sends, adopting capabilities, drain mode, and destroying its local state. The scope map now carries both rows.
- **An Operator-mediated lifecycle within Wire Protocol Version 1.** Version 1 has no message that refuses an enrollment and none that tells the Sender it is active, draining, or retired. The Operator, who holds both devices (ADR 0001), carries those facts between them instead.

HTTP routes belong to the Portal ingress shell, and presentation belongs to the Operations page and the `portal` command. Both call this Interface.

## Interface

Python module `activity_tracker_portal.sender_installation`, standard library only, using `activity_tracker_portal.operational_store` and `activity_tracker_portal.ingest_protocol`:

- `ENROLLMENT_CODE_LIFETIME = timedelta(minutes=10)`.
- `InstallationRefusal(failure)` and `InstallationRetryable(failure)` each carry one named failure from the contract.
- `Capabilities(wire_versions, projection_schemas, max_record_count, max_message_bytes)` is a frozen dataclass:
  - `wire_versions` is a tuple of integers;
  - `projection_schemas` is a tuple of `(record_type, versions)` pairs.
- `offer_enrollment(store, portal_url, now, *, random_bytes=secrets.token_bytes) -> EnrollmentOffer`. The result holds `code`, `expires_at`, and the exact `message` bytes.
- `answer_enrollment_confirmation(store, data, now) -> ConfirmationResponse`. The result holds:
  - `outcome`: `pending`, `retryable`, or `refused`;
  - `body`: the response bytes, or `None` when refused;
  - `failure`: the named failure, or `None` when pending.
- Operator lifecycle intents. Each returns the installation's `InstallationStatus` after the transition:
  - `confirm_installation(store, installation_id, fingerprint, now)`
  - `withdraw_installation(store, installation_id, now)`
  - `begin_draining(store, installation_id, now)`
  - `retire_drained(store, installation_id, final_sequence, now)`
  - `retire_destructively(store, installation_id, now, *, acknowledge_possible_loss)`
- `honor_capabilities(store, capabilities, now) -> int` returns the honored generation.
- `stop_superseded_generation(store, now) -> int | None` stops one generation and returns it, or returns `None` when none is superseded.
- `current_advertisement(store) -> bytes` returns the exact bytes of the highest honored generation.

Operator intents raise `InstallationRefusal` or `InstallationRetryable`. `answer_enrollment_confirmation` never raises for an outcome, because it answers a Sender.

## Requirements

Each requirement is bound to named executable tests. The Python suite `tests/acceptance/test_manage_sender_installation_1.py` drives the module against a real `operational-store@1` database in a temporary directory. It builds messages with `ingest-protocol@1`, and injects the random source, the times, the store's free-space reader, and lock contention. The runner `tests/acceptance/run_manage_sender_installation_1.sh` runs it. Fixtures are synthetic: UUIDs start with `00000000-0000-4000-8000-`, and Portal hosts end in `.invalid`.

### R1 — Governed Scope contract

The contract names:
- the scope, version, and kind;
- the purpose;
- the inputs, with their modes;
- the outputs;
- every named failure, with outcome class `refusal` or `retryable`;
- the Operational Store write effect;
- authority;
- the `ingest-protocol@1` and `operational-store@1` dependencies;
- the acceptance oracle.

It does not contradict the `manage-sender-installation` scope-map row. The scope map also carries the `manage-sender-enrollment` row for the Sender side, which depends on `ingest-protocol`.

Tests: `test_governed_scope_contract_has_required_shape`, `test_scope_map_rows_do_not_contradict_contract` (Python).

### R2 — Caller errors

The following raise `ValueError` before the store is used at all, because they are configuration or programming errors rather than outcomes. The tests pass a stand-in store that fails on any use:
- a naive `now`;
- a `portal_url` that the enrollment-offer schema rejects;
- `Capabilities` that the capability-advertisement schema rejects;
- a negative `final_sequence`;
- a random source that does not return exactly 10 bytes.

Tests: `test_naive_times_are_caller_errors`, `test_invalid_portal_url_is_a_caller_error`, `test_invalid_capabilities_are_caller_errors`, `test_negative_final_sequence_is_a_caller_error`, `test_wrong_length_random_source_is_a_caller_error`.

### R3 — Enrollment offers

`offer_enrollment` requires an honored capability generation, so every confirmation it invites can be answered. Without one, it raises `no-capability-generation` and records nothing.

The offer's fields:
- **Code:** the RFC 4648 base32 encoding of 10 random bytes: 16 characters from `A–Z2–7`, holding 80 bits.
- **Expiry:** `now` plus `ENROLLMENT_CODE_LIFETIME`, truncated to the millisecond, so it is exactly representable in the Wire Protocol timestamp form.
- **Message:** the canonical enrollment-offer bytes carrying the code, `portal_url`, and expiry.

The store records only the SHA-256 of the code's ASCII bytes and the expiry (`operational-store@1` R4). A code already offered raises `enrollment-code-exists`. Several offers may be outstanding at once, and each expires independently.

Tests: `test_offer_code_is_base32_of_ten_random_bytes`, `test_offer_records_only_the_code_digest_and_expiry`, `test_offer_message_is_canonical_and_expires_after_ten_minutes`, `test_offer_requires_an_honored_generation`, `test_repeated_code_is_refused`, `test_outstanding_offers_are_independent`.

### R4 — Answering enrollment confirmations

`answer_enrollment_confirmation` decodes `data` with `ingest-protocol@1`. The protocol enforces that the fingerprint matches the identity.

- **Pending:** an accepted confirmation creates a `pending` installation with that identity and fingerprint. The response is `pending`, with the exact stored bytes of the highest honored capability advertisement.
- **Refused, with no body:**
  - a decoding failure, under the protocol's name: `malformed-message`, `noncanonical-message`, or `unsupported-wire-protocol`;
  - a message of another kind: `malformed-message`;
  - `no-capability-generation`, checked before anything is enrolled;
  - `enrollment-code-unknown`, `enrollment-code-consumed`, or `enrollment-code-expired`.
- **Retryable:** `store-busy`, when another writer holds the store past its busy timeout. The response body is the canonical retryable-failure message `temporarily-unavailable`. The store enforces its storage reserve only when archiving messages, so no lifecycle write here can exhaust it.

A code is usable while `now` is earlier than its expiry. The Portal ingress shell turns a refused response into a transport-level refusal. The Sender treats every response that is neither a capability advertisement nor a retryable failure as refused (Decision 1).

Tests: `test_confirmation_returns_the_current_advertisement_bytes`, `test_confirmation_creates_a_pending_installation`, `test_undecodable_confirmations_are_refused_without_a_body`, `test_other_message_kinds_are_malformed`, `test_unknown_consumed_and_expired_codes_are_refused`, `test_code_is_usable_until_its_expiry`, `test_confirmation_without_a_generation_is_refused_before_enrolling`, `test_transient_store_conditions_are_retryable_responses`.

### R5 — Retried confirmations

A Sender that lost the response resends identical bytes, but its code is now consumed. The rule is:
- If the identity belongs to an installation that is `pending`, `active`, or `draining`, the answer is `pending` with the current advertisement: the highest generation honored at the time of the retry, not the one first sent. Nothing is written, whatever code the confirmation carries.
- An identity whose installation is `retired` or `withdrawn` is refused as `installation-ended`. An ended identity never enrolls again.
- A new identity presenting a consumed code is `enrollment-code-consumed`.

Tests: `test_retried_confirmation_returns_the_advertisement_without_writing`, `test_retries_receive_the_newest_advertisement`, `test_ended_identity_never_enrolls_again`, `test_consumed_code_refuses_a_new_identity`.

### R6 — Operator confirmation and withdrawal

`confirm_installation` requires the fingerprint the Operator compared with the one the Sender displays. A different fingerprint is `fingerprint-mismatch`.

It activates a pending installation only when no installation is active or draining. Otherwise it is `active-installation-exists`, and the previous installation must be retired first. Activating one installation leaves any other pending installations pending.

`withdraw_installation` ends a pending installation. Confirming or withdrawing an installation that is not pending is `installation-transition-invalid`.

Tests: `test_confirming_requires_the_compared_fingerprint`, `test_confirmation_activates_only_without_a_delivering_installation`, `test_other_pending_installations_stay_pending`, `test_pending_installation_can_be_withdrawn`, `test_only_pending_installations_can_be_confirmed_or_withdrawn`.

### R7 — Draining and retirement

`begin_draining` moves an active installation to `draining`, where the store still archives its deliveries.

`retire_drained` retires a draining installation as `drained` only when both of these hold:
- `final_sequence`, which the drained Sender reports, equals the installation's highest archived sequence, taken as 0 when it has archived nothing. Otherwise it is `drain-sequence-mismatch`.
- No sequence gap is recorded for the installation. Otherwise it is `drain-has-gaps`, because a gap means a message was lost and "drained" would claim otherwise.

`retire_destructively` retires an active or draining installation as `destructive`, and only with `acknowledge_possible_loss=True`. Otherwise it is `loss-not-acknowledged`.

Any other source state is `installation-transition-invalid`.

Tests: `test_draining_starts_only_from_active`, `test_drained_retirement_requires_the_final_sequence`, `test_drained_retirement_refuses_sequence_gaps`, `test_never_delivering_installation_drains_at_sequence_zero`, `test_destructive_retirement_requires_acknowledged_loss`, `test_destructive_retirement_from_active_or_draining`.

### R8 — Replacement, unlinking, and loss

The intents compose into the ADR 0009 flows:
- **Replacement:** offer, then answer the new Sender's confirmation (`answer_enrollment_confirmation`), which leaves it pending. Then drain and retire the old installation, and the Operator activates the new one (`confirm_installation`).
- **Unlinking:** drain, then retire, with no successor.
- **Replacing a lost phone:** retire the old installation destructively, then activate the new one (`confirm_installation`).

After replacement, the new active installation has no Last Delivery and no checkpoint, so its Polling Freshness is never-reported, however much the retired installation delivered.

Tests: `test_replacement_starts_with_no_operational_state`, `test_unlink_leaves_no_delivering_installation`, `test_lost_phone_is_replaced_after_destructive_retirement`.

### R9 — Capability generations

`honor_capabilities` encodes the configured content as a capability advertisement:
- When the content differs from the highest honored generation's, it uses the next generation number (1 when there is none) and honors those exact canonical bytes.
- When the content is unchanged, it returns the existing generation without writing.

`stop_superseded_generation` stops the lowest honored generation below the governing acknowledgement, in one store transaction, and returns it. An installation's acknowledgement is the highest staged generation among its archived capability acknowledgements: the highest acknowledged, not the most recently received. The governing acknowledgement depends on whether an installation is delivering:
- When an installation is active or draining, it is that installation's own acknowledgement. The installation now delivering is the only one whose queued messages can still arrive, and it may hold an older generation than a retired predecessor acknowledged.
- When no installation is delivering, it is the highest acknowledgement in the store, which every future enrollee exceeds because it receives the current advertisement.

Stopping below it is always within what `ingest-protocol@1` R10 permits. The Operations page or `portal` command repeats the call until it returns `None`. It returns `None` when there is no governing acknowledgement, including when the delivering installation has acknowledged nothing, or when nothing is below it (Decision 9). It raises `pending-installation-exists` while any installation is pending, because a pending Sender may hold an older advertisement it has not yet acknowledged (Decision 6).

`current_advertisement` returns the highest honored generation's exact bytes, or raises `no-capability-generation`.

Tests: `test_configured_capabilities_get_the_next_generation`, `test_unchanged_capabilities_keep_their_generation`, `test_advertisement_bytes_are_exact_and_canonical`, `test_superseded_generations_stop_only_below_the_latest_acknowledgement`, `test_latest_acknowledgement_is_the_highest_not_the_newest`, `test_each_call_stops_only_the_lowest_superseded_generation`, `test_a_replacement_keeps_its_generation_until_it_acknowledges_a_later_one`, `test_without_a_delivering_installation_any_acknowledgement_counts`, `test_generations_are_kept_while_an_installation_is_pending`, `test_current_advertisement_is_the_highest_generation`.

### R10 — Refusals change nothing

The store is left exactly as it was, as compared by SQLite's `iterdump` before and after, when either of these happens:
- an Operator intent raises `InstallationRefusal` or `InstallationRetryable`;
- a confirmation is answered `refused` or `retryable`.

Tests: `test_every_refusal_leaves_the_store_unchanged`, `test_retryable_conditions_leave_the_store_unchanged`, `test_activation_under_contention_is_retryable`.

### R11 — Health-data-free outcomes

Refusals, retryable outcomes, and refused or retryable confirmation responses carry only a named failure or retryable-failure bytes. They carry no confirmation bytes, codes, identities, or exception text.

Every named failure in the contract is reachable, and the module produces no failure the contract does not name.

Tests: `test_outcomes_carry_only_named_failures`, `test_every_named_failure_is_reachable_and_in_the_contract`.

### R12 — Order of checks

When several outcomes apply, the first listed for the operation wins. Caller errors (R2) always come first, and a transient store condition can arise at any store call.

| Operation | Order |
|---|---|
| `offer_enrollment` | `no-capability-generation`, `enrollment-code-exists` |
| `answer_enrollment_confirmation` | decoding failure, other kind, `no-capability-generation`, an existing identity (R5), `enrollment-code-unknown`, `enrollment-code-consumed`, `enrollment-code-expired` |
| `confirm_installation` | `installation-unknown`, `fingerprint-mismatch`, `installation-transition-invalid`, `active-installation-exists` |
| `withdraw_installation`, `begin_draining` | `installation-unknown`, `installation-transition-invalid` |
| `retire_drained` | `installation-unknown`, `installation-transition-invalid`, `drain-sequence-mismatch`, `drain-has-gaps` |
| `retire_destructively` | `loss-not-acknowledged`, `installation-unknown`, `installation-transition-invalid` |
| `stop_superseded_generation` | `pending-installation-exists` |
| `current_advertisement` | `no-capability-generation` |

Tests: `test_checks_follow_the_documented_order`.

## Decisions for the Operator

1. **How the Operator-mediated lifecycle works over Wire Protocol Version 1.** The principle was approved on 2026-10-14; these are the mechanics.
   - **Answering a confirmation.** A confirmation is answered with the current capability advertisement when its installation is pending, or already was. It is answered with a retryable failure for transient store conditions. Otherwise it is refused with no Wire Protocol bytes (R4).
   - **Learning of confirmation.** The Sender learns that the Operator confirmed it only when its deliveries start being accepted. The `accept-*` units therefore need to keep a pending installation's deliveries retryable rather than blocked; that is recorded here as their requirement.
   - **New capability generations.** `current_advertisement` gives the Portal ingress shell a way to offer the newest generation to an active Sender. A Portal upgrade's new limits or versions then reach the Sender without a new message kind.

   The alternative, a Wire Protocol Version 2 with explicit lifecycle messages, was declined.
2. **Drained retirement checks the Sender's final sequence.** The Operator enters the final sequence the drained phone displays. The Portal retires as drained only when that equals its highest archived sequence and no sequence gap exists (R7). Otherwise the only path is destructive retirement with acknowledged possible loss. This makes "drained" a checked claim rather than a button. The alternative is to trust the Operator's word that the phone drained.
3. **Enrollment codes.** Each code is 80 random bits as 16 base32 characters, valid for 10 minutes. The Portal stores only its digest, and several offers may be outstanding at once (R3). The alternatives are a different lifetime, or revoking earlier offers when a new one is issued; the store has no revocation operation in version 1.
4. **Retried confirmations are idempotent, and ended identities never return.** A resent confirmation from a pending, active, or draining identity gets the advertisement again without a write, even though its code was consumed (R5). The identity is not a credential (ADR 0009), so this reveals only the capability advertisement. A retired or withdrawn identity is refused for good. A reinstalled Sender is a new installation (ADR 0014).
5. **Activation requires the compared fingerprint.** The Operator confirms a specific pending installation by its fingerprint, so a second pending installation cannot be activated by mistake. How the Operations page displays fingerprints for comparison belongs to presentation.
6. **The Portal numbers capability generations.**
   - Configured content gets the next generation only when it changes.
   - Superseded generations stop only below the acknowledgement of the installation now delivering, or the highest acknowledgement in the store when none is delivering. That stays within the protocol's own safety rule. This was amended through DEV-msi-01 on 2026-10-14: the frozen `pack/1` used the store-wide acknowledgement, which a retired predecessor could raise past the generation its replacement still held.
   - Superseded generations never stop while an installation is pending, because a pending Sender may hold an older advertisement it acknowledges only after the Operator confirms it.

   The alternative is to number generations by hand in configuration.
7. **The scope split.** The new `manage-sender-enrollment` row is in `docs/governance/scope-map.md` and the Phase 3 list in `docs/governance/delivery-flow.md`, and Beads has a matching feature. The split itself was approved on 2026-10-14. Its row text is part of this review, and that unit gets its own Gate A.

## Decisions at the Gate B escalation

The approver escalated two changes made since Gate A. The Operator decided both on 2026-10-14.

8. **The Portal serializes store writes.** The guarantees of Decisions 2 and 6 hold because the one Portal process runs every store write one at a time, behind a single writer lock. That covers lifecycle intents, confirmation answers, generation changes, and message archival. The Portal ingress shell provides the lock and proves serialization in its own tests. The declined alternative was to deliver an `operational-store@2` first, with conditional retirement and generation-stop operations that would be atomic whatever the callers do.
9. **One generation per call.** `stop_superseded_generation` stops only the lowest superseded generation, in one transaction, and its callers repeat it. Every retryable outcome therefore still leaves the store unchanged (R10). The declined alternative was to stop all superseded generations in one call and accept a partly completed result when the store becomes busy.

## Ground rules

- By Decision 8, the Portal process runs every store-writing operation on one Operational Store one at a time: the lifecycle intents, confirmation answers, and generation changes here, and the `accept-*` units' archival. This unit's checks read a `StoreView` snapshot and then write in a separate store transaction. The `retire_drained` sequence and gap checks and the `stop_superseded_generation` pending check rely on that serialization. The Portal ingress shell provides it and proves it in its own acceptance tests.
- The module uses only the standard library and the published `operational_store` and `ingest_protocol` modules. It reads the store only through `StoreView` and writes only through the store's lifecycle and generation methods (ADR 0023).
- Tests use a real store on a temporary database. They create lock contention with an external `BEGIN IMMEDIATE` and a short busy timeout, and use fixed timezone-aware times and fixed random bytes.
- Tests archive capability acknowledgements and other Outbox Messages through the store, from `ingest-protocol@1`-encoded synthetic envelopes, to create delivery history, gaps, and acknowledgements.
- The runner does not trust a test command's exit status. It loads the bound suite itself and passes only when every `test_` method of the suite was loaded and ran, with none failed, errored, skipped, or expected to fail.
- The trusted toolchain boundary is `uv`, the pinned CPython, `pyproject.toml` and `uv.lock`, and the standard library's `unittest`.
- Not mechanically provable here; the approver confirms these in implementation review:
  - the default random source is `secrets.token_bytes`;
  - the implementation changes nothing in the trusted toolchain boundary and does not alter test machinery;
  - nothing logs or returns codes, confirmation bytes, or identities in refusals;
  - every effect goes through the store.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the Python acceptance suite, with 55 tests;
- the runner, which verifies that every test method ran and passed;
- the Interface stub `src/activity_tracker_portal/sender_installation.py`.

It also makes two refinements:
- **Retryable outcomes.** The draft listed `storage-reserve-exhausted` as a retryable outcome. The store enforces its reserve only when archiving messages, so no lifecycle write can exhaust it, and the contract names `store-busy` alone.
- **Stopping generations.** `stop_superseded_generation` stops one generation per call (Decision 9).

Against the stub, the two R1 contract tests pass, and the other 50 tests error with the stub's `NotImplementedError`.

The implementer's read-only implementability review found nothing blocking. Its six clarifications are reflected in:
- R2's stand-in store;
- R8's naming of the two confirmation steps;
- R9's test that the latest acknowledgement is the highest, not the newest;
- R10's per-call retryable outcomes;
- the serialization boundary in the ground rules and the contract.

`pack/2`, through DEV-msi-01, amends R9 and Decision 6 and adds three tests:
- the replacement regression the approver reproduced;
- the case with no delivering installation;
- activation under store contention, from the Codex review.

Against the `pack/1` implementation, the replacement regression fails and the other 54 pass.

The binding is:
- spec files: the contract and this pack;
- test files: `test_manage_sender_installation_1.py` and the runner.
