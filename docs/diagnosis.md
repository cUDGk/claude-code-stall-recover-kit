# Diagnosis

The failing case was a long-running local Claude Code session.

Transcript path (generic form):

`~/.claude/projects/<project>/<session-id>.jsonl`

## Symptom

Claude Code sometimes ended a turn after printing a legacy XML-style tool call
as normal assistant text:

```text
call
<invoke name="Bash">
<parameter name="command">...</parameter>
</invoke>
```

Because this was plain text, Claude Code did not execute the tool call. The
session looked like it had stalled.

## Observed Evidence

The transcript contained:

- 3110 total events
- 1257 assistant messages
- 588 user messages
- about 7.3 MB of transcript JSONL
- cache read usage near 981k tokens
- 34 malformed XML-style tool-call leaks

The leaks became common in very long turns with:

- Japanese-heavy text;
- nested quotes;
- long Bash commands;
- heredocs;
- chained commands with `;` or `&&`;
- `TaskUpdate` payloads with long descriptions;
- old XML tool-call examples already present in the transcript.

## Root Cause

The model was not blocked by a running process or a permission prompt. It was
generating the wrong output format. Instead of a native Claude Code `tool_use`
block, it copied legacy XML tool-call syntax into assistant text.

The first recovery hook only blocked the stop and told Claude to retry. That
helped once, but if Claude repeated the same malformed XML on the continuation,
the loop could still stall or hit the Stop-hook block cap.

## Fix

The current fix has two layers:

1. `tool_call_guard.py` injects compact guidance at session start, prompt
   submission, and periodically in large sessions.
2. `stall_recover.py` detects leaked XML at Stop time and handles it:
   - auto-executes safe built-in file and Bash operations where possible;
   - absorbs Task-tool leaks as progress-only bookkeeping;
   - deduplicates leaked calls so repeated malformed output is not executed
     twice;
   - feeds the result back to Claude so it can continue from the actual state.

The Stop-hook block cap is also raised with:

```json
{
  "env": {
    "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "64"
  }
}
```

## Remaining Risk

No hook can make the model's native tool generation mathematically impossible to
break. This setup reduces the chance of the bad output and recovers from common
leaks when they happen. The best prevention is still to start fresh sessions
before the transcript becomes huge and keep tool inputs short.

