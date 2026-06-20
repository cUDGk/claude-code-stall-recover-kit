# claude-code-stall-recover-kit

Claude Code can sometimes leak a legacy XML-style tool call such as
`call` / `<invoke name="Bash">` into assistant text instead of emitting a real
tool call. When that happens, the command is not executed and the turn appears
to stall.

This repository packages a defensive hook setup for that failure mode.

## What It Does

- Adds a Stop hook that detects leaked XML tool calls.
- Auto-handles leaked `Bash`, `Read`, `Write`, and `Edit` calls where possible.
- Handles leaked `mcp__win-control__clipboard_set/get` with the Windows
  clipboard API, and absorbs other leaked MCP calls without turning them into a
  hard failure.
- Absorbs leaked Task-tool calls such as `TaskUpdate` so progress logging does
  not trap the session in a loop.
- Prevents duplicate execution when the same leaked command is repeated.
- Injects a short guard at session start and user prompt submission to reduce
  the chance that the model prints XML tool syntax in the first place.
- Blocks continued use of large, already-contaminated transcripts so a resumed
  bad session does not keep teaching the model the broken XML pattern.
- Raises the Stop-hook block cap to reduce premature override in long sessions.

## Files

- `hooks/stall_recover.py` - Stop hook that parses and handles leaked calls.
- `hooks/tool_call_guard.py` - Session/UserPrompt/PostToolBatch context guard.
- `hooks/session_health_guard.py` - Blocks risky long sessions before another
  prompt is submitted.
- `tools/audit_sessions.py` - Audits `~/.claude/projects` for leaked tool-call
  history and can quarantine stopped sessions.
- `settings.snippet.json` - Minimal Claude Code settings block to install.
- `install.ps1` - Copies hooks into `~/.claude/hooks` and merges settings.
- `tests/test_hooks.py` - Local smoke tests for the hooks.
- `docs/diagnosis.md` - Root-cause notes from the failing session.

## Install

From this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Restart Claude Code or run `/hooks` to confirm that the new hooks are loaded.
Environment variable changes are guaranteed after the next Claude Code launch.

## Test

```powershell
python .\tests\test_hooks.py
```

The test creates temporary transcript files, simulates leaked tool calls, and
checks that the Stop hook handles them.

## Audit and Quarantine

List risky transcripts:

```powershell
python .\tools\audit_sessions.py
```

Quarantine a stopped session:

```powershell
python .\tools\audit_sessions.py --quarantine 3bac9d8e-9ec4-4cdf-98c7-1be6d5ac4069
```

Active or busy sessions are not moved by default. Exit Claude Code first.

## Notes

This is a guardrail, not a replacement for keeping sessions healthy. The most
reliable prevention is still:

- start a new session before transcripts become huge;
- do not resume a session after repeated raw `<invoke>` leaks;
- keep Bash commands short;
- avoid command chains with `;` or `&&`;
- write complex or Japanese-heavy content into files first;
- never include `call` / `<invoke>` examples in assistant-facing instructions.
