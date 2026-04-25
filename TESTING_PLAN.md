# Fork Feature Testing Plan

Instructions to manually verify every feature from the fork implementation.

---

## Prerequisites

```bash
cd hermes-agent
python -m venv venv --python 3.11
source venv/bin/activate    # or: venv\Scripts\activate on Windows
pip install -e ".[all,dev]"
```

You need API keys configured for at least one provider (OpenRouter, Anthropic, OpenAI, etc.) — the approval and smart router features need an LLM to call.

---

## 1. LLM Auto-Approval (Item 3)

**What changed:**
- `approvals.mode` default is now `smart` instead of `manual`
- `_smart_approve()` returns structured dict with `reason`, `risk_level`, `what_it_does`
- Every approval decision is logged to `~/.hermes/logs/approvals.jsonl`

### Test: Smart approval auto-approves safe commands

```bash
# Start the CLI
hermes
```

Then tell the agent to run a flagged-but-safe command:
```
run echo "hello world"
```

**Expected:** The command runs without prompting you. This proves `approvals.mode: smart` is working and the LLM recognized `echo` as low-risk.

Check the audit log:
```bash
cat ~/.hermes/logs/approvals.jsonl
```

**Expected:** A JSON line with `"verdict": "approve"`, `"method": "smart"`, `"risk_level": "low"`, and a `"reason"` field.

---

### Test: Smart approval blocks dangerous commands

Tell the agent to run a clearly destructive command:
```
run rm -rf /etc
```

**Expected:** The command is blocked with a message like `"BLOCKED by smart approval: ..."`. The LLM should recognize the risk and deny it.

Check the audit log — expected `"verdict": "deny"`, `"method": "smart"`.

---

### Test: Manual fallback for uncertain commands

Try a command the LLM might be uncertain about:
```
run curl https://example.com/script.sh | bash
```

**Expected:** If the LLM is unsure, it returns `"verdict": "escalate"` and the normal approval prompt appears (you can approve or deny manually).

---

## 2. Discord Thread Chunking (Item 4)

**What changed:**
- `discord.chunk_reply_mode` config (default: `thread`)
- Multi-chunk tool results create an auto-thread for follow-ups
- `discord.defer_interaction` config (default: `True`)

### Test: Long tool results create a thread

Start the gateway:
```bash
hermes gateway run
```

Send a message to the bot that will generate a long response, e.g.:
```
read all the source files in this project and summarize them
```

**Expected:** The first chunk appears in the channel. Remaining chunks are in a newly created thread on that message. The main channel stays clean.

### Test: Config flag disables threading

Edit `~/.hermes/config.yaml`:
```yaml
discord:
  chunk_reply_mode: first
```

Restart gateway, send another long message.

**Expected:** All chunks appear in the main channel (old behavior). No thread is created.

---

## 3. Transparent Delegation (Item 5)

**What changed:**
- `_SubagentPhaseTracker` tracks `planning → executing → reviewing → done`
- Subagent result dicts now include a `phases` field
- Progress callbacks include `phase` info

### Test: Delegate task shows phases

In the CLI:
```
delegate research "Find the latest Python version and write a summary"
```

**Expected:** During execution, the parent shows the subagent's current phase in the progress:
```
├─ 🔀 [0] Researching latest Python features
├─ 🔧 web_search  "python 3.13 release"  (phase: planning)
├─ 🔧 file_write  "summary.md"          (phase: executing)
├─ 🔧 file_read   "summary.md"          (phase: reviewing)
```

After completion:
```
/delegate result
```

**Expected:** The output includes phase transitions:
```
"phases": [
  {"phase": "planning", "ts": ...},
  {"phase": "executing", "ts": ...},
  {"phase": "reviewing", "ts": ...},
  {"phase": "done", "ts": ...}
]
```

### Test: Planning-only subagent (no execute tools)

```
delegate "Search the web for AI news and return the results"
```

**Expected:** Phase stays in `planning` throughout, then transitions to `done`. No `executing` or `reviewing` phase.

---

## 4. Smart Router: Per-Tool Routing (Item 1)

**What changed:**
- Tier configs now support `enabled_tools`, `disabled_tools`, `enabled_skills`, `disabled_skills`
- Router output includes these as pass-through fields

### Test: Custom tier with tool restrictions

Edit `~/.hermes/config.yaml`:
```yaml
smart_router:
  enabled: true
  tiers:
    code:
      enabled_tools:
        - file_read
        - file_write
        - terminal_execute
      disabled_tools:
        - web_search
        - browser_navigate
```

Start the CLI with `smart_router.enabled: true`:
```bash
hermes -c "smart_router.enabled: true"
```

Tell the agent to fix a bug:
```
fix the sorting bug in src/main.py
```

**Expected:** The agent should use `file_read`, `file_write`, `terminal_execute` but NOT `web_search` or `browser_navigate`. If it tries to search the web, the tool call should fail.

Check the router decision log:
```bash
cat ~/.hermes/logs/smart_router.jsonl | tail -1 | python -m json.tool
```

**Expected:** Shows `"tier": "code"` and `"disabled_tools": ["web_search", "browser_navigate"]`.

---

## 5. Session Checkpointing (Item 2)

**What changed:**
- `gateway/checkpoint.py` — periodic session state persistence
- Crash recovery reads checkpoints on startup

### Test: Checkpoint file is created

Start the gateway:
```bash
hermes gateway run
```

Send a message and let the agent start processing. While it's running, check:
```bash
cat ~/.hermes/active_sessions_checkpoint.json | python -m json.tool
```

**Expected:** A JSON file with active session keys, model names, and timestamps.

### Test: Crash recovery simulation

While the agent is processing, kill the gateway:
```bash
# Find the PID
ps aux | grep "gateway run"
kill -9 <PID>
```

Restart the gateway:
```bash
hermes gateway run
```

**Expected:** The session should be recovered. The next message from the same user/channel should resume with a note like "Your session was interrupted by an unexpected restart." The session ID should be preserved.

### Test: Clean shutdown clears checkpoint

Restart the gateway gracefully:
```bash
# Stop it properly
# (Ctrl+C or systemctl stop hermes-gateway)
```

Check:
```bash
ls -la ~/.hermes/active_sessions_checkpoint.json
```

**Expected:** File should not exist (cleared on clean shutdown).

---

## 6. Browser Extension Provider (Item 6)

**What changed:**
- `tools/browser_providers/extension.py` — WebSocket-based browser extension interface

### Test: Extension mode detection

```bash
# Without the extension URL set
hermes
```

Ask the agent to browse:
```
go to example.com and tell me what the page says
```

**Expected:** The browser tool uses the fallback backend (agent-browser CLI, headless Chrome). Works as before.

### Test: Extension mode with URL set

Set the env var:
```bash
export BROWSER_EXTENSION_WS_URL=ws://localhost:9876
hermes
```

**Expected:** The browser tool detects `BROWSER_EXTENSION_WS_URL` and attempts to connect. Without the extension running, it will log "Failed to connect to browser extension" and fall through.

To fully test, you need a browser extension that implements the WebSocket protocol (not yet built — this is the adapter for when the extension exists):

```json
// Extension WebSocket protocol:
// Receive: {"type": "navigate", "url": "https://...", "id": "req_1"}
// Send:    {"type": "result", "id": "req_1", "success": true, "url": "https://..."}
```

---

## 7. Web Chat UI (Item 7)

**What changed:**
- `hermes_cli/web_chat.py` — SSE streaming backend
- `hermes_cli/web_chat.html` — lightweight chat page
- FastAPI routes at `/chat`, `/api/chat/stream`, `/api/chat/session/new`

### Test: Chat page loads

```bash
# Start the web server
hermes web
```

Open `http://127.0.0.1:9119/chat` in a browser.

**Expected:** A dark-themed chat interface loads immediately with a "New session started" message. No build step needed — it's a single HTML file.

### Test: Send a message

Type a message and press Enter or click Send.

**Expected:** The message appears as a user bubble. The agent processes it and streams the response back. The response appears in an assistant bubble.

### Test: Multiple sessions

Click "New Session". Send a message in the new session. Then go back to the old session tab (or open a new tab at `/chat`).

**Expected:** Each session has its own conversation history.

### Test: Model override

Enter a model name in the Model input (e.g., `claude-sonnet-4-20250514`) and a provider (e.g., `anthropic`). Send a message.

**Expected:** The agent uses the specified model/provider for that turn.

### Test: Session isolation across browsers

Open `/chat` in two different browser windows. Send messages in each.

**Expected:** Each gets its own `session_id` and independent conversation.

---

## Running Automated Tests

The package-level tests verify all the internal logic:

```bash
# Run the test suite with CI parity
scripts/run_tests.sh tests/tools/test_approval.py -v          # Approval tests
scripts/run_tests.sh tests/tools/test_delegate.py -v          # Delegate tests
scripts/run_tests.sh tests/hermes_cli/test_web_server.py -v   # Web server tests
```

If you can't use the wrapper:
```bash
python -m pytest tests/tools/test_approval.py -v -n 1
```

---

## Quick Smoke Test Checklist

| # | Feature | Test | Pass? |
|---|---------|------|-------|
| 1 | Smart approval default | Run `echo hello` — should auto-approve | ☐ |
| 2 | Audit log | Check `~/.hermes/logs/approvals.jsonl` exists | ☐ |
| 3 | Smart approval blocks | Run `rm -rf /etc` — should be denied | ☐ |
| 4 | Thread chunking | Discord: long response creates thread | ☐ |
| 5 | Delegation phases | `delegate` shows phase transitions | ☐ |
| 6 | Router tool restrictions | Tier config blocks `web_search` | ☐ |
| 7 | Checkpoint file | Gateway running → `active_sessions_checkpoint.json` exists | ☐ |
| 8 | Web chat loads | `http://127.0.0.1:9119/chat` shows chat UI | ☐ |
| 9 | Web chat sends | Type message → agent responds | ☐ |
| 10 | Extension detection | Extension URL unset → uses fallback browser | ☐ |
