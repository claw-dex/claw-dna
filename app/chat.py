"""ClaudeChat — sync adapter for claude-agent-sdk with non-blocking streaming.

Also provides the Streamlit chat UI render() function.
"""

import asyncio
import atexit
import json
import os
import queue
import threading
import weakref
from pathlib import Path

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from app.shared import CHAT_HISTORY_PATH, CHAT_META_PATH, _write_json_atomic
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
CLAUDE_SYSTEM_PROMPT_MD = Path("/home/agent/claude-system-prompt.md")
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "myagent")


def _build_system_prompt(chat_history: list[dict] | None = None) -> str:
    """Build system prompt from system.md + constitution.md + public URL + optional chat history.

    Sections are wrapped in XML tags so that boundaries remain unambiguous when
    concatenated with other markdown content (e.g. by agent.sh).
    """
    parts: list[str] = []
    if SYSTEM_MD.exists():
        parts.append("<system_info>")
        parts.append(SYSTEM_MD.read_text())
        parts.append("</system_info>")
    if CONSTITUTION_MD.exists():
        parts.append("<constitution>")
        parts.append(CONSTITUTION_MD.read_text())
        parts.append("</constitution>")
    # Inject public hostname if configured
    if PORTAL_CONFIG.exists():
        try:
            cfg = json.loads(PORTAL_CONFIG.read_text())
            public_url = cfg.get("public_url", "")
            if public_url:
                parts.append("<public_url>")
                parts.append(f"This agent is accessible at: {public_url}")
                parts.append(
                    "When sharing links with the user (portal, file explorer, workspace files, "
                    "generated reports), use this public URL as the base instead of localhost:8080. "
                    "For example:\n"
                    f"- Portal: {public_url}/app/\n"
                    f"- Static Web: {public_url}/web/ (static files from /agent/web/)\n"
                    f"- File Explorer: {public_url}/_/\n"
                    f"- Workspace files: {public_url}/_/agent/workspace/path/to/<filename>"
                )
                parts.append(
                    "Note: For internal operations (curl, health checks, Caddy admin API), "
                    "continue using localhost."
                )
                parts.append("</public_url>")
        except (json.JSONDecodeError, OSError):
            pass
    if chat_history:
        parts.append("<previous_chat_history>")
        parts.append(
            "Below is the conversation history from the previous session. Use it for context."
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
            parts.append(f"**{role}**: {content}")
        parts.append("</previous_chat_history>")
    if CLAUDE_SYSTEM_PROMPT_MD.exists():
        parts.append("<claude_system_prompt>")
        parts.append(CLAUDE_SYSTEM_PROMPT_MD.read_text())
        parts.append("</claude_system_prompt>")
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


def _load_chat_meta() -> dict:
    """Load persisted SDK metadata (resume session id, etc.)."""
    try:
        with open(CHAT_META_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_chat_session_id(session_id: str | None) -> None:
    """Persist the latest SDK session_id so the singleton can resume across
    streamlit process restarts."""
    if not session_id:
        return
    meta = _load_chat_meta()
    if meta.get("session_id") == session_id:
        return
    meta["session_id"] = session_id
    _write_json_atomic(CHAT_META_PATH, meta, indent=2)


# ── Module-level cleanup helpers (used by atexit + weakref.finalize) ───────


# Tracks (loop_id, sdk_id) tuples that have already been torn down, so that
# weakref.finalize + atexit + explicit close() racing does not re-disconnect
# an already-closed SDK transport.
_SHUTDOWN_DONE: set[tuple[int, int]] = set()
_SHUTDOWN_LOCK = threading.Lock()


def _shutdown_chat_resources(loop, sdk, thread) -> None:
    """Best-effort tear-down of an asyncio loop, SDK client, and daemon thread.

    Module-level (not a method) so it can be called from a weakref.finalize
    callback without keeping the ClaudeChat instance alive. Idempotent:
    repeated calls for the same (loop, sdk) pair are a no-op.
    """
    key = (id(loop), id(sdk))
    with _SHUTDOWN_LOCK:
        if key in _SHUTDOWN_DONE:
            return
        _SHUTDOWN_DONE.add(key)
    try:
        if loop is not None and loop.is_running() and sdk is not None:

            async def _shutdown():
                try:
                    await sdk.disconnect()
                finally:
                    loop.stop()

            try:
                fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
                fut.result(timeout=5)
            except Exception:
                pass
        elif sdk is not None:
            # Loop is already dead but the SDK may still own a child process.
            # Try a synchronous best-effort terminate on whatever transport it
            # exposes — attribute names vary across claude-agent-sdk versions
            # so we probe a few rather than hard-coding one.
            for attr in ("_transport", "transport", "_process", "process"):
                obj = getattr(sdk, attr, None)
                if obj is None:
                    continue
                proc = getattr(obj, "process", obj)
                for action in ("terminate", "kill"):
                    fn = getattr(proc, action, None)
                    if callable(fn):
                        try:
                            fn()
                        except Exception:
                            pass
                        break
                break
    except Exception:
        pass
    try:
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
    except Exception:
        pass


def _atexit_close_chat(ref) -> None:
    """atexit callback that closes a ClaudeChat instance via weakref.

    Using a weakref means atexit does not pin the instance in memory — if it
    has already been GC'd / closed, this is a no-op.
    """
    inst = ref()
    if inst is None:
        return
    try:
        inst.close()
    except Exception:
        pass


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
        self._submit_lock = threading.Lock()
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
            # Connect timed out — best-effort cleanup so we don't leak the
            # daemon thread + a possibly half-spawned `claude` subprocess.
            _shutdown_chat_resources(self._loop, self._sdk, self._thread)
            raise TimeoutError("Claude SDK client did not connect within 30s")
        if self._error is not None:
            # _connect raised — the loop has already exited via _run_loop's
            # except branch, but the SDK may have started a subprocess before
            # failing. Best-effort tear-down.
            _shutdown_chat_resources(self._loop, self._sdk, self._thread)
            raise self._error

        # Register cleanup hooks AFTER the SDK is up, so we never finalize a
        # half-built instance.
        # - weakref.finalize: instance GC'd without close() (e.g.
        #   cache_resource was cleared, or the daemon thread died and a new
        #   instance replaced this one) → still tear down the subprocess.
        # - atexit: streamlit process exit → disconnect SDK → reap `claude`
        #   child. Wrapped in a weakref so atexit doesn't keep the instance
        #   alive past its natural lifetime.
        self._finalizer = weakref.finalize(
            self,
            _shutdown_chat_resources,
            self._loop,
            self._sdk,
            self._thread,
        )
        self._atexit_ref = weakref.ref(self)
        # Wrap in a per-instance closure so atexit.unregister(self._atexit_cb)
        # in close() only drops THIS instance's hook — atexit.unregister
        # matches by callable identity, not by (callable, args).
        ref = self._atexit_ref
        self._atexit_cb = lambda: _atexit_close_chat(ref)
        atexit.register(self._atexit_cb)

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
        """Send prompt, push typed event dicts to *chunk_q*, set done_event when complete.

        Iterates `receive_messages()` directly (rather than `receive_response()`)
        so that after the turn's `ResultMessage` we can keep pulling on the
        *same* iterator for a short drain window — catching any late-arriving
        messages so they do not leak into the next turn. Opening a parallel
        iterator on the SDK's shared receive stream would steal messages and
        race the next turn, so the drain must reuse this iterator.
        """
        try:
            await self._sdk.query(prompt)
            streamed_any = False
            result_seen = False
            it = self._sdk.receive_messages().__aiter__()
            while True:
                if result_seen:
                    try:
                        msg = await asyncio.wait_for(it.__anext__(), timeout=0.05)
                    except (asyncio.TimeoutError, StopAsyncIteration):
                        break
                else:
                    try:
                        msg = await it.__anext__()
                    except StopAsyncIteration:
                        break
                if result_seen:
                    # Late-arriving message after this turn's ResultMessage —
                    # discard so it doesn't appear as the next turn's response.
                    continue
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
                    result_seen = True
                    # Continue iterating in drain mode to catch any late
                    # post-Result messages on this same iterator.
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

        Serialized via _submit_lock so that if a previous turn is still
        in-flight, this call interrupts it and waits for its consumer to
        finish (consuming the resulting ResultMessage and draining any tail)
        before issuing the new query. That ordering is what prevents stale
        SDK messages from surfacing as the next turn's response.
        """
        with self._submit_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("ClaudeChat session is closed")
                prev_future = self._stream_future
                prev_done = self._done_event

            if prev_future is not None and not prev_future.done():
                # Ask the SDK to end the in-flight turn cleanly so the
                # consumer can drain the tail and exit on its own.
                try:
                    int_fut = asyncio.run_coroutine_threadsafe(
                        self._sdk.interrupt(), self._loop
                    )
                    try:
                        int_fut.result(timeout=2)
                    except Exception:
                        pass
                except Exception:
                    pass
                settled = prev_done.wait(timeout=5) if prev_done is not None else False
                if not settled and not prev_future.done():
                    # Interrupt didn't take — fall back to hard cancel.
                    prev_future.cancel()

            with self._lock:
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

    def interrupt(self) -> None:
        """Best-effort cancel of the in-flight stream WITHOUT closing the SDK.

        Used by the "Clear chat" button so an abandoned turn (and any tool
        calls it would have made) actually stops on the server side, instead
        of leaking until the next user prompt.

        Serialized via ``_submit_lock`` so we can't race ``submit()`` and end
        up SDK-interrupting the *next* turn after this one has already
        completed (the SDK's ``interrupt()`` cancels whatever is currently
        active, not a specific future).
        """
        with self._submit_lock:
            with self._lock:
                if self._closed or self._loop is None or self._sdk is None:
                    return
                future = self._stream_future
                done = self._done_event
            if future is None or future.done():
                return
            try:
                int_fut = asyncio.run_coroutine_threadsafe(
                    self._sdk.interrupt(), self._loop
                )
                try:
                    int_fut.result(timeout=2)
                except Exception:
                    pass
            except Exception:
                pass
            # Only wait/cancel if it's still the same in-flight turn.
            with self._lock:
                still_current = future is self._stream_future
            if not still_current:
                return
            if done is not None:
                done.wait(timeout=5)
            if not future.done():
                future.cancel()

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
        # Detach lifetime hooks now that we've cleaned up explicitly.
        try:
            if getattr(self, "_finalizer", None) is not None:
                self._finalizer.detach()
        except Exception:
            pass
        try:
            cb = getattr(self, "_atexit_cb", None)
            if cb is not None:
                atexit.unregister(cb)
        except Exception:
            pass


# ── Streamlit chat UI ─────────────────────────────────────────


@st.cache_resource(show_spinner="Connecting to Claude Code…")
def _get_chat_singleton() -> "ClaudeChat":
    """Return the process-wide ClaudeChat singleton.

    Cached at process scope (not per-browser-session), so a hard refresh or
    a second tab reuses the same SDK connection and the same `claude` child
    subprocess instead of spawning a new one and orphaning the old one.

    On a streamlit *process* restart the singleton is rebuilt and resumes
    the previous SDK session via the persisted session_id (if any).
    """
    history = _load_chat_history()
    resume_id = _load_chat_meta().get("session_id")
    return ClaudeChat(resume_session_id=resume_id, chat_history=history)


def _get_or_recreate_chat() -> "ClaudeChat | None":
    """Return a live ClaudeChat, recreating the singleton if its background
    thread died or it was explicitly closed.

    Cache is cleared BEFORE attempting to close the dead instance so that a
    hung close() never blocks recreation. If close() can't bring the thread
    down, we force-shutdown its resources directly to avoid leaking a
    `claude` subprocess.
    """
    chat = _get_chat_singleton()
    if chat.is_alive() and not chat.is_closed:
        return chat

    dead = chat
    _get_chat_singleton.clear()
    try:
        dead.close()
    except Exception:
        pass
    if dead._thread is not None and dead._thread.is_alive():
        # close() did not bring the daemon thread down — fall back to a
        # direct teardown so the SDK subprocess doesn't outlive us.
        try:
            _shutdown_chat_resources(dead._loop, dead._sdk, dead._thread)
        except Exception:
            pass
    return _get_chat_singleton()


def render():
    """Render the Claude Code Chat UI component (always visible at top of page)."""
    st.subheader("Claude Code Chat")
    st.caption(
        "Interactive chat with Claude Code (native, powered by Claude Agent SDK)."
    )

    # Initialize chat history (load from disk on first session)
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = _load_chat_history()

    # Resolve the process-wide ClaudeChat singleton. Cached failures avoid
    # blocking 30s on every rerun when the CLI isn't authenticated.
    if not st.session_state.get("chat_connect_failed"):
        try:
            st.session_state.chat_session = _get_or_recreate_chat()
            st.session_state.chat_connect_failed = False
        except Exception as exc:
            st.warning(
                f"Could not start Claude Code chat session: {exc}\n\n"
                "Make sure Claude Code CLI is authenticated:\n"
                f"```\ndocker exec -it {CONTAINER_NAME} claude\n```"
            )
            try:
                _get_chat_singleton.clear()
            except Exception:
                pass
            st.session_state.chat_session = None
            st.session_state.chat_connect_failed = True
    elif "chat_session" not in st.session_state:
        st.session_state.chat_session = None

    if st.session_state.get("chat_connect_failed"):
        if st.button("Retry connection", key="retry_chat"):
            st.session_state.chat_connect_failed = False
            st.rerun()

    # Initialize streaming state
    if "chat_streaming" not in st.session_state:
        st.session_state.chat_streaming = False

    # Chat-only 1s polling refresh — ONLY while a turn is in flight. The
    # ClaudeChat singleton runs the SDK on a background daemon thread, so
    # nothing about the streaming actually depends on streamlit reruns;
    # this timer just drives `session.poll()` to surface streamed chunks
    # to the UI. When the stream ends we stop registering it, leaving the
    # global 60s refresh as the only timer. That guarantees the global
    # tick never has to interrupt or accelerate the chat flow.
    if st.session_state.get("chat_streaming"):
        st_autorefresh(interval=1_000, key="chat_poll_refresh")
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
            # Capture session_id for resumption (per-session_state + on disk
            # so the cache_resource singleton can resume after a process
            # restart).
            if session.session_id:
                st.session_state.chat_session_id = session.session_id
                _save_chat_session_id(session.session_id)
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

    # Clear chat button — fully discard the current SDK session (interrupt any
    # in-flight turn, close the singleton, wipe persisted session_id) so the
    # next rerun spawns a brand-new `claude` session with no resumed context.
    if st.session_state.chat_messages:
        if st.button("Clear chat", key="clear_chat"):
            if st.session_state.chat_streaming and session is not None:
                try:
                    session.interrupt()
                except Exception:
                    pass
            # Tear down the cached singleton so a fresh ClaudeChat is built.
            try:
                _get_chat_singleton.clear()
            except Exception:
                pass
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
                if session._thread is not None and session._thread.is_alive():
                    try:
                        _shutdown_chat_resources(
                            session._loop, session._sdk, session._thread
                        )
                    except Exception:
                        pass
            # Wipe persisted resume id so the new singleton starts fresh.
            try:
                _write_json_atomic(CHAT_META_PATH, {}, indent=2)
            except Exception:
                pass
            st.session_state.chat_messages = []
            _save_chat_history([])
            st.session_state.chat_streaming = False
            st.session_state.chat_stream_text = ""
            st.session_state.chat_stream_events = []
            st.session_state.pop("chat_session", None)
            st.session_state.pop("chat_session_id", None)
            st.session_state.pop("chat_connect_failed", None)
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
