"""Lightweight web chat — SSE streaming chat endpoint.

Provides an SSE-based chat interface that connects the existing Hermes
agent to a browser.  Designed as a plug-in module for the FastAPI web
server — minimal imports, no gateway dependency.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncGenerator, Dict, Optional

logger = logging.getLogger(__name__)

# In-memory session store (single-process FastAPI, lost on restart)
_sessions: Dict[str, dict] = {}


def _get_session(session_id: str) -> Optional[dict]:
    """Return session dict or None."""
    return _sessions.get(session_id)


def _create_session() -> str:
    """Create a new chat session and return its ID."""
    session_id = f"chat_{uuid.uuid4().hex[:12]}"
    _sessions[session_id] = {
        "id": session_id,
        "messages": [],
        "created": time.time(),
    }
    return session_id


def _delete_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


async def chat_stream(
    message: str,
    session_id: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Run a chat turn and yield SSE events.

    Event types::

        event: token
        data: {"text": "Hello"}

        event: tool_call
        data: {"name": "web_search", "args": {"query": "..."}, "id": "call_1"}

        event: tool_result
        data: {"id": "call_1", "result": "..."}

        event: error
        data: {"message": "..."}

        event: done
        data: {"session_id": "chat_abc123"}
    """
    # Get or create session
    session = _get_session(session_id)
    if not session:
        session_id = _create_session()
        session = _get_session(session_id)

    session["messages"].append({"role": "user", "content": message})

    try:
        from run_agent import AIAgent

        agent = AIAgent(
            model=model or "",
            provider=provider or "",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

        # We run the agent in a thread since AIAgent.run_conversation() is sync
        loop = asyncio.get_running_loop()

        def _run_agent() -> str:
            result = agent.run_conversation(message)
            final = result.get("final_response", "")
            session["messages"].append({"role": "assistant", "content": final})
            return final

        try:
            final = await asyncio.wait_for(
                loop.run_in_executor(None, _run_agent),
                timeout=300,
            )
        except asyncio.TimeoutError:
            yield f"event: error\ndata: {json.dumps({'message': 'Agent timed out after 300s'})}\n\n"
            yield f"event: done\ndata: {json.dumps({'session_id': session_id})}\n\n"
            return

        # Stream the final response as tokens (rough simulation)
        # In a full implementation, we would hook into the agent's streaming
        # callback and yield tokens as they arrive.
        yield f"event: token\ndata: {json.dumps({'text': final})}\n\n"
        yield f"event: done\ndata: {json.dumps({'session_id': session_id})}\n\n"

    except Exception as e:
        logger.exception("Chat stream error")
        yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
        yield f"event: done\ndata: {json.dumps({'session_id': session_id})}\n\n"
    finally:
        pass
