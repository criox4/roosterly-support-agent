"""Client for the Roosterly helpdesk API.

Handles the helpdesk's failure modes:
- 429: wait Retry-After seconds, retry.
- 503 / network errors: exponential backoff with jitter, retry (the API saves nothing on a 503).
- 409: never retried; surfaced to the caller as "already handled".
- other 4xx: raised immediately, retrying would not help.
A client-side throttle keeps us under the 5 requests/second limit in the first place.
"""
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, status, payload):
        super().__init__(f"helpdesk {status}: {payload}")
        self.status, self.payload = status, payload


class Helpdesk:
    MIN_INTERVAL = 0.25  # 4 req/s, under the 5 req/s limit
    MAX_TRIES = 8
    RETRYABLE = (None, 502, 503, 504)  # None = network error / timeout

    def __init__(self):
        self.base = os.environ["HELPDESK_URL"].rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {os.environ['HELPDESK_KEY']}",
            "X-Candidate-Email": os.environ["CANDIDATE_EMAIL"],
            "Content-Type": "application/json",
        }
        if os.environ.get("CANDIDATE_NAME"):
            self.headers["X-Candidate-Name"] = os.environ["CANDIDATE_NAME"]
        self._last_call = 0.0
        self.retries = 0

    def _send(self, method, path, body):
        wait = self._last_call + self.MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"raw": raw.decode(errors="replace")[:300]}
            return e.code, payload, e.headers
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return None, {"error": "network", "message": str(e)}, {}

    def call(self, method, path, body=None):
        for attempt in range(1, self.MAX_TRIES + 1):
            status, data, headers = self._send(method, path, body)
            if status is not None and status < 300:
                return data
            if status == 429:
                try:
                    delay = float(headers.get("Retry-After") or 1)
                except ValueError:
                    delay = 1.0
            elif status in self.RETRYABLE:
                delay = min(8.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.25)
            else:
                raise ApiError(status, data)
            if attempt == self.MAX_TRIES:
                raise ApiError(status, data)
            self.retries += 1
            time.sleep(delay)

    # ---- endpoints

    def all_tickets(self):
        """Every ticket in the current set (open and handled), following the cursor."""
        tickets, cursor = [], None
        while True:
            path = "/tickets?limit=10&status=all"
            if cursor:
                path += "&cursor=" + urllib.parse.quote(cursor)
            page = self.call("GET", path)
            tickets += page["tickets"]
            cursor = page.get("next_cursor")
            if not cursor:
                return tickets

    def ticket(self, tid):
        return self.call("GET", f"/tickets/{urllib.parse.quote(tid)}")

    def examples(self):
        data = self.call("GET", "/examples")
        return data["examples"] if isinstance(data, dict) else data

    def reset(self):
        return self.call("POST", "/practice/reset")

    def decide(self, tid, action, payload):
        """Post one decision. Returns 'posted' or 'already_handled'.

        Exactly-once: re-check the ticket right before posting and never post if it is handled.
        If a POST times out after the server saved it, our retry gets a 409: the decision we
        sent is the one that was saved, so that also counts as done, never as a second answer.
        """
        assert action in ("reply", "handoff", "close")
        if self.ticket(tid).get("handled"):
            return "already_handled"
        try:
            self.call("POST", f"/tickets/{urllib.parse.quote(tid)}/{action}", payload)
            return "posted"
        except ApiError as e:
            if e.status == 409:
                return "already_handled"
            raise
