import random
import threading
import time
from urllib.parse import urlparse

import requests

# ==========================================
# POLITE HTTP LAYER
# ==========================================
# The pipelines run many threads at once. Without coordination they all hit the
# same few hosts (api.crossref.org, doi.org, a handful of big publishers) and
# earn a 429. This module keeps a small state machine per host: a concurrency
# cap, a minimum gap between requests, and an interval that grows when the host
# pushes back.

# Set this to a real address: Crossref moves you to the faster "polite pool"
# when it can contact you, and stops throttling you as an anonymous client.
CONTACT_EMAIL = "paulo.pimenta@esciencefactory.com"
USER_AGENT = f"GoTriple-Metadata-Bot/1.0 (mailto:{CONTACT_EMAIL})"

DEFAULT_MIN_INTERVAL = 0.5   # seconds between two requests to the same host
DEFAULT_MAX_CONCURRENT = 2   # simultaneous requests to the same host
MAX_MIN_INTERVAL = 30.0      # ceiling for the adaptive backoff

# Hosts that need their own settings. Everything else uses the defaults above.
HOST_RULES = {
    "api.crossref.org": {"min_interval": 0.35, "max_concurrent": 2},
    "api.openalex.org": {"min_interval": 0.2, "max_concurrent": 2},
    "doi.org": {"min_interval": 0.5, "max_concurrent": 2},
    "pub.orcid.org": {"min_interval": 0.5, "max_concurrent": 2},
    "doaj.org": {"min_interval": 1.0, "max_concurrent": 1},
    "api.gotriple.eu": {"min_interval": 0.5, "max_concurrent": 2},
}


class _HostState:
    def __init__(self, min_interval, max_concurrent):
        self.lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(max_concurrent)
        self.min_interval = min_interval
        self.base_interval = min_interval
        self.next_allowed = 0.0  # time.monotonic() of the next permitted request


_states = {}
_states_lock = threading.Lock()


def _host_state(host):
    with _states_lock:
        if host not in _states:
            rules = HOST_RULES.get(host, {})
            _states[host] = _HostState(
                rules.get("min_interval", DEFAULT_MIN_INTERVAL),
                rules.get("max_concurrent", DEFAULT_MAX_CONCURRENT),
            )
        return _states[host]


def _wait_turn(state):
    """Block until this thread is allowed to send to the host, then reserve the
    next slot so other threads queue behind it."""
    while True:
        with state.lock:
            now = time.monotonic()
            if now >= state.next_allowed:
                state.next_allowed = now + state.min_interval
                return
            delay = state.next_allowed - now
        time.sleep(delay)


def _penalise(state, retry_after):
    """A host said 'slow down'. Double its interval (capped) so every later
    request to it, in any thread, is spaced further apart."""
    with state.lock:
        state.min_interval = min(state.min_interval * 2, MAX_MIN_INTERVAL)
        if retry_after:
            state.next_allowed = max(state.next_allowed, time.monotonic() + retry_after)


def _recover(state):
    """Successful request: creep back toward the host's normal pace."""
    with state.lock:
        if state.min_interval > state.base_interval:
            state.min_interval = max(state.base_interval, state.min_interval * 0.8)


def _retry_after_seconds(response, attempt):
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    # Exponential backoff with jitter when the host gives us no hint.
    return min(2 ** attempt, 30) + random.uniform(0, 1)


def polite_request(session, method, url, max_retries=3, **kwargs):
    """Throttled request. Retries 429/503 honouring Retry-After, and returns
    None if the host keeps refusing."""
    host = urlparse(url).netloc.lower()
    state = _host_state(host)

    for attempt in range(max_retries + 1):
        _wait_turn(state)
        with state.semaphore:
            try:
                response = session.request(method, url, **kwargs)
            except requests.exceptions.RequestException:
                raise

        if response.status_code in (429, 503):
            wait = _retry_after_seconds(response, attempt)
            _penalise(state, wait)
            if attempt == max_retries:
                return response
            time.sleep(wait)
            continue

        _recover(state)
        return response

    return None


def polite_get(session, url, **kwargs):
    return polite_request(session, "GET", url, **kwargs)


def polite_head(session, url, **kwargs):
    return polite_request(session, "HEAD", url, **kwargs)
