# Hermes Fork — Additional Feature Ideas

Brainstormed features organized by area, with implementation notes and file-level change estimates.

---

## Agent Capabilities

### 1. Persistent Long-Term Memory (Built-in)

**Problem:** Existing memory providers (mem0, supermemory, honcho, etc.) are bolted-on plugins with inconsistent quality. The default experience is basically stateless — agent forgets everything between sessions.

**Solution:** A built-in vector memory using SQLite + a local embedding model (sentence-transformers or an API). Each session auto-summarizes key facts and stores them. On session start, retrieve relevant memories via RAG. Sits alongside existing plugin providers — falls through to them on failure.

```python
# Pseudocode for retrieval on session start
def load_relevant_memories(user_message: str, n: int = 5) -> list[str]:
    query_embedding = embed(user_message)
    return vector_search(query_embedding, top_k=n)
    # → injected into system prompt as "Relevant past knowledge: ..."
```

- **New files:** `agent/vector_memory.py`, `agent/embedding_service.py`
- **Modified files:** `agent/memory_manager.py` (router to built-in vs plugin), `hermes_state.py` (vector store table or file)
- **Config keys:** `memory.builtin.enabled`, `memory.builtin.embedding_model`, `memory.builtin.top_k`

---

### 2. Multi-Agent Collaboration (Peer-to-Peer)

**Problem:** Delegation is strictly hierarchical (parent spawns child, waits, gets result). Two agents can't work on a shared task simultaneously or communicate directly.

**Solution:** A shared message bus. Agents subscribe to topics, publish results, ask for help. New tools: `send_message_to_agent(agent_id, message)`, `listen_for_agents(topic, timeout)`, `broadcast_to_agents(topic, message)`.

```
          ┌──────────┐  topic: "research-123"  ┌──────────┐
          │ Agent A  │◄──────────────────────►│ Agent B  │
          │ (coder)  │                         │ (search) │
          └────┬─────┘                         └────┬─────┘
               │                                    │
               └────────── Message Bus ─────────────┘
                          (SQLite / Redis pub-sub)
```

- **New files:** `tools/agent_bus.py`, `tools/agent_collab_tool.py`
- **Modified files:** `toolsets.py` (new `collaboration` toolset), `run_agent.py` (bus registration in agent lifecycle)
- **Config keys:** `collaboration.enabled`, `collaboration.bus_backend` (sqlite / redis / file)

---

### 3. Knowledge Base Ingestion

**Problem:** The agent can web search and read docs, but can't build a persistent knowledge base from project-specific sources (internal docs, private repos, PDFs).

**Solution:** Two new tools:
- `kb_ingest(path|url, name)` — scrapes docs, code repos, or PDFs, chunks them, embeds them, stores in local vector DB
- `kb_query(query, name)` — retrieves relevant chunks, injects into context

```yaml
# Config
knowledge_base:
  storage_dir: "~/.hermes/knowledge_bases/"  # per profile
  embedding_model: "sentence-transformers/all-MiniLM-L6-v2"
  chunk_size: 1000
  chunk_overlap: 200
```

Per-project namespacing via the `name` parameter. Each KB is a separate SQLite table or file.

- **New files:** `tools/knowledge_base.py`, `agent/kb_manager.py`, `agent/kb_chunker.py`
- **Modified files:** `toolsets.py` (new `knowledge_base` toolset)
- **Config keys:** `knowledge_base.*` as above

---

### 4. Agent Workspaces

**Problem:** Terminal sessions are ephemeral. No persistent project directory with state tracking, git history, or long-running processes that survive beyond a turn.

**Solution:** A workspace manager:
- `workspace create <name> [--template python|node|go]` — creates project dir with git init
- `workspace open <name>` — sets `terminal.cwd` to workspace root
- `workspace close <name>` — saves terminal state, commits any changes
- `workspace status` — shows all workspaces with file change counts

```python
class Workspace:
    name: str
    path: Path
    git_branch: str
    last_active: datetime
    file_state: dict[str, FileChange]  # tracked via file_state.py
```

Implemented as a new environment backend in `tools/environments/`.

- **New files:** `tools/workspace_tool.py`, `tools/environments/workspace_env.py`
- **Modified files:** `toolsets.py` (new `workspace` toolset), `hermes_cli/commands.py` (workspace slash commands if needed)
- **Config keys:** `workspaces.dir` (default: `~/.hermes/workspaces/`), `workspaces.max_workspaces`

---

### 5. Automated Code Review Agent

**Problem:** Hermes can write code but has no structured code review mode. No way to say "review this PR and give me line-level feedback."

**Solution:** `codereview <pr_url|diff>` tool:
1. Fetches the diff via GitHub API or reads a raw diff
2. Runs static analysis (ruff, mypy, pyright — currently excluded; this would need to include them)
3. Sends the diff + lint results to a review model
4. Returns a structured review: issues by severity, suggested fixes, code quality score

```python
# CLI usage
/codereview https://github.com/user/repo/pull/42
# OR pipeline integration
hermes codereview --diff <(git diff main...HEAD)
```

- **New files:** `tools/code_review_tool.py`
- **Modified files:** `toolsets.py`, `pyproject.toml` (may need to actually use ruff if you want it)
- **Config keys:** `code_review.enabled_linters`, `code_review.required_checks`

---

## Observability & UX

### 6. Session Replay & Debugging

**Problem:** You can browse session logs but can't replay what happened — step through tool calls, see what the agent saw at each decision point, inspect the full context.

**Solution:** A web-based replay viewer that reads from SessionDB. Timeline view with expandable details for each step. Shows: message content, tool call arguments, tool results, token counts, reasoning, full context at that point.

```
Timeline:
├─ User: "Fix the login bug"
├─ Assistant (reasoning: "Let me find the auth code first")
├─ 🔧 tool_call: file_read → src/auth/login.py  [0.3s, 342 tokens]
│  └─ result: "def login(request): ..."
├─ 🔧 tool_call: grep → "auth" in src/            [1.2s, 0 tokens]
│  └─ result: "src/auth/login.py, src/auth/session.py"
├─ Assistant: "I found the issue..."
```

- **New files:** `web/src/pages/ReplayPage.tsx`
- **Modified files:** `hermes_cli/main.py` (API endpoint: `GET /api/sessions/<id>/replay`), `hermes_state.py` (replay data query), `web/src/App.tsx` (nav entry)
- **API effort:** Small — SessionDB already has all the data. Just need to format it for the timeline.

---

### 7. Live Agent Dashboard (Hardware Dashboard)

**Problem:** No single view of what's happening across all platforms at once. Active sessions, queue depth, ongoing tool calls, approval requests.

**Solution:** Real-time dashboard (SSE from the gateway to the web UI):

```
┌─────────────────────────────────────────────────┐
│  Hermes Live Dashboard                    ⚡ 3  │
├──────────────────┬──────────────────────────────┤
│  Active Sessions │  System                      │
│  ┌──────────────┐│  Model: Claude Sonnet 4      │
│  │ Discord: 2   ││  Tokens/min: 1,200           │
│  │ Telegram: 1  ││  Cost/hr: $0.23              │
│  │ CLI: 0       ││  Queue: 0 pending            │
│  └──────────────┘│  Uptime: 12h 34m             │
│                  │                              │
│  Recent Activity │  Pending Approvals           │
│  12:34 file_read │  ⚠️ rm -rf /tmp/build/      │
│  12:33 web_search│  ⚠️ sudo systemctl restart  │
│  12:32 assistant │                              │
└──────────────────┴──────────────────────────────┘
```

- **New files:** `web/src/pages/LiveDashboard.tsx`
- **Modified files:** `gateway/run.py` (metrics collector + SSE endpoint), `hermes_cli/main.py` (proxy SSE to web), `web/src/App.tsx`

---

### 8. Tool Call Timeline (CLI)

**Problem:** Tool calls flash by in the spinner. No way to review the sequence after the fact.

**Solution:** `/timeline` slash command. Shows every tool call in the current session with duration, input/output token counts, status, and result preview.

```
Session: 20260425_123456_abc123
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 1  web_search     "latest python 3.13 features"    0.8s  200t → 1,200t  ✓
 2  file_read      src/main.py                       0.3s    0t →   450t  ✓
 3  terminal_exec  python -m pytest tests/           5.2s    0t → 8,000t  ✓
 4  file_write     src/new_feature.py                 0.1s  300t →     0t  ✓
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 4 calls · 6.4s · 500 input → 9,650 output tokens · $0.042
```

- **Modified files:** `cli.py` (handler), `hermes_cli/commands.py` (CommandDef), `hermes_state.py` (expose tool_trace query)
- **Config keys:** None — pure UX addition.

---

### 9. Live System Prompt Viewer

**Problem:** The system prompt is assembled from config + personality + skills + context files + memory + active tool schemas. No easy way to see what the agent actually sees.

**Solution:** `/sysprompt` slash command. Prints the fully resolved system prompt. `AIAgent` stores the final assembled system prompt after all injection phases.

- **Modified files:** `run_agent.py` (store `self._system_prompt` after assembly), `cli.py` (handler for `/sysprompt`), `hermes_cli/commands.py` (CommandDef)
- **Config keys:** None.

---

## Operations & Cost

### 10. Per-Session & Per-Model Cost Budgeting

**Problem:** No automatic cost control. The agent can burn through API budget without warning — one runaway research session can cost $5+.

**Solution:** Configurable budgets enforced in the main agent loop:

```yaml
budgets:
  session:
    max_cost: 1.00        # interrupt agent if session exceeds $1
    max_tokens_input: 500_000
    max_tokens_output: 100_000
    max_api_calls: 200
  daily:
    max_cost: 10.00       # interrupt all sessions if daily total exceeds $10
  hourly:
    max_cost: 2.00
```

SessionDB already tracks token counts and cost per session. Add a per-day aggregate to a new `daily_usage` table. Check in `run_conversation()` after each API call.

- **New files:** `agent/budget_tracker.py`
- **Modified files:** `run_agent.py` (budget check in main loop), `hermes_cli/config.py` (add `budgets.*` keys), `hermes_state.py` (new `daily_usage` table or file)
- **Enforcement types:**
  - `"warn"` — log a warning, continue
  - `"interrupt"` — kill the current turn, return partial result
  - `"block"` — refuse to start new sessions until reset

---

### 11. Intelligent Provider Routing Based on Stats

**Problem:** The smart router picks a tier with a configured model/provider but never learns from actual performance. A slow/expensive model keeps getting used.

**Solution:** Track per-model metrics locally:

```python
# ~/.hermes/provider_stats.db
model_provider_stats = {
    "claude-sonnet-4/anthropic": {
        "avg_latency": 3.2,        # seconds
        "avg_cost_per_call": 0.008, # dollars
        "error_rate": 0.02,
        "success_rate": 0.98,
        "total_calls": 1542,
        "avg_output_tokens": 420,
    }
}
```

Smart router consults these stats when resolving tiers. E.g., for the "research" tier, if two models are configured, prefer the one with lower latency if cost is within budget.

- **New files:** `agent/provider_stats.py`
- **Modified files:** `agent/smart_router.py` (stat-aware tier resolution), `hermes_state.py` (new `provider_stats` table)
- **Config keys:** `smart_router.stat_aware.enabled`, `smart_router.stat_aware.prefer` (latency | cost | success_rate)

---

### 12. Approval Audit Log

**Problem:** No record of what commands were flagged, auto-approved, denied, or why. Hard to tune the approval system when you can't review past decisions.

**Solution:** Append-only JSONL log at `~/.hermes/logs/approvals.jsonl`:

```json
{"ts": 1714060800, "command": "rm -rf /tmp/build", "pattern": "recursive_delete", "verdict": "prompt_approved", "method": "user", "risk_level": "medium", "duration_s": 12.3}
{"ts": 1714060805, "command": "python -c \"print('hi')\"", "pattern": "python_c", "verdict": "approve", "method": "smart", "risk_level": "low", "reason": "Harmless one-liner"}
{"ts": 1714060810, "command": "sudo rm -rf /etc", "pattern": "sudo_recursive", "verdict": "deny", "method": "smart", "risk_level": "high", "reason": "Would delete system config"}
```

- **Modified files:** `tools/approval.py` only — one logger call at each decision point in `check_dangerous_command()`.
- **Config keys:** `approvals.audit_log.enabled`, `approvals.audit_log.max_entries`

---

### 13. Multi-Platform Budget Pooling

**Problem:** Cost budgets (#10) are per-session. A Discord session and a Telegram session each have their own budget, but they share an API key.
**Solution:** Budgets are per-profile, aggregated across all active platform sessions. Uses the daily/hourly tracking from #10.

- **Modified files:** `agent/budget_tracker.py` — aggregation across sessions via shared DB

---

## Platform-Specific

### 14. Multi-Channel Session Continuity

**Problem:** Sessions are per-platform. You can't start a conversation in Discord and continue it in Telegram. The agent has no memory of the conversation if you switch platforms.

**Solution:** Map all platforms to shared identities:

```yaml
# config.yaml
identity:
  linking:
    enabled: true
    method: pin           # "pin" | "link" | "manual"
  # User generates a PIN in one platform, enters it in another to link them
```

Implementation:
1. `SessionStore` resolves `(platform, chat_id)` → canonical `user_identity`
2. `user_identity` → `session_id` (shared across platforms)
3. Messages from any platform append to the same session
4. Agent sees the full cross-platform conversation history

```python
class SessionStore:
    def get_or_create_session(self, platform: str, chat_id: str) -> SessionEntry:
        uid = self._resolve_identity(platform, chat_id)  # NEW: cross-platform
        return self._get_or_create_by_identity(uid, platform, chat_id)
```

- **New files:** `gateway/identity_store.py` (identity linking logic)
- **Modified files:** `gateway/session.py` (identity mapping), `gateway/platforms/base.py` (pass identity context), `hermes_cli/config.py` (identity config)
- **Config keys:** `identity.linking.*` as above

---

### 15. Discord Voice Chat Enhancement

**Problem:** Discord voice support exists (join VC, play audio, listen) but is limited. No push-to-talk, no voice-activity-based turn taking, no STT transcription of user's speech.

**Solution:** Three improvements:
1. **Push-to-talk mode:** User presses a key in Discord, speaks, agent responds via TTS
2. **Voice activity detection (VAD):** Agent detects when user is speaking, processes naturally
3. **Real-time transcription:** User speech → Whisper/whisper.cpp → text → agent processes → edge-tts response

- **Modified files:** `gateway/platforms/discord.py` (voice connection enhancements)
- **Config keys:** `discord.voice.stt_model`, `discord.voice.vad_enabled`, `discord.voice.push_to_talk_key`

---

### 16. Scheduled Digest / Morning Briefing

**Problem:** Cron can run ad-hoc tasks but there's no structured daily briefing pattern. "Every morning at 8 AM, check my email, calendar, GitHub, and weather, then DM me a summary."

**Solution:** A `digest` toolset with configurable schedule and sources:

```yaml
digest:
  enabled: true
  morning:
    time: "08:00"
    timezone: "America/New_York"
    channel: "discord:123456789:thread"  # delivery target
    sources:
      - check_email(recent=5)
      - check_calendar(today)
      - github_notifications(unread=true)
      - weather(city="New York")
    prompt: "Summarize my morning in 3-5 bullet points. Be concise."
```

- **New files:** `tools/digest_tool.py`, `cron/digest_job.py`
- **Modified files:** `toolsets.py` (new `digest` toolset), `hermes_cli/commands.py` (CommandDef), `cron/jobs.py` (register digest job type)

---

## Developer & Integration

### 17. OpenCode / Cursor / Claude Desktop Integration (MCP)

**Problem:** Hermes runs as its own CLI/gateway. It doesn't integrate with IDE-based AI assistants like OpenCode, Cursor, or Claude Desktop.

**Solution:** Expose Hermes tools as an MCP server. Any MCP-compatible client (OpenCode, Cursor, Claude Desktop, VS Code via Continue) can use `hermes` tools:

- `web_search` — IDE agent can search the web
- `browser_navigate` — browse documentation
- `terminal_execute` — run commands in Hermes's managed terminal
- `delegate_task` — spawn a Hermes subagent from within the IDE

```python
# mcp_serve.py additions
@mcp.tool()
async def web_search(query: str) -> str:
    return await handle_function_call("web_search", {"query": query})

@mcp.tool()
async def terminal_execute(command: str) -> str:
    return await handle_function_call("terminal_execute", {"command": command})
```

- **Modified files:** `mcp_serve.py` (MCP tool registrations from Hermes's tool registry), `acp_adapter/` (may need ACP→MCP bridge)
- **Config keys:** `mcp.enabled`, `mcp.transport` (stdio | sse)

---

### 18. Sandboxed Code Execution with Dev Containers

**Problem:** `execute_code` runs raw Python in the Hermes process. No isolation, no resource limits, no network restrictions. Dangerous for untrusted code or multi-tenant setups.

**Solution:** New execution backend that spins up ephemeral Docker containers:
- Each `execute_code` call → new container from a hermetic image
- 30-second hard timeout
- No network access (unless configured)
- /tmp only writable
- Container destroyed after execution

```python
class DevContainerBackend:
    def execute(self, code: str, language: str, timeout: int = 30) -> str:
        container = self._client.containers.run(
            image=f"hermes-sandbox-{language}",
            command=[language, "-c", code],
            mem_limit="256m",
            network_disabled=True,
            read_only=True,
            tmpfs={"/tmp": "size=10m"},
            timeout=timeout,
        )
        return container.logs()
```

- **New files:** `tools/environments/devcontainer_env.py`
- **Modified files:** `tools/execute_code_tool.py` (new backend option), `toolsets.py`
- **Config keys:** `code_execution.sandbox.type` (none | container), `code_execution.sandbox.image_prefix`

---

### 19. Plugin Scaffolding CLI

**Problem:** Writing a Hermes plugin requires knowing the registration API, hook signatures, and directory layout. Steep learning curve with zero tooling.

**Solution:** `hermes plugin init <name>` command:

```
$ hermes plugin init my-tools
Creating plugin: my-tools
  plugins/my-tools/
  ├── __init__.py         # register() function with example hooks
  ├── tools.py            # example tool registration
  ├── config.yaml         # plugin config schema
  └── tests/
      └── test_tools.py   # example tests

Plugin created. Enable with: hermes config set plugins.my-tools.enabled true
```

```python
# Generated __init__.py
def register(ctx):
    """Register plugin hooks and tools."""

    @ctx.hook("pre_tool_call")
    def log_tool_call(tool_name, args):
        print(f"Tool: {tool_name}")

    ctx.register_tool(
        name="my_tool",
        description="Does something useful",
        handler=my_handler,
    )

    ctx.register_cli_command(
        name="my-command",
        description="A custom CLI subcommand",
        parser=add_my_parser,
    )
```

- **New files:** `hermes_cli/plugin_scaffold.py`, `hermes_cli/templates/plugin/`
- **Modified files:** `hermes_cli/plugins.py` (add scaffold function), `hermes_cli/main.py` (wire `hermes plugin init` subcommand), `hermes_cli/commands.py` (CommandDef)
- **Config keys:** None.

---

## Quick Wins (Small Code Changes)

### 20. `/diff` Slash Command
Show files modified in the current session. Uses `file_state.py` to list what changed.
- **Files:** `cli.py` (handler), `hermes_cli/commands.py` (CommandDef)
- **Depends on:** File state tracking (already exists).

### 21. Multi-Step `/undo`
Current `/undo` removes last assistant+tool message pair. Extend to `/undo N` (undo N steps) with a visual diff of what's being undone.
- **Files:** `cli.py`, `hermes_state.py` (session snapshot/revert)
- **Risk:** Must not break prompt caching (undo = altering past context → cache miss is guaranteed)

### 22. Tool Call Cost Display
Show per-call cost in CLI spinner output and gateway message footers.
```python
# In display.py or the gateway response formatter
"web_search (0.3s · $0.002) ✓"
```
- **Files:** `agent/display.py`, `gateway/platforms/base.py` (message formatter)

### 23. Auto-Retry with Fallback Model on 429/402
When a provider returns rate-limited or insufficient-balance, auto-retry with the next model in a configured fallback chain. Partial infrastructure exists in `agent/credential_pool.py` but no model-level fallback.
```yaml
fallback_chain:
  - model: "claude-sonnet-4"
    provider: "anthropic"
  - model: "gemini-2.5-pro"
    provider: "google"
  - model: "gpt-4o"
    provider: "openrouter"
```
- **Files:** `run_agent.py` (retry logic in main loop), `hermes_cli/config.py`

### 24. Session Export
`hermes session export <id> --format json|md|html`. Export full transcript with tool calls for sharing, documentation, or audit.
```bash
hermes session export abc123 --format md > conversation.md
```
- **Files:** `hermes_cli/main.py` (new subcommand), `hermes_state.py` (export query)
- **Formats:** JSON (raw), Markdown (readable), HTML (styled)

### 25. `/cost` Slash Command
Show running cost for the current session and today across all sessions.
```
Session cost:  $0.47 (1,234 input · 5,678 output tokens)
Today total:   $2.31 across 4 sessions
Hourly rate:   $0.12/hr
```
- **Files:** `cli.py` (handler), `hermes_cli/commands.py` (CommandDef), `hermes_state.py` (cost query)

---

## Dependencies Between Ideas

```
1. Vector memory        ← 3. Knowledge base (shares embedding infrastructure)
2. Multi-agent bus      ← 5. Code review agent (could run as a sub-agent on the bus)
10. Cost budgeting       → 11. Provider stats (stats feed budget enforcement)
10. Cost budgeting       → 13. Multi-platform budget (aggregation over budget base)
4. Workspaces           → 8. Tool timeline (workspace-scoped timeline)
14. Multi-channel        ← 2. Session resume (resume needs cross-platform identity)

Quick wins (20-25) are all independent of each other and of larger features.
```

## Effort Estimates

| Category | Feature | Est. Effort | Risk |
|----------|---------|-------------|------|
| Quick win | 20. `/diff` | 1-2 hrs | Low |
| Quick win | 22. Cost display | 2-4 hrs | Low |
| Quick win | 24. Session export | 3-6 hrs | Low |
| Quick win | 23. Auto-retry | 4-8 hrs | Low |
| Quick win | 25. `/cost` | 2-3 hrs | Low |
| Quick win | 21. Multi-step undo | 4-8 hrs | Medium (caching) |
| Small | 12. Approval audit log | 2-4 hrs | Low |
| Small | 9. System prompt viewer | 3-5 hrs | Low |
| Small | 8. Tool timeline (CLI) | 4-8 hrs | Low |
| Medium | 15. Discord voice | 1-2 weeks | Medium |
| Medium | 19. Plugin scaffolding | 3-5 days | Low |
| Medium | 5. Code review agent | 3-5 days | Low |
| Medium | 16. Morning digest | 3-5 days | Low |
| Medium | 3. Knowledge base | 1-2 weeks | Medium |
| Medium | 4. Workspaces | 1-2 weeks | Medium |
| Medium | 10. Cost budgeting | 3-5 days | Low |
| Medium | 11. Provider stats | 3-5 days | Medium |
| Large | 1. Vector memory | 2-3 weeks | High (embedding quality) |
| Large | 2. Multi-agent bus | 2-3 weeks | High (concurrency) |
| Large | 6. Session replay UI | 1-2 weeks | Medium |
| Large | 7. Live dashboard | 2-3 weeks | Medium |
| Large | 14. Multi-channel | 2-3 weeks | High (session model change) |
| Large | 17. MCP integration | 1-2 weeks | Low |
| Large | 18. Dev container sandbox | 1-2 weeks | Medium |
