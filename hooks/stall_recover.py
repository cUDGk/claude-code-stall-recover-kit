import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
STATE_PATH = HOME / ".claude" / "hooks" / "stall_recover_state.json"
LOG_PATH = HOME / ".claude" / "hooks" / "stall_recover.log"
MAX_REASON_CHARS = 12000
MAX_BASH_SECONDS = 90
MARKERS = ("<invoke name=", "<parameter name=", "antml:invoke", "antml:function_calls")


def log(message):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except Exception:
        pass


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": {}}


def save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log(f"state write failed: {exc!r}")


def read_stdin_json():
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        raw = ""
    try:
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def last_assistant_text(transcript_path):
    if not transcript_path or not os.path.exists(transcript_path):
        return "", ""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        log(f"transcript read failed: {exc!r}")
        return "", ""

    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        chunks = []
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(block.get("text", ""))
        return "\n".join(chunks), ev.get("uuid", "")
    return "", ""


def parse_invocations(text):
    calls = []
    pattern = re.compile(
        r"<invoke\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</invoke>",
        re.IGNORECASE | re.DOTALL,
    )
    param_pattern = re.compile(
        r"<parameter\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</parameter>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        name = html.unescape(match.group(1)).strip()
        body = match.group(2)
        params = {}
        for p in param_pattern.finditer(body):
            params[html.unescape(p.group(1)).strip()] = html.unescape(p.group(2))
        calls.append({"name": name, "params": params, "raw": match.group(0)})
    return calls


def hash_call(uuid, call):
    # Deliberately ignore assistant message UUID. If the model repeats the same
    # leaked XML in the next continuation, it must not execute the command again.
    payload = json.dumps({"name": call["name"], "params": call["params"]}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:24]


def git_bash():
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "bin" / "bash.exe",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    found = shutil.which("bash")
    return found or ""


def parse_timeout(value):
    try:
        n = int(str(value).strip())
    except Exception:
        return 30
    if n > 1000:
        n = max(1, n // 1000)
    return max(1, min(n, MAX_BASH_SECONDS))


def run_bash(params):
    command = params.get("command", "")
    if not command.strip():
        return False, "Bash leak had no command."
    timeout = parse_timeout(params.get("timeout", 30000))
    shell = git_bash()
    if shell:
        argv = [shell, "-lc", command]
    else:
        argv = command
    try:
        proc = subprocess.run(
            argv,
            shell=not bool(shell),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        body = f"Auto-executed leaked Bash command. exit={proc.returncode}\n"
        body += f"command:\n{command}\n"
        if out:
            body += f"\nstdout:\n{out[-6000:]}"
        if err:
            body += f"\nstderr:\n{err[-3000:]}"
        return proc.returncode == 0, body
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return False, f"Leaked Bash command timed out after {timeout}s.\ncommand:\n{command}\nstdout:\n{out[-2000:]}\nstderr:\n{err[-2000:]}"
    except Exception as exc:
        return False, f"Failed to auto-execute leaked Bash command: {exc!r}\ncommand:\n{command}"


def read_file(params):
    path = params.get("file_path") or params.get("path")
    if not path:
        return False, "Read leak had no file_path."
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        return True, f"Auto-executed leaked Read for {path}.\ncontent:\n{content[-10000:]}"
    except Exception as exc:
        return False, f"Failed leaked Read for {path}: {exc!r}"


def write_file(params):
    path = params.get("file_path") or params.get("path")
    content = params.get("content")
    if not path:
        return False, "Write leak had no file_path."
    if content is None:
        return False, f"Write leak for {path} had no content."
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True, f"Auto-executed leaked Write for {path}. bytes={len(content.encode('utf-8'))}"
    except Exception as exc:
        return False, f"Failed leaked Write for {path}: {exc!r}"


def edit_file(params):
    path = params.get("file_path") or params.get("path")
    old = params.get("old_string")
    new = params.get("new_string")
    replace_all = str(params.get("replace_all", "false")).lower() == "true"
    if not path or old is None or new is None:
        return False, "Edit leak was missing file_path, old_string, or new_string."
    try:
        target = Path(path)
        content = target.read_text(encoding="utf-8", errors="replace")
        if old not in content:
            return False, f"Edit old_string not found in {path}."
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        target.write_text(updated, encoding="utf-8")
        return True, f"Auto-executed leaked Edit for {path}. replace_all={replace_all}"
    except Exception as exc:
        return False, f"Failed leaked Edit for {path}: {exc!r}"


def execute_call(call):
    name = call["name"]
    params = call["params"]
    if name == "Bash":
        return run_bash(params)
    if name == "Read":
        return read_file(params)
    if name == "Write":
        return write_file(params)
    if name == "Edit":
        return edit_file(params)
    if name in ("TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop"):
        return True, (
            f"Detected leaked {name} call. Hooks cannot invoke Claude Code's internal Task tool, "
            "so this progress-only call was absorbed to prevent a stall. Continue the actual user work "
            "without repeating this task XML. Captured params:\n"
            + json.dumps(params, ensure_ascii=False, indent=2)[:5000]
        )
    return False, f"Detected leaked {name} call, but auto-execution is not implemented for this tool. Params: {json.dumps(params, ensure_ascii=False)[:3000]}"


def block(reason):
    reason = reason[-MAX_REASON_CHARS:]
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def main():
    data = read_stdin_json()
    text, uuid = last_assistant_text(data.get("transcript_path"))
    if not text or not any(marker in text for marker in MARKERS):
        return

    calls = parse_invocations(text)
    if not calls:
        block("Detected leaked tool-call markers, but could not parse the call. Continue without repeating XML/plain-text tool syntax. Use a real tool call or answer the user.")
        return

    state = load_state()
    seen = state.setdefault("seen", {})
    feedback = []
    for call in calls[:3]:
        key = hash_call(uuid, call)
        item = seen.setdefault(key, {"count": 0, "executed": False, "last_result": ""})
        item["count"] += 1
        item["last_seen"] = time.time()

        if item.get("handled"):
            result = item.get("last_result", "")
            feedback.append(f"Repeated leaked {call['name']} call was already handled once. Do not repeat it. Previous result:\n{result}")
            continue

        ok, result = execute_call(call)
        item["handled"] = True
        item["success"] = ok
        item["last_result"] = result[-9000:]
        feedback.append(result)
        log(f"leak {call['name']} key={key} ok={ok} active={bool(data.get('stop_hook_active'))}")

    # Keep state small.
    if len(seen) > 80:
        newest = sorted(seen.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)[:80]
        state["seen"] = dict(newest)
    save_state(state)

    reason = (
        "Claude Code emitted a tool call as plain text instead of executing it. "
        "The Stop hook parsed the leaked call and handled it below. "
        "Continue from this result. Do NOT repeat XML/call/<invoke> text. "
        "If more work is needed, use a real tool call, or summarize the result.\n\n"
        + "\n\n---\n\n".join(feedback)
    )
    block(reason)


if __name__ == "__main__":
    main()
