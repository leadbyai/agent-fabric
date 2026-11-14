"""Manage live watchers from a local, operator-owned bot-to-source manifest."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def desired_sources(manifest):
    """Validate all bindings before allowing any worker to start."""
    desired = {}
    claimed_sources = set()
    for entry in manifest["bindings"]:
        if entry.get("enabled") is not True:
            continue
        kind = entry["runtime"]
        bot = entry["botId"]
        sid = entry["nativeSessionId"] if kind == "claude" else entry["sessionId"]
        if kind not in ("claude", "hermes") or not bot or not sid:
            raise ValueError("invalid native binding")
        key = (kind, bot, sid)
        if key in desired:
            raise ValueError("duplicate native binding")
        fingerprints = {
            "claude": ("herdrSession", "paneId", "claudeConfigDir", "cwd"),
            "hermes": ("hermesStateDb", "installId", "profileId"),
        }
        if not all(entry.get(field) for field in (*fingerprints[kind], "openbotOrigin")):
            raise ValueError("incomplete native binding")
        physical_source = (kind, sid, *(entry[field] for field in fingerprints[kind]))
        if physical_source in claimed_sources:
            raise ValueError("source claimed by multiple bots")
        claimed_sources.add(physical_source)
        desired[key] = entry
    return desired


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--herdr", default="herdr")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    def load():
        return desired_sources(json.loads(args.manifest.read_bytes()))
    desired = load()
    if args.check:
        print(json.dumps({"bindings": len(desired)}))
        return
    if not os.environ.get("AGENT_FABRIC_IMPORT_SECRET"):
        parser.error("AGENT_FABRIC_IMPORT_SECRET is required")
    os.umask(0o077)
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    workers = {}
    with ExitStack() as cleanup:
        def stop_all():
            for _, worker in workers.values():
                if worker.poll() is None:
                    worker.terminate()
        cleanup.callback(stop_all)
        while True:
            try:
                desired = load()
            except (OSError, ValueError, KeyError, TypeError):
                desired = {}
                print("Sync manifest unavailable or invalid", file=sys.stderr, flush=True)
            for key, (config, worker) in list(workers.items()):
                if desired.get(key) != config or worker.poll() is not None:
                    if worker.poll() is None:
                        worker.terminate()
                        worker.wait(timeout=15)
                    del workers[key]
            for key, config in desired.items():
                if key in workers:
                    continue
                token = hashlib.sha256(json.dumps(key).encode()).hexdigest()
                position = args.state / token
                path = position.with_suffix(".json")
                path.write_text(json.dumps(config), encoding="utf-8")
                module = "external_sync_watcher.py" if key[0] == "claude" else "external_sync_hermes.py"
                cmd = [sys.executable, str(Path(__file__).with_name(module)),
                       "--config", str(path), "--ledger", str(position.with_suffix(".sqlite3"))]
                if key[0] == "claude":
                    cmd.extend(("--herdr", args.herdr))
                workers[key] = config, subprocess.Popen(cmd)
            time.sleep(5)


if __name__ == "__main__":
    main()
