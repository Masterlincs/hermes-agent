"""Session checkpointing for crash recovery.

Persists active session state to disk so it survives gateway restarts.
On startup, recovers sessions that were in-flight when the process died.

Designed as a drop-in module — the gateway runner calls
``start_checkpointer()`` / ``stop_checkpointer()`` and optionally
``recover_crashed_sessions()`` at startup.  No core gateway internals
are modified.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

CHECKPOINT_FILE = "active_sessions_checkpoint.json"
CHECKPOINT_INTERVAL = 30  # seconds between checkpoints


# ---------------------------------------------------------------------------
# Checkpoint data model
# ---------------------------------------------------------------------------


class _SessionCheckpoint:
    """Serialisable snapshot of a session's runtime state."""

    __slots__ = (
        "session_key", "session_id", "platform", "chat_id", "model", "provider",
        "approved_patterns", "checkpoint_ts", "tool_count", "last_tool",
    )

    def __init__(
        self,
        session_key: str,
        session_id: str = "",
        platform: str = "",
        chat_id: str = "",
        model: str = "",
        provider: str = "",
        approved_patterns: Optional[set] = None,
        checkpoint_ts: float = 0.0,
        tool_count: int = 0,
        last_tool: str = "",
    ):
        self.session_key = session_key
        self.session_id = session_id
        self.platform = platform
        self.chat_id = chat_id
        self.model = model
        self.provider = provider
        self.approved_patterns = approved_patterns or set()
        self.checkpoint_ts = checkpoint_ts or time.time()
        self.tool_count = tool_count
        self.last_tool = last_tool

    def to_dict(self) -> dict:
        return {
            "session_key": self.session_key,
            "session_id": self.session_id,
            "platform": self.platform,
            "chat_id": self.chat_id,
            "model": self.model,
            "provider": self.provider,
            "approved_patterns": list(self.approved_patterns),
            "checkpoint_ts": self.checkpoint_ts,
            "tool_count": self.tool_count,
            "last_tool": self.last_tool,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_SessionCheckpoint":
        return cls(
            session_key=d.get("session_key", ""),
            session_id=d.get("session_id", ""),
            platform=d.get("platform", ""),
            chat_id=d.get("chat_id", ""),
            model=d.get("model", ""),
            provider=d.get("provider", ""),
            approved_patterns=set(d.get("approved_patterns", [])),
            checkpoint_ts=d.get("checkpoint_ts", 0.0),
            tool_count=d.get("tool_count", 0),
            last_tool=d.get("last_tool", ""),
        )


# ---------------------------------------------------------------------------
# Checkpoint file IO
# ---------------------------------------------------------------------------


def _get_checkpoint_path(hermes_home: Optional[str] = None) -> Path:
    if hermes_home:
        base = Path(hermes_home)
    else:
        base = Path(os.path.expanduser("~/.hermes"))
    return base / CHECKPOINT_FILE


def _load_checkpoints(hermes_home: Optional[str] = None) -> Dict[str, dict]:
    path = _get_checkpoint_path(hermes_home)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load session checkpoint: %s", e)
        return {}


def _save_checkpoints(data: Dict[str, dict], hermes_home: Optional[str] = None) -> bool:
    path = _get_checkpoint_path(hermes_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
        return True
    except OSError as e:
        logger.warning("Failed to save session checkpoint: %s", e)
        return False


# ---------------------------------------------------------------------------
# Checkpointer — background thread that periodically snapshots active sessions
# ---------------------------------------------------------------------------


class _Checkpointer:
    """Background thread that persists active session state to disk.

    Usage::

        cp = _Checkpointer()
        cp.register_session("sk-1", session_id="...", model="claude", ...)
        cp.start()    # begins periodic saves
        ...
        cp.unregister_session("sk-1")
        cp.stop()     # final save + thread join
    """

    def __init__(self, hermes_home: Optional[str] = None, interval: float = CHECKPOINT_INTERVAL):
        self._sessions: Dict[str, _SessionCheckpoint] = {}
        self._lock = threading.Lock()
        self._hermes_home = hermes_home
        self._interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # -- Public API ---------------------------------------------------------

    def register_session(self, session_key: str, **kwargs) -> None:
        cp = _SessionCheckpoint(session_key=session_key, **kwargs)
        with self._lock:
            self._sessions[session_key] = cp

    def unregister_session(self, session_key: str) -> None:
        with self._lock:
            self._sessions.pop(session_key, None)

    def update_model(self, session_key: str, model: str, provider: str = "") -> None:
        with self._lock:
            cp = self._sessions.get(session_key)
            if cp:
                cp.model = model
                cp.provider = provider

    def update_approved_patterns(self, session_key: str, patterns: set) -> None:
        with self._lock:
            cp = self._sessions.get(session_key)
            if cp:
                cp.approved_patterns = patterns

    def update_tool_activity(self, session_key: str, tool_name: str, tool_count: int) -> None:
        with self._lock:
            cp = self._sessions.get(session_key)
            if cp:
                cp.last_tool = tool_name
                cp.tool_count = tool_count

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="checkpointer")
        self._thread.start()
        logger.debug("Session checkpointer started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._save_now()
        logger.debug("Session checkpointer stopped")

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            self._save_now()

    def _save_now(self) -> None:
        with self._lock:
            if not self._sessions:
                return
            data = {k: v.to_dict() for k, v in self._sessions.items()}
        _save_checkpoints(data, self._hermes_home)

    def load_known_sessions(self) -> Dict[str, dict]:
        return _load_checkpoints(self._hermes_home)


# ---------------------------------------------------------------------------
# Recovery helpers — call at gateway startup
# ---------------------------------------------------------------------------


def recover_crashed_sessions(hermes_home: Optional[str] = None) -> list[dict]:
    """Return a list of sessions that were active when the process died.

    Reads the checkpoint file, picks sessions that were modified within the
    last 5 minutes, and returns them as structured recovery hints.  The
    caller (gateway runner) decides whether to mark them ``resume_pending``.

    Returns::
        [
            {
                "session_key": "discord:12345",
                "session_id": "20260425_123456_abc",
                "platform": "discord",
                "chat_id": "12345",
                "model": "claude-sonnet-4",
                "provider": "anthropic",
                "approved_patterns": ["recursive delete"],
                "last_tool": "file_read",
            },
            ...
        ]
    """
    raw = _load_checkpoints(hermes_home)
    if not raw:
        return []

    now = time.time()
    cutoff = now - 300  # 5 minutes

    recovered = []
    for key, data in raw.items():
        ts = data.get("checkpoint_ts", 0)
        if ts < cutoff:
            continue
        recovered.append({
            "session_key": key,
            "session_id": data.get("session_id", ""),
            "platform": data.get("platform", ""),
            "chat_id": data.get("chat_id", ""),
            "model": data.get("model", ""),
            "provider": data.get("provider", ""),
            "approved_patterns": list(data.get("approved_patterns", [])),
            "last_tool": data.get("last_tool", ""),
        })

    if recovered:
        logger.info("Recovered %d crashed session(s) from checkpoint", len(recovered))

    return recovered


def clear_checkpoint(hermes_home: Optional[str] = None) -> None:
    """Remove the checkpoint file after a clean shutdown."""
    path = _get_checkpoint_path(hermes_home)
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        logger.debug("Failed to clear checkpoint: %s", e)
