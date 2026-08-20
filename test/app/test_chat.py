"""Tests for app/chat.py — pure tool-call grouping/formatting helpers.

Streamlit-rendering paths (`_chat_stream_fragment`, `render`) are not
exercised here; they call into Streamlit primitives that require a running
session/fragment context. We unit-test the pure helpers that decide *what*
gets grouped and how it's formatted, and rely on `scripts/app_check.py` for
full-render smoke testing.

Regression coverage: `_group_tool_events` must merge consecutive tool calls
into one displayed block even when the SDK interleaves invisible
`SystemMessage` ("status") events between them — this is the *actual*
message sequence observed from a live `ClaudeSDKClient` session driving
several Bash/Grep/Read tool calls in a row. An earlier fix that only
tested a clean `tool, tool, thinking, tool` sequence (no `system` events)
passed its unit test but did not fix the live bug, because `SystemMessage`
was being treated as a run-breaker. These tests pin the real interleave
so that regression cannot recur silently.
"""

from __future__ import annotations

from app.chat import (
    ClaudeChat,
    _format_tool_call,
    _format_tool_group_label,
    _group_tool_events,
)


def test_consecutive_tool_calls_merge_into_one_group():
    events = [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.md"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "b.md"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    ]
    grouped = _group_tool_events(events)
    assert len(grouped) == 1
    assert grouped[0]["type"] == "tool_use_group"
    assert _format_tool_group_label(grouped[0]["names"]) == "Read ×2, Bash"
    assert len(grouped[0]["calls"]) == 3


def test_system_status_events_do_not_break_a_tool_run():
    """Regression: the SDK emits `SystemMessage(subtype="status")` between
    every consecutive tool call in real usage (tool, status, tool, status,
    tool, ...). These must stay invisible to the grouping — they are never
    rendered by the live-bubble loop, so letting them break a run defeats
    the merge almost entirely (one tool call per line again).
    """
    events = [
        {"type": "tool_use", "name": "Bash", "input": {"command": "echo one"}},
        {"type": "system", "subtype": "status", "data": {}},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "foo"}},
        {"type": "system", "subtype": "status", "data": {}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.md"}},
        {"type": "system", "subtype": "status", "data": {}},
    ]
    grouped = _group_tool_events(events)
    assert len(grouped) == 1
    group = grouped[0]
    assert group["type"] == "tool_use_group"
    assert _format_tool_group_label(group["names"]) == "Bash, Grep, Read"
    assert [c["name"] for c in group["calls"]] == ["Bash", "Grep", "Read"]


def test_result_and_error_events_are_also_transparent():
    events = [
        {"type": "tool_use", "name": "Bash", "input": {"command": "cmd1"}},
        {"type": "result", "cost": 0.01},
        {"type": "tool_use", "name": "Bash", "input": {"command": "cmd2"}},
        {"type": "error", "error": "transient"},
    ]
    grouped = _group_tool_events(events)
    assert len(grouped) == 1
    assert _format_tool_group_label(grouped[0]["names"]) == "Bash ×2"


def test_thinking_still_breaks_a_tool_run():
    """Unlike system/result/error, `thinking` IS rendered (its own
    expander) — it must remain a real separator between tool bursts."""
    events = [
        {"type": "tool_use", "name": "Bash", "input": {"command": "cmd1"}},
        {"type": "thinking", "text": "pausing to think"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x.md"}},
    ]
    grouped = _group_tool_events(events)
    assert [g["type"] for g in grouped] == [
        "tool_use_group",
        "thinking",
        "tool_use_group",
    ]


def test_text_still_breaks_a_tool_run():
    events = [
        {"type": "tool_use", "name": "Bash", "input": {"command": "cmd1"}},
        {"type": "text", "text": "now let me check the other file"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x.md"}},
    ]
    grouped = _group_tool_events(events)
    assert [g["type"] for g in grouped] == [
        "tool_use_group",
        "text",
        "tool_use_group",
    ]


def test_format_tool_call_known_tools_render_like_function_calls():
    assert _format_tool_call("Read", {"file_path": "path/to/file.md"}) == (
        'Read("path/to/file.md")'
    )
    assert (
        _format_tool_call(
            "Edit",
            {
                "file_path": "path/to/file.md",
                "old_string": "foo",
                "new_string": "bar",
            },
        )
        == 'Edit("path/to/file.md", "foo", "bar")'
    )
    assert _format_tool_call("Bash", {"command": "gh auth status"}) == (
        'Bash("gh auth status")'
    )


def test_format_tool_call_truncates_long_values():
    rendered = _format_tool_call("Bash", {"command": "x" * 200})
    assert rendered.startswith('Bash("')
    assert len(rendered) < 100


def test_format_tool_call_unknown_tool_falls_back_to_all_values():
    rendered = _format_tool_call("SomeCustomTool", {"a": 1, "b": "hello"})
    assert rendered == 'SomeCustomTool("1", "hello")'


def test_format_tool_call_missing_input_renders_empty_parens():
    assert _format_tool_call("Read", None) == "Read()"


# ---------------------------------------------------------------------------
# Fatal-SDK-error classification.
#
# These are pure classmethods, deliberately tested against the *exact* strings
# the SDK produces. The SDK's stdout reader catches its own exception and
# re-raises it downstream as a bare `Exception(str(e))` (see
# claude_agent_sdk/_internal/query.py::_read_messages), so the original error
# class never reaches us and detection must be substring-based. An earlier fix
# classified by `isinstance` alone and was therefore a silent no-op — these
# tests pin the real wire text so that cannot recur.
#
# `CLIJSONDecodeError` formats as f"Failed to decode JSON: {line[:100]}..." and
# `ProcessError` as f"{message} (exit code: {n})".
# ---------------------------------------------------------------------------

_BUFFER_OVERFLOW_TEXT = (
    "Failed to decode JSON: JSON message exceeded maximum buffer size of "
    "1048576 bytes..."
)


def test_buffer_overflow_is_classified_fatal():
    assert ClaudeChat._is_fatal_stream_error(Exception(_BUFFER_OVERFLOW_TEXT))


def test_dead_subprocess_texts_are_classified_fatal():
    for text in (
        "Cannot write to terminated process",
        "Command failed with exit code -9 (exit code: -9)",
        "BrokenPipeError: [Errno 32] Broken pipe",
        "process is not running",
        "Not connected",
    ):
        assert ClaudeChat._is_fatal_stream_error(Exception(text)), text


def test_ordinary_errors_are_not_classified_fatal():
    for text in (
        "model refused to answer",
        "Tool execution failed: file not found",
        "rate limit exceeded",
    ):
        assert not ClaudeChat._is_fatal_stream_error(Exception(text)), text


def test_buffer_overflow_description_explains_the_cause():
    described = ClaudeChat._describe_stream_error(Exception(_BUFFER_OVERFLOW_TEXT))
    assert "more output than the chat transport could buffer" in described
    # The raw SDK text is retained so operators can still grep for it.
    assert "maximum buffer size" in described


def test_non_fatal_description_is_passed_through_verbatim():
    assert ClaudeChat._describe_stream_error(Exception("boom")) == "boom"
