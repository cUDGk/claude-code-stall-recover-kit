import json
import os
import sys
from pathlib import Path

CONTEXT = (
    "CRITICAL Claude Code tool-call guard: never write literal `call`, "
    "`<invoke ...>`, `<parameter ...>`, `antml:invoke`, or "
    "`antml:function_calls` in assistant text. If a tool is needed, emit a real "
    "Claude Code tool_use only. Keep Bash calls short and ASCII. Put complex, "
    "multi-step, quoted, or Japanese content into a file first, then run one "
    "short command. One tool call per assistant step."
)

LONG_SESSION_CONTEXT = (
    "Long-session tool-call guard is active. This transcript is large enough "
    "that legacy XML tool-call leakage has occurred before. Use only native "
    "tool_use blocks; never print call/<invoke> markup as text."
)

STATE_PATH = Path.home() / ".claude" / "hooks" / "tool_call_guard_state.json"


def read_input():
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        raw = ""
    try:
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def additional_context(event, context):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }))


def transcript_size(data):
    path = data.get("transcript_path")
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def main():
    data = read_input()
    event = data.get("hook_event_name") or "UserPromptSubmit"
    session = data.get("session_id") or "unknown"

    if event in ("SessionStart", "UserPromptSubmit"):
        additional_context(event, CONTEXT)
        return

    if event == "PostToolBatch":
        # Avoid bloating normal sessions. The failure correlates with very large
        # transcripts, so only reinforce once every 10 tool batches once the
        # transcript is already large.
        if transcript_size(data) < 2_000_000:
            return
        state = load_state()
        key = f"{session}:post_tool_batch"
        count = int(state.get(key, 0)) + 1
        state[key] = count
        save_state(state)
        if count == 1 or count % 10 == 0:
            additional_context(event, LONG_SESSION_CONTEXT)


if __name__ == "__main__":
    main()
