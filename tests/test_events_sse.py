"""SSE endpoint: frame format and config sanity.

The long-lived streaming behaviour (keepalives, disconnect handling) is
verified manually against a running server -- it doesn't exercise cleanly
through the in-process test client, whose ASGI transport never reports a
client disconnect, so the generator's `while True` would block the test.
The broker itself is covered in test_events_broker.py.
"""
from app.api.admin import _SSE_KEEPALIVE_SECONDS, _sse_frame


def test_sse_frame_format():
    frame = _sse_frame("status", {"status": "running", "current_item": None})
    assert frame == 'event: status\ndata: {"status": "running", "current_item": null}\n\n'


def test_sse_frame_is_valid_for_each_event_type():
    for ev in ("status", "log", "stats_dirty"):
        frame = _sse_frame(ev, {"x": 1})
        assert frame.startswith(f"event: {ev}\n")
        assert frame.endswith("\n\n")


def test_keepalive_interval_is_reasonable():
    # Long enough not to be chatty, short enough to beat common proxy idle timeouts.
    assert 5 <= _SSE_KEEPALIVE_SECONDS <= 55
