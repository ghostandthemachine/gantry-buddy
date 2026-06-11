#!/usr/bin/env python3
"""Stream Gantry status to Gantry Buddy over USB serial.

The firmware already accepts the Hardware Buddy heartbeat JSON over Serial.
This bridge adapts Gantry's local JSONL files into that shape so the stick can
show current runs, recent completions, and short app-sent messages.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import serial
except ImportError:  # pragma: no cover - exercised by local environment only
    serial = None


MAX_ENTRIES = 8
MAX_LINE = 91
DEFAULT_BAUD = 115200


def gantry_home() -> Path:
    return Path(os.environ.get("GANTRY_HOME") or Path.home() / ".gantry")


def find_port() -> str | None:
    patterns = [
        "/dev/cu.usbserial-*",
        "/dev/cu.wchusbserial*",
        "/dev/cu.SLAB_USBtoUART*",
        "/dev/cu.usbmodem*",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


def trim(text: Any, limit: int = MAX_LINE) -> str:
    s = str(text or "").replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "..."


def read_jsonl(path: Path, max_lines: int = 400) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    rows: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def parse_ts(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def replay_activities(rows: list[dict[str, Any]]) -> OrderedDict[str, dict[str, Any]]:
    activities: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in rows:
        op = row.get("op")
        if op == "start":
            activity = row.get("activity")
            if isinstance(activity, dict) and activity.get("id"):
                activities[str(activity["id"])] = dict(activity)
            continue

        activity_id = row.get("id")
        if not activity_id or activity_id not in activities:
            continue
        activity = activities[str(activity_id)]
        if op == "event" and isinstance(row.get("event"), dict):
            activity.setdefault("events", []).append(row["event"])
        elif op == "progress":
            activity["progress"] = row.get("progress")
        elif op == "progressInfo" and isinstance(row.get("progressInfo"), dict):
            activity["progressInfo"] = row["progressInfo"]
        elif op == "title":
            activity["title"] = row.get("title")
        elif op == "complete":
            activity["status"] = row.get("status")
            activity["completedAt"] = row.get("completedAt")
            if row.get("summaryText"):
                activity["summaryText"] = row.get("summaryText")
    return activities


def group_runs(rows: list[dict[str, Any]]) -> OrderedDict[str, list[dict[str, Any]]]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        if row.get("schema") != "gantry-runs/v2":
            continue
        run_id = row.get("runId")
        event = row.get("event")
        if not run_id or not isinstance(event, dict):
            continue
        grouped.setdefault(str(run_id), []).append(row)
    return grouped


def event_label(event: dict[str, Any]) -> str:
    typ = event.get("type")
    if typ == "phase_update":
        return trim(event.get("phase"))
    if typ == "message_start":
        model = event.get("model") or "model"
        return trim(f"started {model}")
    if typ == "message_complete":
        return trim(event.get("content") or "completed")
    if typ == "usage":
        usage = event.get("usage") or {}
        out = usage.get("outputTokens") or 0
        return trim(f"usage {out} out")
    return trim(typ or "event")


def usage_tokens_for_today(groups: OrderedDict[str, list[dict[str, Any]]]) -> int:
    today = datetime.now().astimezone().date()
    total = 0
    for rows in groups.values():
        latest_usage: dict[str, Any] | None = None
        latest_ts = 0.0
        for row in rows:
            ts = parse_ts(row.get("ts"))
            if not ts:
                continue
            if datetime.fromtimestamp(ts).astimezone().date() != today:
                continue
            event = row.get("event") or {}
            usage = event.get("usage")
            if isinstance(usage, dict) and ts >= latest_ts:
                latest_usage = usage
                latest_ts = ts
        if latest_usage:
            total += int(latest_usage.get("outputTokens") or 0)
    return total


def build_snapshot(
    home: Path,
    stale_seconds: int,
    history_hours: float,
    message: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    cutoff = now - history_hours * 3600
    activity_rows = read_jsonl(home / "activities.jsonl")
    run_rows = read_jsonl(home / "runs.jsonl", max_lines=800)
    activities = replay_activities(activity_rows)
    runs = group_runs(run_rows)

    feed: list[tuple[float, str]] = []
    running = 0
    waiting = 0

    for activity in activities.values():
        status = activity.get("status")
        title = trim(activity.get("title") or activity.get("originalCommand") or "activity")
        ts = parse_ts(activity.get("completedAt") or activity.get("startedAt"))
        if ts and ts < cutoff:
            continue
        if status in {"queued", "running"}:
            running += 1
            feed.append((ts or now, f"run: {title}"))
        elif status in {"failed", "cancelled"}:
            feed.append((ts or 0.0, f"{status}: {title}"))
        elif status == "success":
            feed.append((ts or 0.0, f"done: {title}"))

    for run_id, rows in runs.items():
        latest = rows[-1]
        latest_ts = parse_ts(latest.get("ts"))
        event = latest.get("event") or {}
        event_type = event.get("type")
        is_active = event_type != "message_complete" and latest_ts and now - latest_ts <= stale_seconds
        if is_active:
            running += 1
            feed.append((latest_ts, f"model: {event_label(event)}"))
        elif latest_ts >= cutoff:
            feed.append((latest_ts, f"last: {event_label(event)}"))

    if message:
        feed.append((now + 1, trim(message)))

    entries = [label for _, label in sorted(feed, key=lambda item: item[0], reverse=True)]
    entries = entries[:MAX_ENTRIES]
    if not entries:
        entries = ["Gantry idle"]

    total = min(99, max(len(feed), running))
    msg = trim(message or entries[0], 23)
    tokens_today = usage_tokens_for_today(runs)

    return {
        "total": total,
        "running": min(99, running),
        "waiting": waiting,
        "msg": msg,
        "entries": entries,
        "tokens": tokens_today,
        "tokens_today": tokens_today,
    }


def time_sync() -> dict[str, Any]:
    now = datetime.now().astimezone()
    offset = int(now.utcoffset().total_seconds()) if now.utcoffset() else 0
    return {"time": [int(now.timestamp()), offset]}


def send_line(port: Any, obj: dict[str, Any], dry_run: bool) -> None:
    line = json.dumps(obj, separators=(",", ":"))
    if dry_run:
        print(line)
        return
    port.write((line + "\n").encode("utf-8"))
    port.flush()


def open_serial(path: str, baud: int) -> Any:
    if serial is None:
        sys.exit("pyserial is required: python3 -m pip install pyserial")
    return serial.Serial(path, baud, timeout=0.2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port; auto-detected when omitted")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--gantry-home", type=Path, default=gantry_home())
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--stale-seconds", type=int, default=45)
    parser.add_argument("--history-hours", type=float, default=24.0)
    parser.add_argument("--owner", default=os.environ.get("USER") or "")
    parser.add_argument("--name", default="Gantry")
    parser.add_argument("--message", help="prepend a one-shot message to the display")
    parser.add_argument("--once", action="store_true", help="send one snapshot and exit")
    parser.add_argument("--dry-run", action="store_true", help="print JSON instead of opening serial")
    args = parser.parse_args()

    port_path = args.port or find_port()
    if not args.dry_run and not port_path:
        sys.exit("no M5Stick serial port found")

    serial_port = None
    if not args.dry_run:
        serial_port = open_serial(port_path, args.baud)
        time.sleep(1.5)
        serial_port.reset_input_buffer()
        print(f"streaming Gantry status to {port_path}", file=sys.stderr)

    try:
        send_line(serial_port, time_sync(), args.dry_run)
        if args.owner:
            send_line(serial_port, {"cmd": "owner", "name": trim(args.owner, 24)}, args.dry_run)
        if args.name:
            send_line(serial_port, {"cmd": "name", "name": trim(args.name, 20)}, args.dry_run)

        while True:
            snapshot = build_snapshot(
                args.gantry_home,
                args.stale_seconds,
                args.history_hours,
                args.message,
            )
            send_line(serial_port, snapshot, args.dry_run)
            if args.once:
                return 0
            args.message = None
            time.sleep(args.interval)
    finally:
        if serial_port is not None:
            serial_port.close()


if __name__ == "__main__":
    raise SystemExit(main())
