"""Preview or deliver a bounded historical slice of one pinned Claude transcript."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile

from external_sync_cursor import CursorLedger
from external_sync_watcher import Binding, ImportClient, resolve_claude


def _timestamp(raw: str) -> float:
    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("a timezone is required")
    return stamp.timestamp()


def backfill(binding, source, ledger_path, start, end, *, client=None, max_batches=100):
    """Use a separate ledger and the same deterministic IDs as the live watcher."""
    ledger = CursorLedger(ledger_path)
    count = 0
    for _ in range(max_batches):
        cursor = ledger.open_or_initialize(
            bot_id=binding.bot_id, session_id=source.session_id, path=source.path, backfill=True,
        )
        batch = ledger.read_batch(cursor, path=source.path, limit=20)
        if batch.next_offset == cursor.offset:
            return count, True
        events = tuple(
            replace(event, sender=binding.terminal_sender) if event.kind == "human" else event
            for event in batch.events
            if start <= _timestamp(event.timestamp) < end
        )
        if client and events:
            client.deliver(binding.bot_id, events)
        ledger.acknowledge(batch)
        count += len(events)
    return count, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--from-time", type=_timestamp, required=True)
    parser.add_argument("--before-time", type=_timestamp, required=True)
    parser.add_argument("--herdr", default="herdr")
    parser.add_argument("--max-batches", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--job-ledger", type=Path)
    args = parser.parse_args()
    if args.from_time >= args.before_time or not 1 <= args.max_batches <= 10000:
        parser.error("invalid time interval or batch limit")
    if args.apply != bool(args.job_ledger):
        parser.error("--apply and --job-ledger must be used together")
    config = json.loads(args.config.read_bytes())
    if config.get("enabled") is not True or not config.get("nativeSessionId"):
        parser.error("requires an enabled binding with an explicit native session ID")
    binding = Binding(
        config["botId"], config["herdrSession"], config["paneId"],
        Path(config["claudeConfigDir"]).resolve(), Path(config["cwd"]).resolve(),
        config["nativeSessionId"], config.get("terminalSender", "Derek (terminal)"),
    )
    source = resolve_claude(binding, herdr_bin=args.herdr)
    with tempfile.TemporaryDirectory(prefix="afx-backfill-preview-") as tmp:
        client = None
        ledger = Path(tmp) / "preview.sqlite3"
        if args.apply:
            # The job identity includes the source and exact time window. It cannot
            # overwrite or advance the live sync ledger.
            job = args.job_ledger.resolve()
            identity = json.dumps([binding.bot_id, source.session_id, args.from_time, args.before_time])
            job.mkdir(parents=True, exist_ok=True, mode=0o700)
            ledger = job / (hashlib.sha256(identity.encode()).hexdigest() + ".sqlite3")
            client = ImportClient(config["openbotOrigin"], os.environ["AGENT_FABRIC_IMPORT_SECRET"])
        count, complete = backfill(
            binding, source, ledger, args.from_time, args.before_time,
            client=client, max_batches=args.max_batches,
        )
    print(json.dumps({"events": count, "sourceExhausted": complete, "applied": args.apply}))


if __name__ == "__main__":
    main()
