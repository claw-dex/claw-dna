"""ClaudeChat — sync adapter for claude-agent-sdk with non-blocking streaming.

Also provides the Streamlit chat UI render() function.
"""

import asyncio
import json
import os
import queue
import threading
from pathlib import Path

import streamlit as st

from app.shared import CHAT_HISTORY_PATH, _write_json_atomic
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent

SYSTEM_MD = Path("/agent/system.md")
CONSTITUTION_MD = Path("/agent/constitution.md")
PORTAL_CONFIG = Path("/agent/memory/portal_config.json")
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "myagent")


def _build_system_prompt(chat_history: list[dict] | None = None) -> str:
    """Build system prompt from system.md + constitution.md + public URL + optional chat history."""
    parts: list[str] = []
    if SYSTEM_MD.exists():
        parts.append(SYSTEM_MD.read_text())
    if CONSTITUTION_MD.exists():
        parts.append("## Immutable Rules (Constitution)\n")
        parts.append(CONSTITUTION_MD.read_text())
    # Inject public hostname if configured
    if PORTAL_CONFIG.exists():
        try:
            cfg = json.loads(PORTAL_CONFIG.read_text())
            public_url = cfg.get("public_url", "")
            if public_url:
                parts.append(f"\n## Public URL\n")
                parts.append(f"This agent is accessible at: {public_url}\n")
                parts.append(
                    "When sharing links with the user (portal, file explorer, workspace files, "
                    "generated reports), use this public URL as the base instead of localhost:8080. "
                    "For example:\n"
                    f"- Portal: {public_url}/app/\n"
                    f"- Static Web: {public_url}/web/ (static files from /agent/web/)\n"
                    f"- File Explorer: {public_url}/_/\n"
                    f"- Workspace files: ${public_url}/_/agent/workspace/path/to/<filename>\n"
                )
                parts.append(
                    "Note: For internal operations (curl, health checks, Caddy admin API), "
                    "continue using localhost."
                )
        except (json.JSONDecodeError, OSError):
            pass
    if chat_history:
        parts.append("\n## Previous Chat History\n")
        parts.append(
            "Below is the conversation history from the previous session. Use it for context.\n"
        )
        # Cap injected history to avoid exceeding context window limits
        max_chars = 8000
        recent = chat_history[-20:]
        total = 0
        trimmed: list[dict] = []
        for msg in reversed(recent):
            entry_len = len(msg.get("content", "")) + len(msg.get("role", "")) + 10
            if total + entry_len > max_chars:
                break
            trimmed.append(msg)
            total += entry_len
        trimmed.reverse()
        for msg in trimmed:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            parts.append(f"**{role}**: {content}\n")
    return "\n".join(parts)


# ── Chat history helpers ───────────────────────────────────────
_MAX_CHAT_HISTORY = 200  # cap persisted messages


def _load_chat_history() -> list:
    """Load chat history from disk."""
    try:
        with open(CHAT_HISTORY_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_chat_history(messages: list) -> None:
    """Persist chat messages to disk (capped at _MAX_CHAT_HISTORY)."""
    _write_json_atomic(CHAT_HISTORY_PATH, messages[-_MAX_CHAT_HISTORY:], indent=2)


class ClaudeChat:
    """Sync adapter for claude-agent-sdk, lives in st.session_state.

    A background daemon thread runs an asyncio event loop with a persistent
    ClaudeSDKClient.  Public API:

    - send(prompt)   — blocking, returns full response text (for programmatic use)
    - submit(prompt) — non-blocking, starts background streaming
    - poll()         — non-blocking, drains available typed events from the queue
    - is_streaming() — True while a submit() is in progress
    - is_alive()     — True if the background thread is running
    - close()        — disconnect and stop
    """

    def __init__(
        self,
        resume_session_id: str | None = None,
        chat_history: list[dict] | None = None,
    ) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sdk: ClaudeSDKClient | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._resume_session_id = resume_session_id
        self._chat_history = chat_history or []
        self._session_id: str | None = None

        # Streaming state (for submit/poll pattern)
        self._chunk_q: queue.Queue | None = None
        self._done_event: threading.Event | None = None
        self._stream_future: asyncio.Future | None = None

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # Block until SDK is connected (or failed)
        if not self._ready.wait(timeout=30):
            raise TimeoutError("Claude SDK client did not connect within 30s")
        if self._error is not None:
            raise self._error

    # ── background thread ─────────────────────────────────────

    def _run_loop(self) -> None:
        """Entry point for the daemon thread — owns the asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
            self._loop.run_forever()
        except Exception as exc:
            self._error = exc
            self._ready.set()  # unblock __init__ on failure
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()

    async def _connect(self) -> None:
        """Create and connect the SDK client."""
        options = ClaudeAgentOptions(
            system_prompt=_build_system_prompt(self._chat_history),
            permission_mode="bypassPermissions",
            include_partial_messages=True,
            cwd="/agent",
            add_dirs=["/home/agent", "/home/agent/.claude", "/agent/.claude"],
            setting_sources=["user", "project"],  # Load Skills from filesystem
            allowed_tools=[
                "Skill",
                "Bash",
                "Glob",
                "Grep",
                "Read",
                "Edit",
                "Write",
                "TodoWrite",
                "WebFetch",
                "WebSearch",
                "BashOutput",
                "KillShell",
                "ListMcpResourcesTool",
                "ReadMcpResourceTool",
            ],  # Enable Skill tool
            disallowed_tools=["AskUserQuestion"],
            resume=self._resume_session_id,
        )
        self._sdk = ClaudeSDKClient(options)
        await self._sdk.connect()
        self._ready.set()

    # ── async helpers (run on background loop) ────────────────

    async def _async_send(self, prompt: str) -> str:
        """Send prompt, collect and return full response text."""
        await self._sdk.query(prompt)
        parts: list[str] = []
        async for msg in self._sdk.receive_response():
            if isinstance(msg, StreamEvent):
                continue  # skip deltas — we want the final AssistantMessage
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                sid = getattr(msg, "session_id", None)
                if sid:
                    with self._lock:
                        self._session_id = sid
                break
        return "\n".join(parts)

    async def _async_stream(
        self, prompt: str, chunk_q: queue.Queue, done_event: threading.Event
    ) -> None:
        """Send prompt, push typed event dicts to *chunk_q*, set done_event when complete."""
        try:
            await self._sdk.query(prompt)
            streamed_any = False
            async for msg in self._sdk.receive_response():
                if isinstance(msg, StreamEvent):
                    event = msg.event
                    sid = getattr(msg, "session_id", None)
                    if sid:
                        with self._lock:
                            self._session_id = sid
                    if event.get("type") == "content_block_delta":
                        text = (event.get("delta") or {}).get("text", "")
                        if text:
                            streamed_any = True
                            chunk_q.put({"type": "text", "text": text})
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            if not streamed_any:
                                chunk_q.put({"type": "text", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            chunk_q.put(
                                {
                                    "type": "tool_use",
                                    "name": block.name,
                                    "tool_id": block.id,
                                    "input": block.input,
                                }
                            )
                        elif isinstance(block, ThinkingBlock):
                            chunk_q.put(
                                {
                                    "type": "thinking",
                                    "text": block.thinking,
                                }
                            )
                    streamed_any = False  # reset for next AssistantMessage round
                elif isinstance(msg, SystemMessage):
                    chunk_q.put(
                        {
                            "type": "system",
                            "subtype": msg.subtype,
                            "data": msg.data,
                        }
                    )
                elif isinstance(msg, ResultMessage):
                    sid = getattr(msg, "session_id", None)
                    if sid:
                        with self._lock:
                            self._session_id = sid
                    chunk_q.put(
                        {
                            "type": "result",
                            "cost": msg.total_cost_usd,
                            "duration_ms": msg.duration_ms,
                            "is_error": msg.is_error,
                            "num_turns": msg.num_turns,
                            "session_id": self._session_id,
                        }
                    )
                    break
        except Exception as exc:
            chunk_q.put({"type": "error", "error": str(exc)})
        finally:
            done_event.set()

    # ── public API ────────────────────────────────────────────

    def send(self, prompt: str, timeout: float = 300) -> str:
        """Blocking send. Returns full response text."""
        with self._lock:
            future = asyncio.run_coroutine_threadsafe(
                self._async_send(prompt),
                self._loop,
            )
            return future.result(timeout=timeout)

    def submit(self, prompt: str) -> None:
        """Non-blocking: start processing prompt in background.

        Call poll() on subsequent reruns to drain results.
        Call is_streaming() to check if the stream has finished.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("ClaudeChat session is closed")
            # Cancel any existing in-flight stream
            if self._stream_future is not None and not self._stream_future.done():
                self._stream_future.cancel()

            self._chunk_q = queue.Queue()
            self._done_event = threading.Event()
            self._stream_future = asyncio.run_coroutine_threadsafe(
                self._async_stream(prompt, self._chunk_q, self._done_event),
                self._loop,
            )

    def poll(self) -> list[dict]:
        """Non-blocking: drain all available events from the queue right now.

        Returns a list of typed event dicts (may be empty if nothing new).
        Event types: "text", "tool_use", "thinking", "system", "result", "error".
        """
        if self._chunk_q is None:
            return []
        events = []
        while True:
            try:
                item = self._chunk_q.get_nowait()
                events.append(item)
            except queue.Empty:
                break
        return events

    def is_streaming(self) -> bool:
        """True if a submit() stream is in progress (not yet finished)."""
        if self._done_event is None:
            return False
        return not self._done_event.is_set()

    def is_alive(self) -> bool:
        """Return True if the background thread is still running."""
        return self._thread.is_alive()

    @property
    def is_closed(self) -> bool:
        """Return True if close() has been called."""
        return self._closed

    @property
    def session_id(self) -> str | None:
        """Return the current SDK session ID (captured from responses)."""
        with self._lock:
            return self._session_id

    def close(self) -> None:
        """Disconnect the SDK client and stop the background loop."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Cancel any in-flight stream
        if self._stream_future is not None and not self._stream_future.done():
            self._stream_future.cancel()
        if self._loop is not None and self._loop.is_running():

            async def _shutdown():
                if self._sdk is not None:
                    await self._sdk.disconnect()
                self._loop.stop()

            fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            try:
                fut.result(timeout=10)
            except Exception:
                pass
        self._thread.join(timeout=10)


# ── Streamlit chat UI ─────────────────────────────────────────


def render():
    """Render the Claude Code Chat UI component (always visible at top of page)."""
    st.subheader("Claude Code Chat")
    st.caption(
        "Interactive chat with Claude Code (native, powered by Claude Agent SDK)."
    )

    # Initialize chat history (load from disk on first session)
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = _load_chat_history()

    # Initialize or recover ClaudeChat (cache failure to avoid 30s block on every rerun)
    if "chat_session" not in st.session_state:
        st.session_state.chat_session = None

    needs_new = (
        st.session_state.chat_session is None
        or not st.session_state.chat_session.is_alive()
    )
    if needs_new and not st.session_state.get("chat_connect_failed"):
        old = st.session_state.chat_session
        if old is not None:
            old.close()
        try:
            resume_id = st.session_state.get("chat_session_id")
            history = st.session_state.get("chat_messages", [])
            st.session_state.chat_session = ClaudeChat(
                resume_session_id=resume_id,
                chat_history=history,
            )
            st.session_state.chat_connect_failed = False
        except Exception as exc:
            st.warning(
                f"Could not start Claude Code chat session: {exc}\n\n"
                "Make sure Claude Code CLI is authenticated:\n"
                f"```\ndocker exec -it {CONTAINER_NAME} claude\n```"
            )
            st.session_state.chat_session = None
            st.session_state.chat_connect_failed = True

    if st.session_state.get("chat_connect_failed"):
        if st.button("Retry connection", key="retry_chat"):
            st.session_state.chat_connect_failed = False
            st.rerun()

    # Initialize streaming state
    if "chat_streaming" not in st.session_state:
        st.session_state.chat_streaming = False
    if "chat_stream_text" not in st.session_state:
        st.session_state.chat_stream_text = ""
    if "chat_stream_events" not in st.session_state:
        st.session_state.chat_stream_events = []

    session = st.session_state.chat_session

    # ── Active streaming: poll for new chunks on each rerun ──
    if st.session_state.chat_streaming and session:
        new_events = session.poll()
        for ev in new_events:
            st.session_state.chat_stream_events.append(ev)
            if ev["type"] == "text":
                st.session_state.chat_stream_text += ev["text"]
            elif ev["type"] == "error":
                st.session_state.chat_stream_text += f"\n\n**Error:** {ev['error']}"

        if not session.is_streaming():
            # Final drain to catch any events between last poll() and done_event
            trailing = session.poll()
            for ev in trailing:
                st.session_state.chat_stream_events.append(ev)
                if ev["type"] == "text":
                    st.session_state.chat_stream_text += ev["text"]
                elif ev["type"] == "error":
                    st.session_state.chat_stream_text += f"\n\n**Error:** {ev['error']}"
            # Stream complete — save to history and persist to disk
            full_text = st.session_state.chat_stream_text
            if full_text:
                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": full_text}
                )
                _save_chat_history(st.session_state.chat_messages)
            # Capture session_id for resumption
            if session.session_id:
                st.session_state.chat_session_id = session.session_id
            st.session_state.chat_streaming = False
            st.session_state.chat_stream_text = ""
            st.session_state.chat_stream_events = []

    # Display chat history
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Render active stream (in progress, not yet saved to history)
    if st.session_state.chat_streaming:
        with st.chat_message("assistant"):
            st.markdown(st.session_state.chat_stream_text or "Processing...")
            for ev in st.session_state.chat_stream_events:
                if ev["type"] == "tool_use":
                    st.caption(f"Used tool: {ev['name']}")
                elif ev["type"] == "thinking":
                    with st.expander("Thinking..."):
                        st.markdown(ev["text"])
            if not st.session_state.chat_stream_text:
                st.caption("Thinking...")
            else:
                st.caption("Streaming...")

    # Clear chat button — visual reset only; keeps SDK session and
    # chat_session_id alive so server-side context survives.
    if st.session_state.chat_messages:
        if st.button("Clear chat", key="clear_chat"):
            st.session_state.chat_messages = []
            _save_chat_history([])
            st.session_state.chat_streaming = False
            st.session_state.chat_stream_text = ""
            st.session_state.chat_stream_events = []
            st.rerun()

    # Handle pending prompts from other components (e.g. "Ask AI" buttons)
    if (
        not st.session_state.chat_streaming
        and st.session_state.get("chat_pending_prompt")
        and session is not None
        and session.is_alive()
        and not session.is_closed
    ):
        pending = st.session_state.pop("chat_pending_prompt")
        st.session_state.chat_messages.append({"role": "user", "content": pending})
        _save_chat_history(st.session_state.chat_messages)
        session.submit(pending)
        st.session_state.chat_streaming = True
        st.session_state.chat_stream_text = ""
        st.session_state.chat_stream_events = []
        st.rerun()

    # Drop pending prompt if chat session is unavailable
    if st.session_state.get("chat_pending_prompt") and (
        session is None or not session.is_alive() or session.is_closed
    ):
        dropped = st.session_state.pop("chat_pending_prompt")
        st.warning(
            f"Chat session is not available. Could not send prompt:\n\n{dropped[:200]}"
        )

    # Chat input (disabled during active streaming to prevent overlap)
    if prompt := st.chat_input(
        "Ask Claude Code anything...",
        key="chat_input",
        disabled=st.session_state.chat_streaming,
    ):
        session = st.session_state.chat_session  # re-read in case it was recreated
        if session is None or not session.is_alive() or session.is_closed:
            st.error(
                "Chat session is not available. Please check Claude Code CLI authentication."
            )
        else:
            st.session_state.chat_messages.append({"role": "user", "content": prompt})
            _save_chat_history(st.session_state.chat_messages)
            session.submit(prompt)
            st.session_state.chat_streaming = True
            st.session_state.chat_stream_text = ""
            st.session_state.chat_stream_events = []
            st.rerun()
