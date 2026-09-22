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

from app.shared import _write_json_atomic
from app.data import (
    load_chat_sdk_settings as _load_chat_sdk_settings,
    save_portal_config as _save_portal_config,
)

# Pull the chat-path helpers + migrator from services/shared.py. We use
# the same sys.path bootstrap pattern as scripts/register_internal_agent.py
# so the bare-name `import shared` style is preserved and we end up with
# the *same* module instance the daemon uses (avoids the dual-cache
# problem of `import services.shared` + `import shared`).
import sys as _sys
from pathlib import Path as _Path

_services_dir = str(_Path(__file__).resolve().parent.parent / "services")
if _services_dir not in _sys.path:
    _sys.path.insert(0, _services_dir)
from shared import (  # noqa: E402
    EFFORT_LEVELS,
    PORTAL_MODEL_CHOICES,
    chat_history_path as _chat_history_path,
    ensure_chat_dir as _ensure_chat_dir,
    load_session_id as _shared_load_session_id,
    migrate_chat_layout as _migrate_chat_layout,
    save_session_id as _shared_save_session_id,
    sdk_buffer_size_kwargs as _sdk_buffer_size_kwargs,
    sdk_effort_kwargs as _sdk_effort_kwargs,
    FATAL_SDK_ERROR_HINTS as _FATAL_SDK_ERROR_HINTS,
)

# The portal is the "main" chat surface; everything lives under
# /agent/memory/chat/main/ (history, archive, main.session). See
# `shared.CHAT_DIR` for the canonical layout.
_PORTAL_CHAT_NAME = "main"
CHAT_HISTORY_PATH = str(_chat_history_path(_PORTAL_CHAT_NAME))
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLIJSONDecodeError,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent

# Failures that mean the `claude` subprocess / its stdout reader is gone. The
# SDK runs its stdout reader as a background task; when that task dies it
# pushes one `{"type": "error"}` then `{"type": "end"}` and exits, so every
# later turn on the same ClaudeSDKClient returns nothing (and can block on
# `receive_response()` forever). We flag the instance broken and let
# `_get_or_recreate_chat` rebuild it, resuming the persisted session_id.
#
# The isinstance tuple is only a fast path for errors raised directly at us
# (e.g. `connect()`/`query()` failures); `_FATAL_SDK_ERROR_HINTS` does the real
# work, because anything surfacing through the reader task arrives as a bare
# `Exception(str(e))` with the SDK error class stripped.
_FATAL_SDK_ERRORS = (CLIConnectionError, CLIJSONDecodeError, ProcessError)

SYSTEM_MD = Path("/agent/system.md")
CONSTITUTION_MD = Path("/agent/constitution.md")
PORTAL_CONFIG = Path("/agent/memory/portal_config.json")
CLAUDE_SYSTEM_PROMPT_MD = Path("/home/agent/claude-system-prompt.md")
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "myagent")


def _build_system_prompt(chat_history: list[dict] | None = None) -> str:
    """Build system prompt from system.md + constitution.md + public URL + optional chat history.

    Sections are wrapped in XML tags so that boundaries remain unambiguous when
    concatenated with other markdown content (e.g. by agent.sh). The first three
    sections (`agent_system_prompt`, `agent_constitution`, `public_url`) are
    kept in lock-step — same tag names, same order — with `heartbeat.sh`'s
    `build_system_prompt`, so all three entry points (heartbeat shell agent,
    chat tab, internal-agent daemon) hand the SDK an identically-shaped header.
    """
    parts: list[str] = []
    if SYSTEM_MD.exists():
        parts.append("<agent_system_prompt>")
        parts.append(SYSTEM_MD.read_text())
        parts.append("</agent_system_prompt>")
    if CONSTITUTION_MD.exists():
        parts.append("<agent_constitution>")
        parts.append(CONSTITUTION_MD.read_text())
        parts.append("</agent_constitution>")
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


# Move legacy chat files (memory/chat_history.json, memory/chat_meta.json,
# messages/internal/<name>/chat_history*.json, memory/sessions/internal/<name>.session)
# into the unified /agent/memory/chat/<name>/ layout. Idempotent — a
# sentinel inside CHAT_DIR makes subsequent calls cheap. We do this at
# module import time so the very first _load_chat_history() call hits
# the new path even if the daemon hasn't run yet.
try:
    _migrate_chat_layout()
    _ensure_chat_dir(_PORTAL_CHAT_NAME)
except Exception:
    # Non-fatal: chat will still work against the new path; the daemon's
    # own startup will retry the migration if anything was left behind.
    pass


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
    """Return ``{"session_id": <id-or-None>}`` for the portal chat.

    The session id is stored as a bare-string sidecar under
    /agent/memory/chat/main/main.session; this function preserves the
    legacy ``dict`` return shape used elsewhere in this module.
    """
    sid = _shared_load_session_id(_PORTAL_CHAT_NAME)
    return {"session_id": sid} if sid else {}


def _save_chat_session_id(session_id: str | None) -> None:
    """Persist the latest SDK session_id so the singleton can resume across
    streamlit process restarts."""
    if not session_id:
        return
    if _shared_load_session_id(_PORTAL_CHAT_NAME) == session_id:
        return
    _shared_save_session_id(_PORTAL_CHAT_NAME, session_id)


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
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sdk: ClaudeSDKClient | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._closed = False
        # Set when the SDK transport dies mid-turn (see _FATAL_SDK_ERRORS).
        # Makes is_alive() report False so the singleton gets rebuilt.
        self._broken = False
        self._resume_session_id = resume_session_id
        self._chat_history = chat_history or []
        # Connect-time SDK overrides: the SDK maps them onto `claude --model`
        # / `--effort` when it spawns the subprocess, so changing them on a
        # live client does nothing — the portal tears the singleton down and
        # rebuilds it instead (see `_teardown_chat_singleton`).
        self._model = model
        self._effort = effort
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

    def _build_options(self) -> ClaudeAgentOptions:
        """Build the SDK options for this session.

        Kept in lock-step with `services/internal_agent_chat.py::_build_options`
        — the two call sites are intentionally identical apart from the
        internal agents' `send_reply` MCP tool and appended system prompt.

        Split out of `_connect` so the options can be asserted in tests
        without spawning a `claude` subprocess.
        """
        return ClaudeAgentOptions(
            **_sdk_buffer_size_kwargs(ClaudeAgentOptions),
            # Both `model` and `effort` are consumed when the SDK spawns the
            # `claude` subprocess. `effort` is a newer option than `model`, so
            # it is passed through a field probe rather than unconditionally.
            **_sdk_effort_kwargs(ClaudeAgentOptions, self._effort),
            model=self._model,
            system_prompt=_build_system_prompt(self._chat_history),
            permission_mode="bypassPermissions",
            include_partial_messages=True,
            # Recent claude-agent-sdk versions default newer models (Opus
            # 4.7+) to `display: "omitted"` for extended-thinking output,
            # which silently drops ThinkingBlock content from the stream
            # entirely (no ThinkingBlock is ever constructed — see
            # message_parser.py). Explicitly request summarized thinking
            # text so the portal's "Thinking..." expander keeps working
            # regardless of which model the session resolves to.
            thinking={"type": "adaptive", "display": "summarized"},
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

    async def _connect(self) -> None:
        """Create and connect the SDK client."""
        self._sdk = ClaudeSDKClient(self._build_options())
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

    def _msg_to_events(self, msg, *, streamed_any: bool) -> tuple[list[dict], bool]:
        """Translate one SDK message into zero or more typed event dicts.

        Shared between `_async_stream` (in-turn) and `_async_drain` (Refresh
        button) so the two paths cannot disagree on event shape. Returns
        ``(events, new_streamed_any)``: events to enqueue, and the updated
        `streamed_any` flag that controls whether a final AssistantMessage
        TextBlock should be re-emitted (skip when deltas already covered it).
        """
        events: list[dict] = []
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
                    events.append({"type": "text", "text": text})
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    if not streamed_any:
                        events.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    events.append(
                        {
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": block.id,
                            "input": block.input,
                        }
                    )
                elif isinstance(block, ThinkingBlock) and block.thinking:
                    # Guard against empty-string ThinkingBlocks: some
                    # thinking-display configs (or a session without an
                    # explicit `thinking` option) can yield a ThinkingBlock
                    # with `thinking=""` — skip it rather than showing a
                    # blank "Thinking..." expander.
                    events.append({"type": "thinking", "text": block.thinking})
            streamed_any = False
        elif isinstance(msg, SystemMessage):
            events.append({"type": "system", "subtype": msg.subtype, "data": msg.data})
        elif isinstance(msg, ResultMessage):
            sid = getattr(msg, "session_id", None)
            if sid:
                with self._lock:
                    self._session_id = sid
            events.append(
                {
                    "type": "result",
                    "cost": msg.total_cost_usd,
                    "duration_ms": msg.duration_ms,
                    "is_error": msg.is_error,
                    "num_turns": msg.num_turns,
                    "session_id": self._session_id,
                }
            )
        return events, streamed_any

    # Drain window after the turn's ResultMessage. The SDK occasionally
    # pushes follow-up messages 100-1500 ms after the Result on the same
    # receive channel. A short window (the original 50 ms) stranded those
    # in the SDK's internal anyio memory channel, where the *next* turn's
    # iterator picked them up — surfacing as "response to message N
    # appears at the start of turn N+1." 1.5 s catches the common case
    # while keeping turn-end latency bounded.
    _POST_RESULT_DRAIN_TIMEOUT_S = 1.5

    async def _async_stream(
        self, prompt: str, chunk_q: queue.Queue, done_event: threading.Event
    ) -> None:
        """Send prompt, push typed event dicts to *chunk_q*, set done_event when complete.

        Iterates `receive_messages()` directly (rather than `receive_response()`)
        so that after the turn's `ResultMessage` we can keep pulling on the
        *same* iterator for a short drain window — catching any late-arriving
        messages so they do not leak into the next turn. Opening a parallel
        iterator on the SDK's shared receive stream while a query is active
        would steal messages and race this loop, so the in-turn drain must
        reuse this iterator. (See `drain_pending` for the between-turn case.)
        """
        try:
            await self._sdk.query(prompt)
            streamed_any = False
            result_seen = False
            it = self._sdk.receive_messages().__aiter__()
            while True:
                if result_seen:
                    try:
                        msg = await asyncio.wait_for(
                            it.__anext__(),
                            timeout=self._POST_RESULT_DRAIN_TIMEOUT_S,
                        )
                    except (asyncio.TimeoutError, StopAsyncIteration):
                        break
                else:
                    try:
                        msg = await it.__anext__()
                    except StopAsyncIteration:
                        break
                events, streamed_any = self._msg_to_events(
                    msg, streamed_any=streamed_any
                )
                for ev in events:
                    chunk_q.put(ev)
                if isinstance(msg, ResultMessage):
                    result_seen = True
                    # Continue iterating in drain mode to catch any late
                    # post-Result messages on this same iterator.
        except Exception as exc:
            chunk_q.put({"type": "error", "error": self._describe_stream_error(exc)})
            self._mark_broken_if_fatal(exc)
        finally:
            done_event.set()

    @staticmethod
    def _is_fatal_stream_error(exc: Exception) -> bool:
        """True if *exc* means this SDK client can no longer serve turns."""
        if isinstance(exc, _FATAL_SDK_ERRORS):
            return True
        text = str(exc)
        return any(hint in text for hint in _FATAL_SDK_ERROR_HINTS)

    def _mark_broken_if_fatal(self, exc: Exception) -> None:
        """Flag the instance for rebuild when *exc* killed the transport."""
        if self._is_fatal_stream_error(exc):
            with self._lock:
                self._broken = True

    @classmethod
    def _describe_stream_error(cls, exc: Exception) -> str:
        """Human-facing text for a stream failure.

        The SDK's buffer-overflow message ("JSON message exceeded maximum
        buffer size of N bytes") is opaque on its own, so we say what
        actually happened and that the session reconnects itself.
        """
        text = str(exc)
        if "maximum buffer size" in text:
            return (
                "A tool returned more output than the chat transport could "
                "buffer, so this turn was cut short. Reconnecting — please "
                "send the message again, ideally asking for less output at "
                f"once.\n\n_({text})_"
            )
        if cls._is_fatal_stream_error(exc):
            return f"{text}\n\n_Reconnecting the chat session…_"
        return text

    async def _async_drain(self, total_timeout_s: float) -> list[dict]:
        """Pull any messages the SDK has buffered on its receive channel
        without sending a new query.

        Used by the Refresh button to recover late-arriving content that
        slipped past `_async_stream`'s post-result drain window. Must only
        run when no turn is in flight — otherwise it competes with the
        active stream's iterator on the same anyio memory channel.
        """
        events: list[dict] = []
        loop = asyncio.get_event_loop()
        deadline = loop.time() + total_timeout_s
        it = self._sdk.receive_messages().__aiter__()
        streamed_any = False
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                per_msg_timeout = min(0.2, remaining)
                try:
                    msg = await asyncio.wait_for(
                        it.__anext__(), timeout=per_msg_timeout
                    )
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
                new_events, streamed_any = self._msg_to_events(
                    msg, streamed_any=streamed_any
                )
                events.extend(new_events)
        except Exception as exc:
            events.append({"type": "error", "error": self._describe_stream_error(exc)})
            self._mark_broken_if_fatal(exc)
        return events

    # ── public API ────────────────────────────────────────────

    def send(self, prompt: str, timeout: float = 300) -> str:
        """Blocking send. Returns full response text.

        Does NOT hold `_lock` across the wait: `_async_send` takes the same
        non-reentrant lock on the loop thread to store the session_id, so
        holding it here would self-deadlock — and would also block
        `is_alive()` (which takes `_lock`) for the whole timeout.
        """
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

    def drain_pending(self, total_timeout_s: float = 2.0) -> list[dict]:
        """Drain SDK-buffered messages without sending a new query.

        Returns the typed event dicts collected (same shape as `poll()`).
        Safe to call only when no turn is in flight; while streaming, the
        active `_async_stream` owns the receive iterator and a parallel
        drain would steal messages. We hold `_submit_lock` so `submit()`
        cannot start a new turn mid-drain.
        """
        with self._submit_lock:
            if self.is_streaming():
                return []
            if self._closed or self._sdk is None:
                return []
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._async_drain(total_timeout_s), self._loop
                )
                return fut.result(timeout=total_timeout_s + 2.0)
            except Exception:
                return []

    def is_alive(self) -> bool:
        """Return True if the background thread is running AND the SDK
        transport is still usable.

        The daemon thread survives a dead `claude` subprocess, so thread
        liveness alone is not enough: without the `_broken` check the portal
        would keep handing prompts to a client whose stdout reader has already
        raised, and every turn would fail the same way.
        """
        with self._lock:
            if self._broken:
                return False
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
    sdk_settings = _load_chat_sdk_settings()
    return ClaudeChat(
        resume_session_id=resume_id,
        chat_history=history,
        model=sdk_settings.get("model"),
        effort=sdk_settings.get("effort"),
    )


def _teardown_chat_singleton(session: "ClaudeChat | None") -> None:
    """Drop the cached singleton and stop its SDK subprocess.

    Best-effort at every step: the caller's goal is always "the next rerun
    builds a fresh ClaudeChat", and a failure to close the old one must not
    prevent that. Used by both the Clear-chat button (which additionally wipes
    the persisted session id and history) and the session-settings handler
    (which keeps them, so the new client resumes the same conversation with
    the new connect-time options).
    """
    if session is not None and st.session_state.get("chat_streaming"):
        try:
            session.interrupt()
        except Exception:
            pass
    try:
        _get_chat_singleton.clear()
    except Exception:
        pass
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass
    if session._thread is not None and session._thread.is_alive():
        try:
            _shutdown_chat_resources(session._loop, session._sdk, session._thread)
        except Exception:
            pass


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
    # Salvage whatever the dead instance still holds — in the broken-transport
    # case its queue carries the "Reconnecting…" error event for the turn that
    # just failed. Without this, a full-page rerun that lands before the
    # streaming fragment ticks would drop the instance (and the explanation)
    # on the floor, leaving `chat_streaming` stuck True against a fresh
    # session that has never streamed.
    try:
        _drain_streaming_events(dead)
    except Exception:
        pass
    st.session_state.chat_streaming = False

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


def _drain_streaming_events(session) -> None:
    """Pull pending SDK events from `session` into the streaming buffers.

    Safe to call repeatedly; no-op if `session` is None. Does not commit
    to `chat_messages` or disk and does not rerun.
    """
    if session is None:
        return
    # `_get_or_recreate_chat` can call this before render() has initialized the
    # streaming buffers, so seed them here rather than assuming they exist.
    st.session_state.setdefault("chat_stream_events", [])
    st.session_state.setdefault("chat_stream_text", "")
    new_events = session.poll()
    for ev in new_events:
        st.session_state.chat_stream_events.append(ev)
        if ev["type"] == "text":
            st.session_state.chat_stream_text += ev["text"]
        elif ev["type"] == "error":
            st.session_state.chat_stream_text += f"\n\n**Error:** {ev['error']}"


# Event types that carry no visible content in the live chat bubble (see
# `_chat_stream_fragment`'s render loop, which only branches on
# "tool_use_group" / "thinking"). The SDK interleaves a SystemMessage
# ("status") between every consecutive tool call — one per tool, e.g.
# tool, status, tool, status, tool — so treating them as run-breakers would
# defeat the grouping almost entirely. They must stay transparent: skipped
# from the grouped output (nothing renders them anyway) and *not* counted
# as a break between tool_use runs.
_TRANSPARENT_EVENT_TYPES = {"system", "result", "error"}


def _group_tool_events(events: list[dict]) -> list[dict]:
    """Collapse consecutive ``tool_use`` events into a single grouped event.

    Long tool-call sequences (10+ in a row) otherwise render as one
    `st.caption` line each, flooding the chat UI with near-empty lines.
    This merges each *consecutive* run of ``tool_use`` events into one
    ``tool_use_group`` event carrying ``names``: an ordered list of
    ``(tool_name, count)`` pairs for that run. ``text``/``thinking`` events
    pass through unchanged and act as a break between runs, so interleaved
    tool calls (e.g. tool, thinking, tool) still show as two separate
    groups rather than merging across the thinking step. Events in
    `_TRANSPARENT_EVENT_TYPES` (system/result/error) are dropped entirely
    and do NOT break a run — see that constant's docstring for why.
    """
    grouped: list[dict] = []
    run: list[dict] = []

    def _flush() -> None:
        if not run:
            return
        counts: dict[str, int] = {}
        order: list[str] = []
        for ev in run:
            name = ev.get("name", "?")
            if name not in counts:
                order.append(name)
            counts[name] = counts.get(name, 0) + 1
        grouped.append(
            {
                "type": "tool_use_group",
                "names": [(n, counts[n]) for n in order],
                # Preserve the individual calls (name + input), in original
                # order, so the expander can list each invocation's params —
                # the count-collapsed `names` above is only for the summary
                # label.
                "calls": list(run),
            }
        )
        run.clear()

    for ev in events:
        ev_type = ev.get("type")
        if ev_type == "tool_use":
            run.append(ev)
        elif ev_type in _TRANSPARENT_EVENT_TYPES:
            continue
        else:
            _flush()
            grouped.append(ev)
    _flush()
    return grouped


def _format_tool_group_label(names: list[tuple]) -> str:
    """Render a `tool_use_group`'s ``names`` as e.g. ``Read ×3, Bash ×2``."""
    return ", ".join(f"{n} ×{c}" if c > 1 else n for n, c in names)


# Ordered param keys to surface per tool, so the expander detail reads like
# a natural function call (e.g. `Edit("path", "old", "new")`) instead of a
# raw dict dump. Tools not listed here fall back to all input values, in
# whatever order the SDK provided them.
_TOOL_PARAM_KEYS: dict[str, list[str]] = {
    "Read": ["file_path"],
    "Write": ["file_path", "content"],
    "Edit": ["file_path", "old_string", "new_string"],
    "Bash": ["command"],
    "BashOutput": ["bash_id"],
    "KillShell": ["shell_id"],
    "Grep": ["pattern", "path"],
    "Glob": ["pattern", "path"],
    "WebFetch": ["url", "prompt"],
    "WebSearch": ["query"],
    "TodoWrite": ["todos"],
    "Skill": ["skill", "args"],
}

_TOOL_PARAM_MAX_LEN = 60


def _format_tool_call(name: str, tool_input: dict | None) -> str:
    """Render one tool invocation as e.g. ``Read("path/to/file.md")``.

    Picks known param keys in a sensible order per tool (falls back to raw
    input values for unrecognized tools), truncates long values so a single
    huge file write / bash heredoc doesn't blow up the expander.
    """
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    keys = _TOOL_PARAM_KEYS.get(name)
    values = (
        [tool_input[k] for k in keys if k in tool_input]
        if keys
        else list(tool_input.values())
    )

    def _render_value(value) -> str:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        text = text.replace("\n", "\\n")
        if len(text) > _TOOL_PARAM_MAX_LEN:
            text = text[: _TOOL_PARAM_MAX_LEN - 1] + "…"
        return json.dumps(text)

    args = ", ".join(_render_value(v) for v in values)
    return f"{name}({args})"


def _commit_streaming_buffer() -> bool:
    """Flush `chat_stream_text` / `chat_stream_events` into `chat_messages`
    and disk, then clear the streaming buffers.

    Relaxes the empty-text case: if no text events arrived but tool/thinking
    events did, synthesize a short placeholder so the turn is still recorded
    in the visible history. Returns True if a record was appended.
    """
    full_text = st.session_state.chat_stream_text
    events = st.session_state.chat_stream_events
    if not full_text and events:
        tool_groups = [
            ev["names"]
            for ev in _group_tool_events(events)
            if ev["type"] == "tool_use_group"
        ]
        had_thinking = any(ev.get("type") == "thinking" for ev in events)
        bits: list[str] = []
        if had_thinking:
            bits.append("_(thinking only)_")
        if tool_groups:
            label = ", ".join(_format_tool_group_label(names) for names in tool_groups)
            bits.append("Used tools: " + label)
        if bits:
            full_text = " ".join(bits)
    committed = False
    if full_text:
        st.session_state.chat_messages.append(
            {"role": "assistant", "content": full_text}
        )
        _save_chat_history(st.session_state.chat_messages)
        committed = True
    st.session_state.chat_stream_text = ""
    st.session_state.chat_stream_events = []
    return committed


def _finalize_assistant_turn(session) -> bool:
    """If `session` has finished streaming, drain trailing events, commit
    the assembled reply, persist session_id, and flip `chat_streaming` off.

    Returns True if a record was committed. Does not rerun — the caller
    decides whether to trigger one.
    """
    if session is None or session.is_streaming():
        return False
    _drain_streaming_events(session)
    committed = _commit_streaming_buffer()
    if session.session_id:
        st.session_state.chat_session_id = session.session_id
        _save_chat_session_id(session.session_id)
    st.session_state.chat_streaming = False
    return committed


@st.fragment(run_every="3s")
def _chat_stream_fragment():
    """Streaming poll + live-bubble render, scoped to a fragment.

    Only this region reruns every 3s while a turn is in flight, so the outer
    `st.tabs` widget in `server.py` keeps its identity and the active tab is
    not reset to the first one. `st.chat_input` is intentionally NOT in here
    — it must live at the top level of the page.

    Self-terminates when streaming flips to False: it does a final drain,
    persists history, then issues an app-scoped `st.rerun()` so the next
    render path skips this fragment entirely (the 3s timer stops).
    """
    session = st.session_state.get("chat_session")

    if st.session_state.get("chat_streaming") and session:
        _drain_streaming_events(session)
        if _finalize_assistant_turn(session):
            # App-scoped rerun so the next render() takes the non-fragment
            # path and the 3s polling timer stops.
            st.rerun(scope="app")
        elif not session.is_streaming():
            # Session ended with no text/tool/thinking content — nothing to
            # commit, but still flip the streaming flag and rerun so the
            # fragment unmounts.
            st.rerun(scope="app")

    # Live bubble — only while streaming.
    if st.session_state.get("chat_streaming"):
        with st.chat_message("assistant"):
            st.markdown(st.session_state.chat_stream_text or "Processing...")
            for ev in _group_tool_events(st.session_state.chat_stream_events):
                if ev["type"] == "tool_use_group":
                    label = "tools" if len(ev["names"]) > 1 else "tool"
                    with st.expander(
                        f"Used {label}: {_format_tool_group_label(ev['names'])}"
                    ):
                        st.markdown(
                            "\n".join(
                                f"- {_format_tool_call(c.get('name', '?'), c.get('input'))}"
                                for c in ev["calls"]
                            )
                        )
                elif ev["type"] == "thinking":
                    with st.expander("Thinking..."):
                        st.markdown(ev["text"])
            if not st.session_state.chat_stream_text:
                st.caption("Thinking...")
            else:
                st.caption("Streaming...")


_SDK_DEFAULT_LABEL = "SDK default"


def _label_index(labels: list[str], value: str | None) -> int:
    """Index of *value* in *labels*, falling back to the 'SDK default' entry."""
    try:
        return labels.index(value) if value else 0
    except ValueError:
        return 0


def _render_session_settings() -> None:
    """Model / effort selectors for the portal chat session.

    Both are *connect-time* SDK options (the SDK turns them into
    `claude --model` / `--effort` when it spawns the subprocess), so a change
    has to rebuild the client — we persist the choice, tear the singleton
    down, and rerun. The persisted resume id is deliberately left alone, so
    the rebuilt session picks the conversation back up.

    Change detection compares against a `st.session_state` sentinel holding
    the last applied pair rather than re-reading portal_config.json: the
    sentinel is updated before the rerun, so a stale mtime-cached read can
    never drive a rerun loop.
    """
    model_labels = [_SDK_DEFAULT_LABEL, *PORTAL_MODEL_CHOICES]
    effort_labels = [_SDK_DEFAULT_LABEL, *EFFORT_LEVELS]

    if "chat_sdk_applied" not in st.session_state:
        saved = _load_chat_sdk_settings()
        st.session_state.chat_sdk_applied = (saved.get("model"), saved.get("effort"))
    applied_model, applied_effort = st.session_state.chat_sdk_applied

    streaming = bool(st.session_state.get("chat_streaming"))
    with st.expander("⚙️ Session settings", expanded=False):
        col_model, col_effort = st.columns(2)
        with col_model:
            model_choice = st.selectbox(
                "Model",
                model_labels,
                index=_label_index(model_labels, applied_model),
                key="chat_model_select",
                disabled=streaming,
                help="Leave on 'SDK default' to let the SDK pick the model.",
            )
        with col_effort:
            effort_choice = st.selectbox(
                "Effort",
                effort_labels,
                index=_label_index(effort_labels, applied_effort),
                key="chat_effort_select",
                disabled=streaming,
                help="How much the model thinks per turn (claude --effort).",
            )
        st.caption(
            "Changing either setting reconnects the chat session — the "
            "conversation is preserved. With effort 'low' the model thinks "
            "minimally, so the 'Thinking…' section may be short or empty."
        )

    if streaming:
        return

    chosen = (
        None if model_choice == _SDK_DEFAULT_LABEL else model_choice,
        None if effort_choice == _SDK_DEFAULT_LABEL else effort_choice,
    )
    if chosen == (applied_model, applied_effort):
        return

    # Write only the key that actually changed. portal_config.json is shared
    # by every browser tab while `chat_sdk_applied` is per-session, so writing
    # both keys unconditionally would let a tab that only touched Effort
    # clobber another tab's Model back to the SDK default.
    if chosen[0] != applied_model:
        _save_portal_config("chat_model", chosen[0])
    if chosen[1] != applied_effort:
        _save_portal_config("chat_effort", chosen[1])
    # Update the sentinel BEFORE the rerun so this branch cannot re-fire.
    st.session_state.chat_sdk_applied = chosen
    _teardown_chat_singleton(st.session_state.get("chat_session"))
    st.session_state.chat_session = None
    st.session_state.chat_connect_failed = False
    st.rerun()


def render():
    """Render the Claude Code Chat UI component (always visible at top of page)."""
    st.subheader("Claude Code Chat")
    st.caption(
        "Interactive chat with Claude Code (native, powered by Claude Agent SDK)."
    )

    # Rendered before the singleton is resolved so a just-changed setting
    # tears the old client down instead of connecting with the stale one.
    _render_session_settings()

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

    # Chat polling cadence is driven by the enclosing `st.fragment` (run_every
    # = "3s" while streaming, None when idle). The ClaudeChat singleton runs
    # the SDK on a background daemon thread, so nothing about streaming
    # depends on streamlit reruns; the timer just drives `session.poll()` to
    # surface streamed chunks to the UI. Scoping the rerun to this fragment
    # prevents the outer `st.tabs` widget from being re-identified and
    # snapping back to the first tab during a stream.
    if "chat_stream_text" not in st.session_state:
        st.session_state.chat_stream_text = ""
    if "chat_stream_events" not in st.session_state:
        st.session_state.chat_stream_events = []

    session = st.session_state.chat_session

    # Display chat history (top-level — never reruns on the 3s tick).
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # While a turn is in flight, mount the streaming fragment. Its 3s
    # `run_every` reruns *only* the fragment, so the outer `st.tabs` in
    # server.py keeps its widget identity and the active tab is preserved.
    if st.session_state.chat_streaming:
        _chat_stream_fragment()
    elif st.session_state.chat_stream_text or st.session_state.chat_stream_events:
        # Self-heal: a previous turn left text/events in the buffer without
        # finalizing (e.g. fragment stopped firing before the SDK ended).
        # Commit them now so they appear in this render's history list.
        # Guard: only commit when the SDK session also reports done, to
        # avoid materializing a partial buffer that a still-running turn
        # is about to extend.
        sess = st.session_state.get("chat_session")
        if sess is None or not sess.is_streaming():
            if _commit_streaming_buffer():
                st.rerun()

    # Clear chat button — fully discard the current SDK session (interrupt any
    # in-flight turn, close the singleton, wipe persisted session_id) so the
    # next rerun spawns a brand-new `claude` session with no resumed context.
    # Both Clear and Refresh only appear once the user has actually started a
    # chat (in-memory list is non-empty).
    clear_clicked = False
    refresh_clicked = False
    if st.session_state.chat_messages:
        col_clear, col_refresh, _ = st.columns([1, 1, 8])
        with col_clear:
            clear_clicked = st.button("Clear chat", key="clear_chat")
        with col_refresh:
            refresh_clicked = st.button(
                "Refresh",
                key="refresh_chat",
                help=(
                    "Drain any pending streamed events from the SDK, commit "
                    "a finished turn, and reload chat history from disk."
                ),
            )
    if clear_clicked:
        # Tear down the cached singleton so a fresh ClaudeChat is built.
        _teardown_chat_singleton(session)
        # Wipe persisted resume id so the new singleton starts fresh.
        try:
            from shared import session_path as _session_path

            p = _session_path(_PORTAL_CHAT_NAME)
            if p.exists():
                p.write_text("")
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
    if refresh_clicked:
        # Step 1: drain any pending SDK events into the streaming buffers,
        # then finalize if the turn has completed. This is what the fragment
        # would normally do on its 3s tick — making Refresh do it lets the
        # user manually pull a stuck/late reply without sending again.
        committed = False
        if session is not None:
            _drain_streaming_events(session)
            # If no turn is in flight, also drain the SDK's own internal
            # receive channel — that's where late post-Result messages
            # park if they slipped past _async_stream's drain window.
            # Without this, the reply to message N would only surface
            # when the user sends message N+1.
            if not session.is_streaming():
                leftover = session.drain_pending(total_timeout_s=2.0)
                for ev in leftover:
                    st.session_state.chat_stream_events.append(ev)
                    if ev["type"] == "text":
                        st.session_state.chat_stream_text += ev["text"]
                    elif ev["type"] == "error":
                        st.session_state.chat_stream_text += (
                            f"\n\n**Error:** {ev['error']}"
                        )
            committed = _finalize_assistant_turn(session)
        # Step 2: reconcile in-memory chat_messages with disk. Prefer the
        # longer list to avoid wiping a just-appended record before its
        # disk write has propagated.
        disk = _load_chat_history()
        in_mem = st.session_state.chat_messages
        if len(disk) > len(in_mem):
            st.session_state.chat_messages = disk
            meta_sid = _load_chat_meta().get("session_id")
            if meta_sid and meta_sid != st.session_state.get("chat_session_id"):
                st.session_state.chat_session_id = meta_sid
            st.toast(f"Loaded {len(disk)} messages from disk.")
            st.rerun()
        elif len(in_mem) > len(disk):
            _save_chat_history(in_mem)
            st.toast(f"Saved {len(in_mem)} messages to disk.")
            st.rerun()
        elif committed:
            st.rerun()
        elif st.session_state.chat_streaming:
            st.toast("Still streaming — buffered events drained.")
        elif disk != in_mem:
            st.session_state.chat_messages = disk
            st.toast(f"Reloaded {len(disk)} messages from disk.")
            st.rerun()
        else:
            st.toast("No new messages.")

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
