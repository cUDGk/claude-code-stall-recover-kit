import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STALL_HOOK = ROOT / "hooks" / "stall_recover.py"
GUARD_HOOK = ROOT / "hooks" / "tool_call_guard.py"


def run_hook(script, payload):
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc


def make_transcript(text, uuid="test-message"):
    tmp = Path(tempfile.mkdtemp(prefix="stall-hook-test-"))
    transcript = tmp / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": uuid,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return tmp, transcript


def test_bash_leak():
    text = """call
<invoke name="Bash">
<parameter name="command">echo hook_ok</parameter>
<parameter name="timeout">10000</parameter>
</invoke>"""
    tmp, transcript = make_transcript(text, "bash-1")
    proc = run_hook(
        STALL_HOOK,
        {"hook_event_name": "Stop", "transcript_path": str(transcript), "session_id": "test"},
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["decision"] == "block"
    assert "hook_ok" in data["reason"]
    return tmp


def test_write_leak():
    tmp = Path(tempfile.mkdtemp(prefix="stall-hook-write-"))
    out = tmp / "out.txt"
    text = f"""call
<invoke name="Write">
<parameter name="file_path">{out.as_posix()}</parameter>
<parameter name="content">hello from hook</parameter>
</invoke>"""
    _, transcript = make_transcript(text, "write-1")
    proc = run_hook(
        STALL_HOOK,
        {"hook_event_name": "Stop", "transcript_path": str(transcript), "session_id": "test"},
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == "hello from hook"
    return tmp


def test_task_absorb():
    text = """call
<invoke name="TaskUpdate">
<parameter name="taskId">18</parameter>
<parameter name="status">completed</parameter>
<parameter name="description">done</parameter>
</invoke>"""
    tmp, transcript = make_transcript(text, "task-1")
    proc = run_hook(
        STALL_HOOK,
        {"hook_event_name": "Stop", "transcript_path": str(transcript), "session_id": "test"},
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert "absorbed" in data["reason"]
    return tmp


def test_guard_context():
    proc = run_hook(GUARD_HOOK, {"hook_event_name": "UserPromptSubmit", "session_id": "test"})
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "never write literal" in ctx


def main():
    state = Path.home() / ".claude" / "hooks" / "stall_recover_state.json"
    if state.exists():
        state.unlink()
    test_bash_leak()
    test_write_leak()
    test_task_absorb()
    test_guard_context()
    print("all hook tests passed")


if __name__ == "__main__":
    main()
