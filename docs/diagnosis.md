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

- 3337 total events after continued observation
- about 8.1 MB of transcript JSONL
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

This is a known upstream failure class. Public reports describe the same
signature:

- `call` / `<invoke name="Bash">` rendered as assistant text rather than a
  structured `tool_use`.
- `court` before `<invoke>`, followed by `stop_reason=end_turn` and no
  executable tool call.
- Recurrence in long, tool-heavy sessions after the leaked markup is already in
  context.

Useful references:

- https://github.com/anthropics/claude-code/issues/61221
- https://github.com/anthropics/claude-code/issues/63870
- https://github.com/anthropics/claude-code/issues/65705

On 2026-06-20, local Claude Code was already current:

```text
claude --version -> 2.1.183 (Claude Code)
claude update    -> Claude Code is up to date (2.1.183)
npm latest       -> @anthropic-ai/claude-code 2.1.183
```

So a normal update was not available as a fix at that time.

The first recovery hook only blocked the stop and told Claude to retry. That
helped once, but if Claude repeated the same malformed XML on the continuation,
the loop could still stall or hit the Stop-hook block cap.

## Fix

The current fix has two layers:

1. `tool_call_guard.py` injects compact guidance at session start, prompt
   submission, and periodically in large sessions.
2. `stall_recover.py` detects leaked XML at Stop time and handles it:
   - auto-executes safe built-in file and Bash operations where possible;
   - handles `mcp__win-control__clipboard_set/get` with Windows clipboard API;
   - absorbs unsupported MCP leaks so they do not become hard failures;
   - absorbs Task-tool leaks as progress-only bookkeeping;
   - deduplicates leaked calls so repeated malformed output is not executed
     twice;
   - feeds the result back to Claude so it can continue from the actual state.
3. `session_health_guard.py` blocks continued use of transcripts that are both
   large and already contaminated with many leaked tool-call examples.

The Stop-hook block cap is also raised with:

```json
{
  "env": {
    "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "64",
    "CLAUDE_CODE_BAD_SESSION_GUARD": "1",
    "CLAUDE_CODE_BAD_SESSION_MAX_BYTES": "6000000",
    "CLAUDE_CODE_BAD_SESSION_MAX_LEAK_EVENTS": "8"
  }
}
```

## Remaining Risk

No hook can make the model's native tool generation mathematically impossible to
break. This setup reduces the chance of the bad output and recovers from common
leaks when they happen. The best prevention is still to start fresh sessions
before the transcript becomes huge and keep tool inputs short.

Observed public workarounds:

1. `/compact` can fix contaminated long sessions by replacing verbatim leaked
   tool-call examples with a summary. It must be run while idle.
2. In reports tied to Opus 4.8/1M and large always-loaded context, switching to
   a Sonnet-family model fixed the `court`/XML leak immediately.
3. `/clear` or a fresh session is the fallback when compaction or model switch
   does not stabilize the session.

`session_health_guard.py` blocks normal work prompts in contaminated sessions
but deliberately allows `/compact`, `/clear`, `/rewind`, and `/model`, so the
operator can apply recovery commands.
