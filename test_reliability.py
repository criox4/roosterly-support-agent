"""Offline checks for the helpdesk client's failure handling. Run: uv run python test_reliability.py"""
import os

os.environ.update(HELPDESK_URL="http://x", HELPDESK_KEY="k", CANDIDATE_EMAIL="e")
import helpdesk  # noqa: E402
from helpdesk import ApiError, Helpdesk  # noqa: E402

helpdesk.time.sleep = lambda s: None  # no real waiting
Helpdesk.MIN_INTERVAL = 0


def scripted(*responses):
    """A Helpdesk whose network layer replays (status, json, headers) tuples and records calls."""
    hd = Helpdesk()
    queue, hd.sent = list(responses), []

    def send(method, path, body):
        hd.sent.append((method, path))
        return queue.pop(0)
    hd._send = send
    return hd


OPEN = (200, {"id": "T1", "handled": None}, {})
DONE = (200, {"id": "T1", "handled": {"action": "reply"}}, {})

# 503 and 429 are retried, then the call succeeds
hd = scripted((503, {"error": "service_unavailable"}, {}), (429, {}, {"Retry-After": "1"}), (200, {"ok": 1}, {}))
assert hd.call("GET", "/status") == {"ok": 1} and hd.retries == 2

# a network failure (status None) is retried too
hd = scripted((None, {"error": "network"}, {}), (200, {"ok": 1}, {}))
assert hd.call("GET", "/status") == {"ok": 1}

# 400/422 are not retried
hd = scripted((422, {"error": "validation_failed"}, {}))
try:
    hd.call("POST", "/tickets/T1/reply", {})
    raise AssertionError("expected ApiError")
except ApiError as e:
    assert e.status == 422 and len(hd.sent) == 1

# a ticket that is already handled is never posted to
hd = scripted(DONE)
assert hd.decide("T1", "reply", {}) == "already_handled" and hd.sent == [("GET", "/tickets/T1")]

# POST hits a 503, the retry gets 409 (first attempt was saved after all): done, not an error, no 3rd POST
hd = scripted(OPEN, (503, {}, {}), (409, {"error": "already_handled"}, {}))
assert hd.decide("T1", "reply", {}) == "already_handled"
assert [m for m, _ in hd.sent] == ["GET", "POST", "POST"]

# happy path
hd = scripted(OPEN, (200, {"ok": True}, {}))
assert hd.decide("T1", "handoff", {}) == "posted"

print("reliability ok")
