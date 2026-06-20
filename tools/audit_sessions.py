import argparse
import json
import os
import shutil
import time
from pathlib import Path

MARKERS = ("<invoke name=", "<parameter name=", "antml:invoke", "antml:function_calls")


def assistant_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    chunks = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(block.get("text", ""))
    return "\n".join(chunks)


def active_sessions(claude_dir):
    sessions = {}
    sessions_dir = claude_dir / "sessions"
    if not sessions_dir.exists():
        return sessions
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        session_id = data.get("sessionId")
        if session_id:
            sessions[session_id] = {
                "pid": data.get("pid"),
                "status": data.get("status"),
                "version": data.get("version"),
                "path": str(path),
            }
    return sessions


def scan_transcript(path):
    stats = {
        "session_id": path.stem,
        "path": str(path),
        "size": path.stat().st_size,
        "events": 0,
        "assistant_events": 0,
        "leak_events": 0,
        "last_leak_line": 0,
    }
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            stats["events"] = lineno
            try:
                ev = json.loads(line)
            except Exception:
                continue
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            if msg.get("role") != "assistant":
                continue
            stats["assistant_events"] += 1
            text = assistant_text(msg)
            if text and any(marker in text for marker in MARKERS):
                stats["leak_events"] += 1
                stats["last_leak_line"] = lineno
    return stats


def scan_all(claude_dir):
    projects = claude_dir / "projects"
    if not projects.exists():
        return []
    rows = []
    active = active_sessions(claude_dir)
    for path in projects.rglob("*.jsonl"):
        try:
            row = scan_transcript(path)
        except Exception as exc:
            row = {"session_id": path.stem, "path": str(path), "error": repr(exc)}
        row["active"] = active.get(row["session_id"], {})
        rows.append(row)
    rows.sort(key=lambda r: (r.get("leak_events", 0), r.get("size", 0)), reverse=True)
    return rows


def print_table(rows, min_leaks):
    shown = [r for r in rows if r.get("leak_events", 0) >= min_leaks or r.get("error")]
    if not shown:
        print("No risky sessions found.")
        return
    print("session_id                           MB     leaks  active  status  path")
    for row in shown:
        active = row.get("active") or {}
        active_mark = "yes" if active else "no"
        status = active.get("status", "")
        mb = row.get("size", 0) / 1_000_000
        print(
            f"{row.get('session_id','')[:36]:36} "
            f"{mb:6.1f} {row.get('leak_events',0):6} "
            f"{active_mark:6} {status[:7]:7} {row.get('path','')}"
        )


def quarantine(rows, session_id, claude_dir, force):
    matches = [r for r in rows if r.get("session_id") == session_id]
    if not matches:
        raise SystemExit(f"No transcript found for session {session_id}")
    row = matches[0]
    if row.get("active") and not force:
        raise SystemExit(
            f"Refusing to quarantine active session {session_id}. "
            "Exit Claude Code first, or rerun with --force if you accept the risk."
        )
    src = Path(row["path"])
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = claude_dir / "backups" / "stall-recover-quarantine" / stamp
    root.mkdir(parents=True, exist_ok=True)
    dst = root / src.name
    shutil.move(str(src), str(dst))
    print(f"Moved {src} -> {dst}")


def main():
    parser = argparse.ArgumentParser(description="Audit Claude Code transcripts for leaked XML tool calls.")
    parser.add_argument("--claude-dir", default=str(Path.home() / ".claude"))
    parser.add_argument("--min-leaks", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quarantine", metavar="SESSION_ID")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    claude_dir = Path(args.claude_dir)
    rows = scan_all(claude_dir)
    if args.quarantine:
        quarantine(rows, args.quarantine, claude_dir, args.force)
        return
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_table(rows, args.min_leaks)


if __name__ == "__main__":
    main()
