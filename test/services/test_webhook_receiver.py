"""Tests for services/webhook_receiver.py — handler registry + dispatch."""

from __future__ import annotations

import io
import json
from email.message import Message

# ---------------------------------------------------------------------------
# _find_handler
# ---------------------------------------------------------------------------


class _Stub:
    def __init__(self, prefix):
        self.path_prefix = prefix
        self.calls = []

    def handle(self, req):
        self.calls.append(req.path)


def _swap_handlers(monkeypatch, wr, handlers):
    monkeypatch.setattr(wr, "HANDLERS", handlers)


def test_find_handler_exact_match(monkeypatch, patch_webhook_receiver_paths):
    wr = patch_webhook_receiver_paths
    h = _Stub("/foo")
    _swap_handlers(monkeypatch, wr, [h])

    assert wr._find_handler("/foo") is h


def test_find_handler_prefix_with_slash(monkeypatch, patch_webhook_receiver_paths):
    wr = patch_webhook_receiver_paths
    h = _Stub("/foo")
    _swap_handlers(monkeypatch, wr, [h])

    assert wr._find_handler("/foo/bar") is h


def test_find_handler_does_not_match_partial_word(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths
    h = _Stub("/foo")
    _swap_handlers(monkeypatch, wr, [h])

    assert wr._find_handler("/foobar") is None


def test_find_handler_skips_handlers_without_prefix(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths
    h_no_prefix = _Stub(None)
    h_real = _Stub("/foo")
    _swap_handlers(monkeypatch, wr, [h_no_prefix, h_real])

    assert wr._find_handler("/foo") is h_real


def test_find_handler_returns_none_for_unmatched(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths
    _swap_handlers(monkeypatch, wr, [_Stub("/foo")])
    assert wr._find_handler("/other") is None


# ---------------------------------------------------------------------------
# _write_heartbeat
# ---------------------------------------------------------------------------


def test_webhook_write_heartbeat(patch_webhook_receiver_paths):
    wr = patch_webhook_receiver_paths
    wr.HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    wr._write_heartbeat()
    # Writes a unix-epoch float as a string.
    float(wr.HEARTBEAT_FILE.read_text())


def test_webhook_write_heartbeat_swallows_oserror(
    monkeypatch, patch_webhook_receiver_paths, tmp_path
):
    wr = patch_webhook_receiver_paths
    monkeypatch.setattr(wr, "HEARTBEAT_FILE", tmp_path / "missing" / "hb")
    wr._write_heartbeat()  # no raise


# ---------------------------------------------------------------------------
# WebhookHandler — fake request/response plumbing
# ---------------------------------------------------------------------------


def _make_handler(wr, *, method="POST", path="/anything", body=b"", headers=None):
    """Build a WebhookHandler instance without calling its base __init__.

    BaseHTTPRequestHandler's __init__ expects a real socket; bypassing it lets
    us drive the helper methods directly.
    """
    h = wr.WebhookHandler.__new__(wr.WebhookHandler)
    h.command = method
    h.path = path
    h.client_address = ("127.0.0.1", 0)
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()

    msg = Message()
    msg["Content-Length"] = str(len(body))
    if headers:
        for k, v in headers.items():
            msg[k] = v
    h.headers = msg

    sent = {}

    def send_response(code):
        sent["code"] = code

    def send_header(k, v):
        sent.setdefault("headers", []).append((k, v))

    def end_headers():
        sent["end"] = True

    h.send_response = send_response
    h.send_header = send_header
    h.end_headers = end_headers
    h._sent = sent
    return h


def test_dispatch_to_subhandler_returns_false_when_no_match(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths
    _swap_handlers(monkeypatch, wr, [])
    h = _make_handler(wr, path="/none")
    assert h._dispatch_to_subhandler() is False


def test_dispatch_to_subhandler_returns_true_when_handler_runs(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths
    sub = _Stub("/foo")
    _swap_handlers(monkeypatch, wr, [sub])
    h = _make_handler(wr, path="/foo/extra")

    assert h._dispatch_to_subhandler() is True
    assert sub.calls == ["/foo/extra"]


def test_dispatch_to_subhandler_sends_500_on_handler_exception(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths

    class _Boom:
        path_prefix = "/foo"

        def handle(self, _req):
            raise RuntimeError("handler died")

    _swap_handlers(monkeypatch, wr, [_Boom()])

    surfaced = {}
    monkeypatch.setattr(
        wr,
        "surface_error",
        lambda *a, **kw: surfaced.setdefault("called", True),
    )

    h = _make_handler(wr, path="/foo")
    assert h._dispatch_to_subhandler() is True
    assert h._sent["code"] == 500
    assert surfaced.get("called") is True


def test_handle_write_request_persists_inbox_item(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths
    _swap_handlers(monkeypatch, wr, [])

    captured = {}

    def fake_write_to_inbox(items, *, dedup=True):
        captured["items"] = items
        captured["dedup"] = dedup
        return True

    monkeypatch.setattr(wr, "write_to_inbox", fake_write_to_inbox)

    body = json.dumps({"event": "ping"}).encode()
    h = _make_handler(
        wr,
        method="POST",
        path="/hook?source=ci",
        body=body,
        headers={"X-Custom": "yes"},
    )

    h._handle_write_request()
    assert h._sent["code"] == 200
    assert captured["dedup"] is False
    item = captured["items"][0]
    assert item["type"] == "event"
    assert item["source"] == "webhook"
    assert "POST /hook" in item["content"]
    assert "x-custom: yes" in item["content"].lower()


def test_handle_write_request_skips_sensitive_headers(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths
    _swap_handlers(monkeypatch, wr, [])

    captured = {}
    monkeypatch.setattr(
        wr,
        "write_to_inbox",
        lambda items, **kw: captured.setdefault("items", items) or True,
    )

    h = _make_handler(
        wr,
        method="POST",
        path="/hook",
        body=b"hi",
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "sess=abc",
            "X-Forwarded-For": "1.2.3.4",
            "X-Webhook-Secret": "shh",
            "X-Public": "ok",
        },
    )
    h._handle_write_request()

    content = captured["items"][0]["content"].lower()
    assert "authorization" not in content
    assert "cookie" not in content
    assert "x-forwarded-for" not in content
    assert "x-webhook-secret" not in content
    assert "x-public: ok" in content


def test_handle_write_request_returns_500_when_inbox_write_fails(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths
    _swap_handlers(monkeypatch, wr, [])
    monkeypatch.setattr(wr, "write_to_inbox", lambda items, **kw: False)

    h = _make_handler(wr, method="POST", path="/hook", body=b"{}")
    h._handle_write_request()
    assert h._sent["code"] == 500


def test_handle_read_request_returns_200_without_recording(
    monkeypatch, patch_webhook_receiver_paths
):
    wr = patch_webhook_receiver_paths
    _swap_handlers(monkeypatch, wr, [])

    called = {"write": 0}

    def fake_write(*_a, **_kw):
        called["write"] += 1
        return True

    monkeypatch.setattr(wr, "write_to_inbox", fake_write)

    h = _make_handler(wr, method="GET", path="/probe")
    h._handle_read_request()
    assert h._sent["code"] == 200
    assert called["write"] == 0
