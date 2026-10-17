"""Dev-only, terminal-facing adapters for the physical-device smoke run."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from activity_tracker_portal import ingress, operational_store, sender_installation


def utc(value: datetime | None) -> str:
    return "none" if value is None else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def status(view: operational_store.StoreView) -> str:
    lines = []
    for number, item in enumerate(view.installations(), 1):
        lines.append(f"installation {number}")
        for name in ("installation_id", "fingerprint", "state", "highest_sequence", "enrolled_at",
                     "activated_at", "draining_at", "retired_at", "retirement_kind", "last_delivery_at",
                     "latest_checkpoint_receipt_at", "background_scheduling"):
            value = getattr(item, name)
            value = utc(value) if isinstance(value, datetime) else ("none" if value is None else value)
            lines.append(f"  {'id' if name == 'installation_id' else name}: {value}")
        lines.append(f"  sequence_gaps: {len(view.sequence_gaps(item.installation_id))}")
        lines.append(f"  delivery_blocks: {len(view.delivery_blocks(item.installation_id))}")
    return "\n".join(lines) if lines else "no installations"


def counts(values: Counter) -> str:
    return ", ".join(f"{key}×{value}" for key, value in sorted(values.items())) or "none"


def report(view: operational_store.StoreView) -> str:
    """Select operational counts only; never decode envelopes or projections."""
    lines = ["# Device smoke delivery report"]
    for number, item in enumerate(view.installations(), 1):
        installation_id = item.installation_id
        lines.extend(["", f"## installation {number}", f"- state: {item.state}",
                      f"- retirement_kind: {item.retirement_kind or 'none'}",
                      f"- highest_sequence: {item.highest_sequence or 0}",
                      f"- gap_count: {len(view.sequence_gaps(installation_id))}", "- lifecycle:"])
        for event in view.installation_events(installation_id):
            lines.append(f"  - {event.state}: {utc(event.at)}")
        messages = Counter()
        batches = defaultdict(Counter)
        projections = Counter()
        retractions = Counter()
        completions = defaultdict(Counter)
        completed_records = defaultdict(Counter)
        checkpoints = []
        for sequence in range(1, (item.highest_sequence or 0) + 1):
            message = view.archived_message(installation_id, sequence)
            messages[message.kind if message else "missing"] += 1
            if message is None:
                continue
            if message.batch is not None:
                batch = message.batch
                batches[batch.record_type][batch.cycle_kind] += 1
                projections[batch.record_type] += sum(entry.projection is not None for entry in batch.items)
                retractions[batch.record_type] += sum(entry.retracted_identity is not None for entry in batch.items)
            if message.completion is not None:
                completion = message.completion
                key = f"{completion.completion_kind}/{completion.snapshot_kind or 'none'}"
                completions[completion.record_type][key] += 1
                completed_records[completion.record_type][key] += completion.record_count
            if message.checkpoint is not None:
                checkpoints.append(message.checkpoint)
        lines.append(f"- messages: {counts(messages)}")
        for record_type in sorted(set(batches) | set(completions)):
            lines.extend([f"- record_type: {record_type}", f"  - ingest_batches: {counts(batches[record_type])}",
                          f"  - projections: {projections[record_type]}", f"  - retractions: {retractions[record_type]}",
                          f"  - completions: {counts(completions[record_type])}",
                          f"  - completion_record_counts: {counts(completed_records[record_type])}"])
        lines.append("- sync_checkpoints:")
        for checkpoint in checkpoints:
            fields = [f"{name}={getattr(checkpoint, name)}" for name in (
                "run_sequence", "trigger", "elapsed_ms", "background_scheduling",
                "outbox_messages_before_checkpoint", "outbox_bytes_before_checkpoint")]
            outcomes = Counter(outcome.outcome + (f"/{outcome.reason}" if outcome.reason else "")
                               for outcome in checkpoint.record_types)
            lines.append(f"  - {', '.join(fields)}; outcomes: {counts(outcomes)}")
    blocks = Counter()
    for block in view.delivery_blocks():
        blocks[block.failure] += block.count
    lines.extend(["", f"- delivery_blocks: {counts(blocks)}", "- honored_capability_generations:"])
    for generation in view.honored_generations():
        lines.append(f"  - {generation.generation}: {utc(generation.honored_at)}")
    reserve = view.storage_reserve()
    lines.append(f"- storage_reserve: admitting={str(reserve.admitting).lower()}, "
                 f"free_bytes={reserve.free_bytes}, reserve_bytes={reserve.reserve_bytes}")
    return "\n".join(lines)


def init(args: argparse.Namespace) -> None:
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "portal.json"
    schemas = Path(__file__).resolve().parents[2] / "projection/v1/schemas"
    data = {
        "store_path": str((directory / "store.sqlite3").resolve()), "portal_url": args.portal_url,
        "host": args.host, "port": args.port, "reserve_bytes": args.reserve_bytes,
        "capabilities": {"wire_versions": [1], "projection_schemas": [
            {"record_type": record_type, "versions": [1]}
            for record_type in sorted(file.name.removesuffix(".schema.json") for file in schemas.glob("*.schema.json"))],
            "max_record_count": args.max_record_count, "max_message_bytes": args.max_message_bytes},
    }
    try:
        with path.open("x", encoding="utf-8") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
    except FileExistsError:
        raise operational_store.StoreRefusal("config-already-exists") from None
    try:
        ingress.PortalSettings.from_file(path)
    except ValueError:
        path.unlink()
        raise operational_store.StoreRefusal("invalid-configuration") from None
    print(f"wrote {path}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init")
    create.add_argument("--dir", required=True)
    create.add_argument("--portal-url", required=True)
    create.add_argument("--host", default="127.0.0.1")
    create.add_argument("--port", type=int, default=8766)
    create.add_argument("--reserve-bytes", type=int, default=1073741824)
    create.add_argument("--max-record-count", type=int, default=500)
    create.add_argument("--max-message-bytes", type=int, default=1000000)
    for name in ("offer", "status", "confirm", "drain", "retire", "retire-destructively", "withdraw", "report"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        if name in ("confirm", "drain", "retire", "retire-destructively", "withdraw"):
            command.add_argument("--installation", required=True)
        if name == "confirm":
            command.add_argument("--fingerprint", required=True)
        if name == "retire":
            command.add_argument("--final-sequence", type=int, required=True)
        if name == "retire-destructively":
            command.add_argument("--acknowledge-possible-loss", action="store_true", required=True)
    return result


def execute(args: argparse.Namespace, store: operational_store.OperationalStore, settings: ingress.PortalSettings) -> None:
    now = datetime.now(timezone.utc)
    if args.command in ("status", "report"):
        with store.read() as view:
            output = status(view) if args.command == "status" else report(view)
        print(output)
    elif args.command == "offer":
        sender_installation.honor_capabilities(store, settings.capabilities, now)
        offer = sender_installation.offer_enrollment(store, settings.portal_url, now)
        print(f"Portal URL: {settings.portal_url}\nEnrollment code: {offer.code}\n"
              f"Expires: {utc(offer.expires_at)} ({offer.expires_at.astimezone().isoformat()})")
    else:
        if args.command == "confirm":
            item = sender_installation.confirm_installation(store, args.installation, args.fingerprint, now)
        elif args.command == "drain":
            item = sender_installation.begin_draining(store, args.installation, now)
        elif args.command == "retire":
            if args.final_sequence < 0:
                raise operational_store.StoreRefusal("invalid-final-sequence")
            item = sender_installation.retire_drained(store, args.installation, args.final_sequence, now)
        elif args.command == "retire-destructively":
            item = sender_installation.retire_destructively(store, args.installation, now, acknowledge_possible_loss=True)
        else:
            item = sender_installation.withdraw_installation(store, args.installation, now)
        print(f"installation {args.installation} is now {item.state}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            init(args)
        else:
            settings = ingress.PortalSettings.from_file(args.config)
            writer = ingress.PortalWriter(settings.store_path, reserve_bytes=settings.reserve_bytes)
            try:
                writer.run(lambda store: execute(args, store, settings))
            finally:
                writer.close()
        return 0
    except ingress.PortalWriterBusy:
        print("The Portal is running: stop portal serve, then retry.", file=sys.stderr)
        return 3
    except (sender_installation.InstallationRefusal, operational_store.StoreRefusal) as error:
        print(f"refused: {error.failure}", file=sys.stderr)
        return 2
    except (sender_installation.InstallationRetryable, operational_store.StoreRetryable) as error:
        print(f"retryable: {error.failure}", file=sys.stderr)
        return 4
    except (OSError, ValueError):
        print("refused: invalid-configuration-or-io", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
