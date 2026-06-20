import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import ctypes
from ctypes import wintypes
from pathlib import Path

HOME = Path.home()
STATE_PATH = HOME / ".claude" / "hooks" / "stall_recover_state.json"
LOG_PATH = HOME / ".claude" / "hooks" / "stall_recover.log"
MAX_REASON_CHARS = 12000
MAX_BASH_SECONDS = 90
MARKERS = ("<invoke name=", "<parameter name=", "antml:invoke", "antml:function_calls")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


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


def clipboard_set(text):
    if os.name != "nt":
        return False, "Clipboard fallback is only implemented for Windows."
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
    handle = None
    opened = False
    try:
        if not user32.OpenClipboard(None):
            return False, "OpenClipboard failed."
        opened = True
        if not user32.EmptyClipboard():
            return False, "EmptyClipboard failed."
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False, "GlobalAlloc failed."
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return False, "GlobalLock failed."
        try:
            buf = ctypes.create_unicode_buffer(text)
            ctypes.memmove(locked, ctypes.addressof(buf), size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False, "SetClipboardData failed."
        handle = None
        return True, f"Auto-executed leaked win-control clipboard_set. chars={len(text)}"
    except Exception as exc:
        return False, f"Failed leaked clipboard_set: {exc!r}"
    finally:
        if opened:
            user32.CloseClipboard()


def clipboard_get():
    if os.name != "nt":
        return False, "Clipboard fallback is only implemented for Windows."
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    CF_UNICODETEXT = 13
    opened = False
    try:
        if not user32.OpenClipboard(None):
            return False, "OpenClipboard failed."
        opened = True
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return True, "Auto-executed leaked win-control clipboard_get. Clipboard has no text."
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return False, "GlobalLock failed."
        try:
            text = ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(handle)
        return True, f"Auto-executed leaked win-control clipboard_get.\ncontent:\n{text[-10000:]}"
    except Exception as exc:
        return False, f"Failed leaked clipboard_get: {exc!r}"
    finally:
        if opened:
            user32.CloseClipboard()


def mcp_tool_parts(name):
    if not name.startswith("mcp__"):
        return None, None
    parts = name.split("__", 2)
    if len(parts) != 3:
        return "", name
    return parts[1], parts[2]


def handle_mcp_call(name, params):
    server, tool = mcp_tool_parts(name)
    if server == "win-control" and tool == "clipboard_set":
        text = params.get("text")
        if text is None:
            text = params.get("content")
        if text is None:
            return False, "Leaked win-control clipboard_set had no text/content parameter."
        return clipboard_set(str(text))
    if server == "win-control" and tool == "clipboard_get":
        return clipboard_get()
    return True, (
        f"Detected leaked MCP tool `{name}` and absorbed it so the turn does not stall. "
        "Stop hooks cannot invoke MCP servers directly. Do not repeat the XML/plain-text MCP call. "
        "On the next continuation, either use the real native MCP tool_use for this action, use a "
        "regular Bash/Read/Write/Edit fallback if that can complete the user request, or explain the "
        "current result briefly. Captured params:\n"
        + json.dumps(params, ensure_ascii=False, indent=2)[:5000]
    )


def execute_call(call):
    name = call["name"]
    params = call["params"]
    if name.startswith("mcp__"):
        return handle_mcp_call(name, params)
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
    return True, (
        f"Detected leaked unsupported tool `{name}` and absorbed it so the turn does not stall. "
        "Do not repeat XML/plain-text tool syntax. Use a real tool call if the action is still needed. "
        "Captured params:\n"
        + json.dumps(params, ensure_ascii=False, indent=2)[:5000]
    )


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
