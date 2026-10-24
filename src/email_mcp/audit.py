"""Append-only JSONL audit log for sends and runs, plus hourly rate counting."""

import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from email_mcp.config import Settings

LOG_FILENAME = "send_log.jsonl"
RATE_WINDOW = timedelta(minutes=60)


def log_path(settings: Settings) -> Path:
    """Location of the send log."""
    return settings.state_dir / LOG_FILENAME


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def log_event(settings: Settings, record: dict) -> None:
    """Append one JSON line, stamping ts if absent. Never raises on malformed records."""
    try:
        entry = dict(record)
        entry.setdefault("ts", now_iso())
        line = json.dumps(entry, default=repr, ensure_ascii=False)
    except Exception:
        line = json.dumps({"event": "malformed", "ts": now_iso(), "repr": repr(record)})
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        with log_path(settings).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        sys.stderr.write(f"email_mcp: could not write audit log: {exc}\n")


def iter_records(settings: Settings) -> Iterator[dict]:
    """Yield dict records in file order, skipping corrupt lines."""
    path = log_path(settings)
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def read_log(settings: Settings, limit: int = 50) -> list[dict]:
    """Return up to limit records, most recent first."""
    if limit <= 0:
        return []
    records = list(iter_records(settings))
    records.reverse()
    return records[:limit]


def parse_ts(value: object) -> datetime | None:
    """Parse an ISO timestamp; naive values are treated as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def is_counted_send(record: dict) -> bool:
    """True for a real (non-dry-run) send that succeeded. Only these count against the cap."""
    return (
        record.get("event") == "send"
        and not record.get("dry_run")
        and record.get("status") == "sent"
    )


def hourly_count(settings: Settings) -> int:
    """Real sends in the trailing 60 minutes."""
    since = datetime.now(UTC) - RATE_WINDOW
    count = 0
    for record in iter_records(settings):
        if not is_counted_send(record):
            continue
        ts = parse_ts(record.get("ts"))
        if ts is not None and ts >= since:
            count += 1
    return count
