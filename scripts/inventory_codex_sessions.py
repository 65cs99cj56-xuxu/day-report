#!/usr/bin/env python3
"""Inventory top-level Codex JSONL sessions for a local date window."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List top-level Codex sessions overlapping a local date window."
    )
    parser.add_argument("--from-date", required=True, type=date.fromisoformat)
    parser.add_argument("--to-date", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--window-end",
        type=parse_cli_timestamp,
        help=(
            "Fixed exclusive window end in ISO 8601 format. Pass the same value "
            "to inventory and every message page."
        ),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Codex home to inspect. Multica runtime homes are replaced with ~/.codex.",
    )
    parser.add_argument("--scope", choices=("all", "cwd"), default="all")
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--include-subagents", action="store_true")
    parser.add_argument(
        "--messages",
        action="store_true",
        help="Emit one globally ordered page of in-window messages from all sessions.",
    )
    parser.add_argument(
        "--session-id",
        help="Emit only in-window user/assistant messages for this session ID or prefix.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Message offset used with --messages or --session-id.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum messages returned per page with --messages or --session-id.",
    )
    return parser.parse_args()


def read_metadata(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as stream:
            record = json.loads(stream.readline())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    if record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=REPORT_TIMEZONE)
        return parsed.astimezone(REPORT_TIMEZONE)
    except ValueError:
        return None


def parse_cli_timestamp(value: str) -> datetime:
    parsed = parse_timestamp(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(
            "must be a valid ISO 8601 timestamp, for example "
            "2026-09-03T20:10:00+08:00"
        )
    return parsed


def is_multica_runtime_home(path: Path) -> bool:
    """Return whether path points at a Multica task's isolated Codex home."""
    resolved = path.expanduser().resolve()
    return "multica_workspaces" in resolved.parts and "codex-home" in resolved.parts


def select_codex_home(requested: Path | None) -> tuple[Path, str]:
    standard_home = (Path.home() / ".codex").resolve()
    env_value = os.environ.get("CODEX_HOME")

    if requested is not None:
        candidate = requested.expanduser().resolve()
        source = "argument"
    elif env_value:
        candidate = Path(env_value).expanduser().resolve()
        source = "CODEX_HOME"
    else:
        candidate = standard_home
        source = "default"

    if is_multica_runtime_home(candidate):
        candidate = standard_home
        source = "standard-home-fallback-from-multica-runtime"

    return candidate, source


def is_subagent(metadata: dict[str, Any]) -> bool:
    if metadata.get("thread_source") == "subagent":
        return True
    if metadata.get("parent_thread_id"):
        return True
    source = metadata.get("source")
    if source is None:
        return False
    try:
        return "subagent" in json.dumps(source, ensure_ascii=False).casefold()
    except (TypeError, ValueError):
        return False


def is_multica_session(metadata: dict[str, Any]) -> bool:
    originator = metadata.get("originator")
    return isinstance(originator, str) and originator.casefold() == "multica-agent-sdk"


def is_conversation_message(record: dict[str, Any]) -> bool:
    if record.get("type") != "response_item":
        return False
    payload = record.get("payload")
    return (
        isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") in {"user", "assistant"}
    )


def messages_in_window(
    path: Path, window_start: datetime, window_end: datetime
) -> tuple[list[dict[str, Any]], int, int]:
    messages: list[dict[str, Any]] = []
    record_count = 0
    invalid_record_count = 0

    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                record_count += 1
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    invalid_record_count += 1
                    continue

                if not is_conversation_message(record):
                    continue
                timestamp = parse_timestamp(record.get("timestamp"))
                if timestamp is None or not (window_start <= timestamp < window_end):
                    continue
                messages.append(
                    {
                        "line_number": record_count,
                        "timestamp": timestamp.isoformat(),
                        "role": record["payload"]["role"],
                        "content": record["payload"].get("content", []),
                    }
                )
    except (OSError, UnicodeDecodeError):
        return [], record_count, invalid_record_count + 1

    return messages, record_count, invalid_record_count


def iter_jsonl(codex_home: Path) -> Iterator[Path]:
    for directory_name in ("sessions", "archived_sessions"):
        directory = codex_home / directory_name
        if directory.is_dir():
            yield from directory.rglob("*.jsonl")


def main() -> int:
    args = parse_args()
    if args.from_date > args.to_date:
        print("--from-date must be earlier than or equal to --to-date", file=sys.stderr)
        return 2
    if args.scope == "cwd" and args.cwd is None:
        print("--cwd is required when --scope=cwd", file=sys.stderr)
        return 2
    if args.offset < 0:
        print("--offset must be greater than or equal to 0", file=sys.stderr)
        return 2
    if args.limit <= 0:
        print("--limit must be greater than 0", file=sys.stderr)
        return 2
    if args.messages and args.session_id:
        print("--messages and --session-id cannot be used together", file=sys.stderr)
        return 2

    run_started_at = datetime.now(REPORT_TIMEZONE)
    if args.to_date > run_started_at.date():
        print("--to-date cannot be later than the local current date", file=sys.stderr)
        return 2

    codex_home, codex_home_source = select_codex_home(args.codex_home)
    if not codex_home.is_dir():
        print(f"Codex home does not exist: {codex_home}", file=sys.stderr)
        return 3
    if is_multica_runtime_home(codex_home):
        print("Refusing to inventory a Multica runtime Codex home", file=sys.stderr)
        return 3

    local_tz = REPORT_TIMEZONE
    window_start = datetime.combine(args.from_date, time.min, local_tz)
    natural_window_end = datetime.combine(
        args.to_date + timedelta(days=1), time.min, local_tz
    )
    if args.window_end is not None:
        window_end = args.window_end
        if window_end <= window_start:
            print("--window-end must be later than the window start", file=sys.stderr)
            return 2
        if window_end > natural_window_end:
            print("--window-end cannot exceed the selected date range", file=sys.stderr)
            return 2
        if window_end > run_started_at:
            print("--window-end cannot be later than the current time", file=sys.stderr)
            return 2
        window_end_mode = "explicit-fixed"
    elif args.to_date == run_started_at.date():
        window_end = run_started_at
        window_end_mode = "run-started-at"
    else:
        window_end = natural_window_end
        window_end_mode = "next-day-exclusive"
    expected_cwd = args.cwd.resolve() if args.cwd else None
    sessions_by_id: dict[str, dict[str, Any]] = {}
    excluded = {
        "subagent": 0,
        "multica_agent_sdk": 0,
        "outside_window": 0,
        "invalid_metadata": 0,
    }

    for path in iter_jsonl(codex_home):
        metadata = read_metadata(path)
        if metadata is None:
            excluded["invalid_metadata"] += 1
            continue
        if not args.include_subagents and is_subagent(metadata):
            excluded["subagent"] += 1
            continue
        if is_multica_session(metadata):
            excluded["multica_agent_sdk"] += 1
            continue

        session_cwd_text = metadata.get("cwd")
        if expected_cwd is not None:
            if not isinstance(session_cwd_text, str):
                continue
            try:
                if Path(session_cwd_text).resolve() != expected_cwd:
                    continue
            except OSError:
                continue

        window_messages, record_count, invalid_record_count = messages_in_window(
            path, window_start, window_end
        )
        if not window_messages:
            excluded["outside_window"] += 1
            continue
        activity_start = window_messages[0]["timestamp"]
        activity_end = window_messages[-1]["timestamp"]

        stat = path.stat()
        created_at = parse_timestamp(metadata.get("timestamp"))
        if created_at is None:
            created_at = datetime.fromtimestamp(stat.st_ctime, local_tz)

        session_id = metadata.get("id") or metadata.get("session_id")
        item = {
            "session_id": session_id,
            "activity_start_at": activity_start,
            "activity_end_at": activity_end,
            "created_at": created_at.isoformat(),
            "cwd": session_cwd_text,
            "originator": metadata.get("originator"),
            "thread_source": metadata.get("thread_source"),
            "record_count": record_count,
            "matched_message_count": len(window_messages),
            "invalid_record_count": invalid_record_count,
            "bytes": stat.st_size,
            "path": str(path.resolve()),
        }
        dedupe_key = str(session_id or path.resolve())
        previous = sessions_by_id.get(dedupe_key)
        if previous is None or (
            item["activity_end_at"], item["bytes"]
        ) > (previous["activity_end_at"], previous["bytes"]):
            sessions_by_id[dedupe_key] = item

    sessions = sorted(
        sessions_by_id.values(),
        key=lambda item: (item["activity_start_at"], item["path"]),
    )
    if args.messages:
        all_messages: list[dict[str, Any]] = []
        for item in sessions:
            session_messages, _, _ = messages_in_window(
                Path(item["path"]), window_start, window_end
            )
            for message in session_messages:
                all_messages.append(
                    {
                        "session_id": item["session_id"],
                        "cwd": item["cwd"],
                        **message,
                    }
                )
        all_messages.sort(
            key=lambda message: (
                message["timestamp"],
                str(message["session_id"]),
                message["line_number"],
            )
        )
        page = all_messages[args.offset : args.offset + args.limit]
        next_offset = args.offset + len(page)
        output = {
            "source": "codex-jsonl-window-messages",
            "timezone": str(REPORT_TIMEZONE),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "window_end_mode": window_end_mode,
            "scope": args.scope,
            "session_count": len(sessions),
            "offset": args.offset,
            "limit": args.limit,
            "returned_count": len(page),
            "total_matching_messages": len(all_messages),
            "has_more": next_offset < len(all_messages),
            "next_offset": next_offset if next_offset < len(all_messages) else None,
            "messages": page,
        }
        json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.session_id:
        matches = [
            item
            for item in sessions
            if isinstance(item["session_id"], str)
            and item["session_id"].startswith(args.session_id)
        ]
        if not matches:
            print(
                f"No in-window session matches --session-id={args.session_id}",
                file=sys.stderr,
            )
            return 4
        if len(matches) > 1:
            print(
                f"Ambiguous --session-id={args.session_id}; use a longer prefix",
                file=sys.stderr,
            )
            return 4

        selected = matches[0]
        selected_messages, _, _ = messages_in_window(
            Path(selected["path"]), window_start, window_end
        )
        page = selected_messages[args.offset : args.offset + args.limit]
        next_offset = args.offset + len(page)
        output = {
            "source": "codex-jsonl-window-messages",
            "timezone": str(REPORT_TIMEZONE),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "session_id": selected["session_id"],
            "path": selected["path"],
            "offset": args.offset,
            "limit": args.limit,
            "returned_count": len(page),
            "total_matching_messages": len(selected_messages),
            "has_more": next_offset < len(selected_messages),
            "next_offset": next_offset if next_offset < len(selected_messages) else None,
            "messages": page,
        }
        json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    output = {
        "source": "codex-jsonl",
        "codex_home": str(codex_home),
        "codex_home_source": codex_home_source,
        "from_date": args.from_date.isoformat(),
        "to_date": args.to_date.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_end_mode": window_end_mode,
        "timezone": str(REPORT_TIMEZONE),
        "scope": args.scope,
        "cwd": str(expected_cwd) if expected_cwd else None,
        "count": len(sessions),
        "total_bytes": sum(item["bytes"] for item in sessions),
        "excluded": excluded,
        "sessions": sessions,
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
