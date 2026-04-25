# Hermes Agent

Instructions for AI coding assistants working on this codebase.

## Development Environment

```bash
source .venv/bin/activate   # or: source venv/bin/activate
```

- Python >=3.11, Node >=20 (browser tools require Node).
- **Docker**: `Dockerfile` + `docker-compose.yml` at root, entrypoint in `docker/`.
- **Nix**: `flake.nix` with flake-parts, pyproject-nix, uv2nix for nix builds.
- **Windows** is NOT supported natively — use WSL2.

## Entry Points

From `pyproject.toml` `[project.scripts]`:
- `hermes` → `hermes_cli.main:main` (CLI, TUI)
- `hermes-agent` → `run_agent:main`
- `hermes-acp` → `acp_adapter.entry:main`

## Testing

**ALWAYS use `scripts/run_tests.sh`** — never call `pytest` directly. The script enforces CI parity:
- Unsets ALL `*_API_KEY`/`*_TOKEN`/`*_SECRET` env vars
- Redirects `HERMES_HOME` to temp dir
- Forces TZ=UTC, LANG=C.UTF-8, `-n 4` workers

```bash
scripts/run_tests.sh                                  # full suite
scripts/run_tests.sh tests/gateway/                   # directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # single test
scripts/run_tests.sh -v --tb=long                     # pass pytest flags
```

If you must bypass the wrapper (e.g., Windows): `python -m pytest tests/ -q -n 4`.

**Key test fixtures** (all autouse in `tests/conftest.py`):
- `_hermetic_environment` — blanks credentials, sets `HERMES_HOME` to tmp, pins TZ/locale
- `_reset_module_state` — clears tools.approval, tools.interrupt, gateway session context vars between tests
- `_ensure_current_event_loop` — provides event loop for sync tests

**Don't write change-detector tests** — tests that snapshot model catalogs, config versions, or enumeration counts. Write invariant tests instead (e.g., "every catalog entry has a context length").

## Architecture (file dependency order)

```
tools/registry.py  (no deps)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
model_tools.py  (imports registry → triggers auto-discovery)
       ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

Key files at root level:
- `run_agent.py` (~12k LOC) — `AIAgent` class, core sync loop with interrupt checks and iteration budget
- `cli.py` (~11k LOC) — `HermesCLI` class, prompt_toolkit-based interactive shell
- `model_tools.py` (~650 LOC) — `get_tool_definitions()`, `handle_function_call()`, tool discovery bridge
- `toolsets.py` — `_HERMES_CORE_TOOLS` and toolset definitions
- `hermes_state.py` — `SessionDB` (SQLite with FTS5, WAL mode)
- `hermes_constants.py` — `get_hermes_home()`, `display_hermes_home()` (profile-aware paths)
- `batch_runner.py` (~1.3k LOC) — parallel batch processing
- `trajectory_compressor.py` (~1.5k LOC) — context compression

## AIAgent Loop (`run_agent.py`)

Synchronous `run_conversation()` loop. Messages follow OpenAI format. Reasoning content in `assistant_msg["reasoning"]`. Default `max_iterations=90`. Budget tracking with one-turn grace call.

## Adding a New Tool

1. Create `tools/<name>.py` with `registry.register(...)` call at module level
2. Add tool to `_HERMES_CORE_TOOLS` or a new toolset in `toolsets.py`

Auto-discovery scans for top-level `registry.register()` calls — no manual import needed. All handlers MUST return a JSON string. For agent-level tools (todo, memory), see `tools/todo_tool.py` pattern (intercepted in `run_agent.py` before `handle_function_call()`).

## Adding Configuration

| What | Where | Notes |
|------|-------|-------|
| config.yaml keys (non-secrets) | `DEFAULT_CONFIG` in `hermes_cli/config.py` | Adding to existing section is auto-merged, no version bump needed |
| .env vars (SECRETS only) | `OPTIONAL_ENV_VARS` in `hermes_cli/config.py` | Password field, category |

**Three config loaders** — know which one you're in:
| Loader | Used by | Location |
|--------|---------|----------|
| `load_cli_config()` | CLI mode | `cli.py` |
| `load_config()` | `hermes tools`, `hermes setup`, subcommands | `hermes_cli/config.py` |
| Direct YAML load | Gateway runtime | `gateway/run.py` |

New keys must appear in `DEFAULT_CONFIG` or the gateway won't see them.

## Slash Commands

All defined in `COMMAND_REGISTRY` in `hermes_cli/commands.py`. Adding/aliasing a command requires ONLY that file — downstream consumers (CLI dispatch, gateway, Telegram BotCommand menu, Slack mapping, autocomplete) derive automatically.

## TUI (`hermes --tui` or `HERMES_TUI=1`)

Node (Ink) + Python (`tui_gateway`) over stdio JSON-RPC. TypeScript owns the screen, Python owns agent/tools/sessions.

```bash
cd ui-tui
npm install
npm run dev   # watch mode
npm run build # full build (hermes-ink + tsc)
```

## Plugins

Two surfaces under `plugins/`:
- **General plugins** — hooks: `pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`, `on_session_end`; can register tools and CLI commands
- **Memory-provider plugins** (`plugins/memory/<name>/`) — `MemoryProvider` ABC, orchestrated by `agent/memory_manager.py`

**Critical rule: plugins MUST NOT modify core files** (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`, etc.). Expand the generic plugin surface instead.

## Skills

- `skills/` — built-in, loadable by default
- `optional-skills/` — shipped but NOT active by default; install via `hermes skills install official/<category>/<skill>`

## Profiles

`hermes -p <name>` sets `HERMES_HOME` before imports. All path code MUST:
- Use `get_hermes_home()` from `hermes_constants` — never `Path.home() / ".hermes"`
- Use `display_hermes_home()` for user-facing messages
- Mock `Path.home()` AND `HERMES_HOME` in profile-related tests

## Important Policies

### Prompt Caching
Must never break mid-conversation. Do NOT alter past context, change toolsets, reload memories, or rebuild system prompts mid-turn. Slash commands that mutate system-prompt state must default to deferred invalidation with opt-in `--now` flag.

### Known Pitfalls
- **No `simple_term_menu`** — use `hermes_cli/curses_ui.py` instead
- **No `\033[K`** in spinner/display code — use space-padding
- **No hardcoded cross-tool references** in schema descriptions — tools may be unavailable; add dynamically in `get_tool_definitions()` in `model_tools.py`
- **Gateway has TWO message guards** for active sessions — both must be bypassed for approval/control commands
- **`_last_resolved_tool_names`** is a process-global in `model_tools.py` — saved/restored around subagent execution
- **ruff and ty are excluded** in `pyproject.toml` (`exclude = ["*"]`) — not actively used
- **No pre-commit hooks** configured

### Gateway Background Process Notifications
Controlled by `display.background_process_notifications` in config.yaml: `all` (default), `result`, `error`, `off`.
