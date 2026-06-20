import json
import os
import sys
import time
from pathlib import Path

MARKERS = ("<invoke name=", "<parameter name=", "antml:invoke", "antml:function_calls")
DEFAULT_MAX_BYTES = 6_000_000
DEFAULT_MAX_LEAK_EVENTS = 8
REPORT_DIR = Path.home() / ".claude" / "hooks" / "quarantine"
RECOVERY_COMMANDS = ("/compact", "/clear", "/rewind", "/model")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def read_input():
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        raw = ""
    try:
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


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


def transcript_stats(path):
    p = Path(path)
    stats = {
        "path": str(p),
        "size": p.stat().st_size,
        "events": 0,
        "assistant_events": 0,
        "leak_events": 0,
        "last_leak_line": 0,
    }
    with p.open("r", encoding="utf-8", errors="replace") as f:
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


def write_report(session_id, stats):
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report = REPORT_DIR / f"{session_id or 'unknown'}-{int(time.time())}.json"
        report.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(report)
    except Exception:
        return ""


def additional_context(event, context):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }, ensure_ascii=False))


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def submitted_prompt(data):
    for key in ("prompt", "user_prompt", "message"):
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def is_recovery_command(prompt):
    lower = prompt.lower()
    return any(lower.startswith(command) for command in RECOVERY_COMMANDS)


def main():
    if os.environ.get("CLAUDE_CODE_BAD_SESSION_GUARD", "1") in ("0", "false", "False"):
        return

    data = read_input()
    event = data.get("hook_event_name") or "UserPromptSubmit"
    if event not in ("SessionStart", "UserPromptSubmit"):
        return

    if event == "UserPromptSubmit" and is_recovery_command(submitted_prompt(data)):
        return

    transcript_path = data.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        return

    max_bytes = env_int("CLAUDE_CODE_BAD_SESSION_MAX_BYTES", DEFAULT_MAX_BYTES)
    max_leaks = env_int("CLAUDE_CODE_BAD_SESSION_MAX_LEAK_EVENTS", DEFAULT_MAX_LEAK_EVENTS)
    try:
        size = os.path.getsize(transcript_path)
    except Exception:
        return
    if size < max_bytes:
        return

    try:
        stats = transcript_stats(transcript_path)
    except Exception:
        return
    if stats["leak_events"] < max_leaks:
        return

    session_id = data.get("session_id") or Path(transcript_path).stem
    report = write_report(session_id, stats)
    mb = stats["size"] / 1_000_000
    message = (
        "このClaude Codeセッションは隔離対象です。根本原因は、巨大化した同一transcript内に "
        "過去の壊れたXML/plain-text tool callが複数残っており、モデルがそれを再模倣することです。\n\n"
        f"session_id: {session_id}\n"
        f"transcript: {transcript_path}\n"
        f"size: {mb:.1f} MB\n"
        f"assistant leak events: {stats['leak_events']}\n"
        f"last leak line: {stats['last_leak_line']}\n"
        f"report: {report}\n\n"
        "このセッションをresumeし続ける限り、フックで救済しても再発率が残ります。"
        "復旧するなら idle 状態で `/compact` を試してください。"
        "それでも `court` / `<invoke>` が再発する場合は `/model` で Sonnet 系へ切り替えるか、"
        "`/clear` または新しいClaude Codeセッションへ逃がしてください。"
        "通常の作業プロンプトは、このsession_idでは続けないでください。"
    )

    if event == "SessionStart":
        additional_context(event, message)
        return
    block(message)


if __name__ == "__main__":
    main()
