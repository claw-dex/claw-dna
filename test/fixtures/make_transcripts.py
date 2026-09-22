#!/usr/bin/env python3
"""Generate the synthetic cycle transcripts used by the usage-handler tests.

    uv run python test/fixtures/make_transcripts.py

Why synthetic: real `/agent/memory/transcripts/cycle-*.jsonl` files contain the
agent's full reasoning, working directories, and branch names. They are not
committable, but the parser in `services/metrics/usage.py` depends on the exact
record shape the Claude CLI writes, so the fixtures reproduce that shape field
for field with fabricated content.

Structure reproduced from real transcripts (CLI v2.1.223):

* record `type`s: queue-operation, last-prompt, user, attachment, assistant
* assistant records carry `message.usage`; nothing else does
* **one API response spans several assistant records** — one per content block
  (thinking / text / tool_use) — all sharing `message.id`, `requestId` and an
  identical `usage` object. Real ratio was 22 records for 12 responses.
* every tool_use is answered by a `user` record holding a `tool_result` block
* `isSidechain` is present, `effort` rides on the assistant record, and
  `usage.service_tier` / `usage.speed` sit inside the usage block

The generator also emits `manifest.json` with the totals it *intended* to
write, computed as it builds each response. The test asserts the parser
reproduces those numbers, so it is checked against generator intent rather than
against itself.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "transcripts"

VERSION = "2.1.223"
CWD = "/agent"
GIT_BRANCH = "main"
MODEL = "claude-sonnet-5"
SESSION_A = "11111111-2222-3333-4444-555555555555"
SESSION_B = "66666666-7777-8888-9999-000000000000"


def _usage(
    *,
    input_tokens,
    cache_creation,
    cache_read,
    output_tokens,
    service_tier="standard",
    speed="standard",
    web_search=0,
    web_fetch=0,
    ephemeral_1h=0,
):
    """A usage block in the exact shape the API returns."""
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output_tokens,
        "server_tool_use": {
            "web_search_requests": web_search,
            "web_fetch_requests": web_fetch,
        },
        "service_tier": service_tier,
        "cache_creation": {
            "ephemeral_1h_input_tokens": ephemeral_1h,
            "ephemeral_5m_input_tokens": cache_creation - ephemeral_1h,
        },
        "inference_geo": "not_available",
        "iterations": [],
        "speed": speed,
    }


def _base(session_id, uuid, parent_uuid, ts):
    return {
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "userType": "external",
        "cwd": CWD,
        "sessionId": session_id,
        "version": VERSION,
        "gitBranch": GIT_BRANCH,
        "entrypoint": "sdk-cli",
        "uuid": uuid,
        "timestamp": ts,
    }


class Builder:
    """Accumulates transcript records and the totals they should produce."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.records = []
        self.responses = []  # one entry per distinct API response
        self._n = 0
        self._parent = None

    # -- helpers ----------------------------------------------------------
    def _uuid(self, tag):
        self._n += 1
        return f"{self.session_id[:8]}-{tag}-{self._n:04d}"

    def _append(self, record):
        self.records.append(record)
        return record

    # -- record kinds -----------------------------------------------------
    def queue_operation(self, ts, content):
        return self._append(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": ts,
                "sessionId": self.session_id,
                "content": content,
            }
        )

    def last_prompt(self, leaf_uuid):
        return self._append(
            {
                "type": "last-prompt",
                "leafUuid": leaf_uuid,
                "sessionId": self.session_id,
            }
        )

    def user_prompt(self, ts, text):
        uuid = self._uuid("user")
        record = self._append(
            {
                **_base(self.session_id, uuid, self._parent, ts),
                "type": "user",
                "message": {"role": "user", "content": text},
                "promptId": self._uuid("prompt"),
                "promptSource": "sdk",
                "permissionMode": "bypassPermissions",
            }
        )
        self._parent = uuid
        return record

    def attachment(self, ts):
        uuid = self._uuid("attach")
        record = self._append(
            {
                **_base(self.session_id, uuid, self._parent, ts),
                "type": "attachment",
                "attachment": {
                    "type": "deferred_tools_delta",
                    "addedNames": ["Read", "Edit"],
                    "removedNames": [],
                    "readdedNames": [],
                    "addedLines": 2,
                    "pendingMcpServers": [],
                    "needsAuthMcpServers": [],
                },
            }
        )
        self._parent = uuid
        return record

    def response(self, ts, usage, blocks, *, effort="medium", model=MODEL):
        """One API response, written as one assistant record per content block.

        This is the shape that makes naive summing wrong: every record repeats
        the same `usage`, so N blocks would count the response N times.
        """
        message_id = self._uuid("msg").replace("-msg-", "-msg_")
        request_id = message_id.replace("msg_", "req_")
        stop_reason = "tool_use" if blocks[-1]["type"] == "tool_use" else "end_turn"

        for block in blocks:
            uuid = self._uuid("asst")
            self._append(
                {
                    **_base(self.session_id, uuid, self._parent, ts),
                    "type": "assistant",
                    "requestId": request_id,
                    "effort": effort,
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [block],
                        "stop_reason": stop_reason,
                        "stop_sequence": None,
                        "stop_details": None,
                        "usage": usage,
                    },
                }
            )
            self._parent = uuid

        self.responses.append(
            {
                "message_id": message_id,
                "ts": ts,
                "day": ts[:10],
                "model": model,
                "effort": effort,
                "usage": usage,
                "records": len(blocks),
            }
        )
        return message_id

    def tool_result(self, ts, tool_use_id, output):
        uuid = self._uuid("user")
        record = self._append(
            {
                **_base(self.session_id, uuid, self._parent, ts),
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": output,
                        }
                    ],
                },
                "toolUseResult": {"stdout": output, "stderr": "", "interrupted": False},
                "sourceToolAssistantUUID": self._parent,
            }
        )
        self._parent = uuid
        return record


def _thinking(text):
    return {"type": "thinking", "thinking": text, "signature": "sig-placeholder"}


def _text(text):
    return {"type": "text", "text": text}


def _tool_use(tool_id, name, tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _turn(builder, index, ts, usage, *, final=False):
    """One assistant turn: a thinking+tool_use response, then its tool result.

    A final turn ends with a text block instead, mirroring `end_turn`.
    """
    tool_id = f"toolu_{index:04d}"
    if final:
        builder.response(ts, usage, [_thinking(f"Wrapping up step {index}.")])
        builder.response(ts, usage, [_text(f"Completed step {index}.")])
        return
    builder.response(
        ts,
        usage,
        [
            _thinking(f"Considering step {index}."),
            _tool_use(
                tool_id, "Read", {"file_path": f"/agent/memory/file-{index}.json"}
            ),
        ],
    )
    builder.tool_result(ts, tool_id, f"contents of file-{index}")


def build_first_cycle():
    """A normal cycle: 12 responses across 22 assistant records, like the real one."""
    b = Builder(SESSION_A)
    b.queue_operation("2026-05-01T08:58:16.503Z", "run the next evolve cycle")
    b.user_prompt("2026-05-01T08:58:20.000Z", "Run the next evolve cycle.")
    b.attachment("2026-05-01T08:58:21.000Z")

    # 10 two-block responses + 2 single-block ones = 22 records / 12 responses.
    for i in range(1, 11):
        _turn(
            b,
            i,
            f"2026-05-01T08:{58 + (i // 6):02d}:{(29 + i * 3) % 60:02d}.000Z",
            _usage(
                input_tokens=2,
                cache_creation=1000 + i * 10,
                cache_read=100_000 + i * 500,
                output_tokens=80 + i,
            ),
        )
    _turn(
        b,
        11,
        "2026-05-01T09:00:18.536Z",
        _usage(
            input_tokens=3, cache_creation=1118, cache_read=109_612, output_tokens=391
        ),
        final=True,
    )
    b.last_prompt(b.records[-1]["uuid"])
    return b


def build_resumed_cycle(first: Builder):
    """The next cycle of the same goal — heartbeat.sh resumes the session.

    `heartbeat.sh:483` passes `-r $SESSION_ID` for an in-progress goal and
    `heartbeat.sh:561` copies the whole session file, so this transcript is a
    **superset** of the previous one. Counting the two files independently
    double-counts everything in the overlap.
    """
    b = Builder(SESSION_A)
    b.records = list(first.records)  # the resumed history, verbatim
    b.responses = list(first.responses)
    b._n = first._n
    b._parent = first.records[-1].get("uuid")

    b.user_prompt("2026-05-01T09:15:00.000Z", "Continue the goal.")
    for i in range(12, 15):
        _turn(
            b,
            i,
            f"2026-05-01T09:{15 + i - 12:02d}:00.000Z",
            _usage(
                input_tokens=4,
                cache_creation=2000 + i,
                cache_read=200_000 + i,
                output_tokens=150 + i,
                web_search=1 if i == 13 else 0,
            ),
        )
    b.last_prompt(b.records[-1]["uuid"])
    return b


def build_other_session():
    """An unrelated later cycle: new session, priority tier, a second model."""
    b = Builder(SESSION_B)
    b.queue_operation("2026-05-02T10:00:00.000Z", "run diagnostics")
    b.user_prompt("2026-05-02T10:00:05.000Z", "Run the self-test suite.")
    b.response(
        "2026-05-02T10:00:10.000Z",
        _usage(
            input_tokens=5,
            cache_creation=500,
            cache_read=50_000,
            output_tokens=60,
            service_tier="priority",
            ephemeral_1h=100,
        ),
        [_thinking("Checking."), _tool_use("toolu_9001", "Bash", {"command": "ls"})],
    )
    b.tool_result("2026-05-02T10:00:11.000Z", "toolu_9001", "ok")
    b.response(
        "2026-05-02T10:00:20.000Z",
        _usage(input_tokens=6, cache_creation=600, cache_read=60_000, output_tokens=70),
        [_text("All checks passed.")],
        effort="high",
        model="claude-opus-5",
    )
    b.last_prompt(b.records[-1]["uuid"])
    return b


def _expected(cycles):
    """Totals the parser should produce, derived from generator intent.

    Deduplicates on message_id across cycles, oldest first — exactly the rule
    services/metrics/usage.py implements, computed here independently of it.
    """
    seen = set()
    per_cycle = {}
    per_day = {}
    totals = {
        "requests": 0,
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "web_search_requests": 0,
        "web_fetch_requests": 0,
    }
    models = set()
    assistant_records = 0

    for cycle, builder in cycles:
        assistant_records += sum(
            1 for r in builder.records if r.get("type") == "assistant"
        )
        cycle_bucket = per_cycle.setdefault(cycle, {"requests": 0, "output_tokens": 0})
        for response in builder.responses:
            if response["message_id"] in seen:
                continue
            seen.add(response["message_id"])
            usage = response["usage"]
            cycle_bucket["requests"] += 1
            cycle_bucket["output_tokens"] += usage["output_tokens"]
            day = per_day.setdefault(
                response["day"], {"requests": 0, "output_tokens": 0, "cycles": set()}
            )
            day["requests"] += 1
            day["output_tokens"] += usage["output_tokens"]
            day["cycles"].add(cycle)
            models.add(response["model"])

            totals["requests"] += 1
            for field in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            ):
                totals[field] += usage[field]
            totals["web_search_requests"] += usage["server_tool_use"][
                "web_search_requests"
            ]
            totals["web_fetch_requests"] += usage["server_tool_use"][
                "web_fetch_requests"
            ]

    totals["total_tokens"] = sum(
        totals[f]
        for f in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    )
    return {
        "assistant_records": assistant_records,
        "totals": totals,
        "models": sorted(models),
        "per_cycle": {str(k): v for k, v in per_cycle.items()},
        "per_day": {
            day: {
                "requests": v["requests"],
                "output_tokens": v["output_tokens"],
                "cycles": len(v["cycles"]),
            }
            for day, v in per_day.items()
        },
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in OUT_DIR.glob("cycle-*"):
        stale.unlink()

    first = build_first_cycle()
    resumed = build_resumed_cycle(first)
    other = build_other_session()

    cycles = [(9001, first), (9002, resumed), (9003, other)]
    for cycle, builder in cycles:
        body = "\n".join(json.dumps(r) for r in builder.records) + "\n"
        if cycle == 9003:
            # Older transcripts are gzipped by heartbeat.sh after 7 days.
            # mtime=0 keeps the output byte-identical across regenerations —
            # gzip stamps the current time into its header otherwise, which
            # would show up as a spurious diff every time this is re-run.
            (OUT_DIR / f"cycle-{cycle}.jsonl.gz").write_bytes(
                gzip.compress(body.encode(), mtime=0)
            )
        else:
            (OUT_DIR / f"cycle-{cycle}.jsonl").write_text(body)

    manifest = _expected(cycles)
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"wrote {len(cycles)} transcripts to {OUT_DIR}")
    print(json.dumps(manifest["totals"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
