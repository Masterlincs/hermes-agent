"""Browser extension provider — routes browser tool calls through a
WebSocket-connected browser extension.

The extension runs in the user's real browser (Chrome/Firefox) and
communicates back to Hermes via a WebSocket bridge.  Unlike the
agent-browser CLI (headless) or Camofox (self-hosted Node server),
this provider gives the agent access to the user's actual browser
session — cookies, logged-in accounts, extensions, etc.

Architecture::

    Hermes Agent ──WebSocket── Browser Extension
                                    │
                                    └── content_script.js (DOM access)
                                        click, type, scroll, navigate,
                                        snapshot, screenshot, evaluate

Usage::

    1. Install the extension in your browser (see browser-extension/ dir)
    2. Set ``BROWSER_EXTENSION_WS_URL`` in ``~/.hermes/.env``::

        BROWSER_EXTENSION_WS_URL=ws://localhost:9876

    3. The browser tool will auto-detect the extension and route calls
       through it when available.
"""

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_EXTENSION_WS_URL = os.getenv("BROWSER_EXTENSION_WS_URL", "").strip()
_connection: Optional[Any] = None
_connection_lock = threading.Lock()
_connected = False
_last_health_check = 0.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def get_extension_url() -> str:
    """Return the configured extension WebSocket URL, or empty string."""
    return _EXTENSION_WS_URL


def is_extension_mode() -> bool:
    """True when a browser extension WebSocket URL is configured."""
    return bool(get_extension_url())


def check_extension_available() -> bool:
    """Check if the browser extension is connected and reachable."""
    global _connected, _last_health_check

    if not is_extension_mode():
        return False

    now = time.time()
    if now - _last_health_check < 5 and not _connected:
        return False

    _last_health_check = now

    try:
        # Try to connect or ping the existing connection
        with _connection_lock:
            if _connection is not None:
                _connected = True
                return True
        # No active connection — try a quick WS handshake
        _connect_sync()
        return _connected
    except Exception:
        _connected = False
        return False


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


def _connect_sync() -> None:
    """Establish a WebSocket connection to the browser extension (sync wrapper)."""
    global _connection, _connected

    url = get_extension_url()
    if not url:
        _connected = False
        return

    try:
        import websocket as _ws

        ws = _ws.create_connection(url, timeout=5)
        with _connection_lock:
            if _connection is not None:
                try:
                    _connection.close()
                except Exception:
                    pass
            _connection = ws
            _connected = True
        logger.info("Connected to browser extension at %s", url)
    except ImportError:
        logger.debug("websocket-client not installed; extension mode unavailable")
        _connected = False
    except Exception as e:
        logger.debug("Failed to connect to browser extension: %s", e)
        _connected = False


def _disconnect() -> None:
    """Close the WebSocket connection."""
    global _connection, _connected
    with _connection_lock:
        if _connection is not None:
            try:
                _connection.close()
            except Exception:
                pass
            _connection = None
        _connected = False


# ---------------------------------------------------------------------------
# Send command to extension and wait for response
# ---------------------------------------------------------------------------


_CMD_TIMEOUT = 30  # seconds


def _send_command(command_type: str, **kwargs) -> Dict[str, Any]:
    """Send a command to the browser extension and return the response.

    Uses a request-response pattern with a unique ``id`` per call.
    """
    if not is_extension_mode():
        return {"success": False, "error": "Browser extension not configured"}

    request_id = f"ext_{int(time.time() * 1000)}_{hash(str(kwargs)) & 0xFFFF}"

    payload = {"type": command_type, "id": request_id, **kwargs}

    with _connection_lock:
        ws = _connection
        if ws is None:
            return {"success": False, "error": "Not connected to browser extension"}

        try:
            ws.send(json.dumps(payload))
            ws.settimeout(_CMD_TIMEOUT)
            response_raw = ws.recv()
            result = json.loads(response_raw)

            if result.get("id") != request_id:
                logger.debug("Extension response ID mismatch (expected %s, got %s)", request_id, result.get("id"))

            return result
        except Exception as e:
            logger.debug("Browser extension command failed: %s", e)
            return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Tool interface (mirrors camofox.py's API surface)
# ---------------------------------------------------------------------------


def ext_navigate(url: str, task_id: Optional[str] = None) -> str:
    """Navigate the browser to a URL."""
    result = _send_command("navigate", url=url)
    if result.get("success"):
        return json.dumps({"result": f"Navigated to {url}", "url": result.get("url", url)})
    return json.dumps({"error": result.get("error", "Navigation failed")})


def ext_snapshot(full: bool = False, task_id: Optional[str] = None, user_task: Optional[str] = None) -> str:
    """Get an accessibility snapshot of the current page."""
    result = _send_command("snapshot", full=full)
    if result.get("success"):
        return json.dumps({"result": result.get("snapshot", ""), "url": result.get("url", "")})
    return json.dumps({"error": result.get("error", "Snapshot failed")})


def ext_click(ref: str, task_id: Optional[str] = None) -> str:
    """Click an element identified by its accessibility ref ID."""
    result = _send_command("click", ref=ref)
    if result.get("success"):
        return json.dumps({"result": f"Clicked element @{ref}", "url": result.get("url", "")})
    return json.dumps({"error": result.get("error", f"Click failed for @{ref}")})


def ext_type(ref: str, text: str, task_id: Optional[str] = None) -> str:
    """Type text into an input field identified by ref ID."""
    result = _send_command("type", ref=ref, text=text)
    if result.get("success"):
        return json.dumps({"result": f"Typed into @{ref}", "url": result.get("url", "")})
    return json.dumps({"error": result.get("error", f"Type failed for @{ref}")})


def ext_scroll(direction: str, task_id: Optional[str] = None) -> str:
    """Scroll the page up or down."""
    result = _send_command("scroll", direction=direction)
    if result.get("success"):
        return json.dumps({"result": f"Scrolled {direction}"})
    return json.dumps({"error": result.get("error", "Scroll failed")})


def ext_back(task_id: Optional[str] = None) -> str:
    """Navigate back in browser history."""
    result = _send_command("back")
    if result.get("success"):
        return json.dumps({"result": "Navigated back", "url": result.get("url", "")})
    return json.dumps({"error": result.get("error", "Navigation back failed")})


def ext_press(key: str, task_id: Optional[str] = None) -> str:
    """Press a keyboard key."""
    result = _send_command("press", key=key)
    if result.get("success"):
        return json.dumps({"result": f"Pressed key: {key}"})
    return json.dumps({"error": result.get("error", f"Key press failed: {key}")})


def ext_console(clear: bool = False, task_id: Optional[str] = None) -> str:
    """Get console output or evaluate JavaScript."""
    result = _send_command("console", clear=clear)
    if result.get("success"):
        return json.dumps({"result": result.get("output", ""), "url": result.get("url", "")})
    return json.dumps({"error": result.get("error", "Console command failed")})


def ext_get_images(task_id: Optional[str] = None) -> str:
    """List all images on the current page."""
    result = _send_command("get_images")
    if result.get("success"):
        return json.dumps({"result": result.get("images", []), "url": result.get("url", "")})
    return json.dumps({"error": result.get("error", "Get images failed")})


def ext_vision(question: str, annotate: bool = False, task_id: Optional[str] = None) -> str:
    """Take a screenshot and ask a vision question about it."""
    result = _send_command("vision", question=question, annotate=annotate)
    if result.get("success"):
        return json.dumps({"result": result.get("answer", ""), "annotated": result.get("annotated", False)})
    return json.dumps({"error": result.get("error", "Vision analysis failed")})
