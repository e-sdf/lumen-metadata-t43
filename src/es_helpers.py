"""Small helper for querying the GoTriple Elasticsearch cluster from notebooks.

Credentials live in .env (which is git-ignored), never in the notebook itself:

    ES_HOST=https://lumen-es-route-lumen-gotriple-production.apps.bst2.paas.psnc.pl
    ES_USER=...
    ES_PASS=...
"""

import collections
import copy
import os
import time

import requests

DOCUMENTS_INDEX = "triple-documents-prod"

# .env sits at the project root, one level above this package. Anchoring to
# __file__ keeps it findable whether the caller is main.py, a notebook, or a
# script started from somewhere else entirely.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")


def load_env(path=ENV_PATH):
    """Read KEY=VALUE lines from .env into os.environ without adding a
    python-dotenv dependency. Existing environment variables win."""
    if not os.path.exists(path):
        return
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class ElasticGatewayError(RuntimeError):
    """The route in front of Elasticsearch gave up (502/503/504) - the request
    was too slow, not malformed. Callers can retry with a smaller page."""


# The route returns an HTML error page rather than JSON when it times out, so
# these have to be recognised before any attempt to parse the body.
GATEWAY_STATUSES = (502, 503, 504)

# Every query actually sent, newest last, so a notebook or a debugging session
# can show the real request instead of a retyped approximation.
QUERY_LOG = collections.deque(maxlen=200)


def last_query(index_contains=None):
    """The most recent query sent, optionally the most recent for one index."""
    for entry in reversed(QUERY_LOG):
        if index_contains is None or index_contains in entry["index"]:
            return entry
    return None


def es_search(body, index=DOCUMENTS_INDEX, timeout=120, retries=2) -> dict:
    """POST a search body and return the parsed response, raising on an
    Elasticsearch-level error rather than returning a half-empty dict.

    Always returns a dict or raises - never None, so callers can subscript the
    result directly.
    """
    load_env()
    QUERY_LOG.append({"index": index, "body": copy.deepcopy(body)})
    host = os.environ.get("ES_HOST")
    user = os.environ.get("ES_USER")
    password = os.environ.get("ES_PASS") or os.environ.get("ES_PASSWORD")

    if not host:
        raise RuntimeError("ES_HOST is not set - add it to .env (see es_helpers.py docstring)")

    url = f"{host.rstrip('/')}/{index}/_search"

    for attempt in range(retries + 1):
        response = requests.post(
            url, json=body, auth=(user, password) if user else None, timeout=timeout
        )

        if response.status_code in GATEWAY_STATUSES:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise ElasticGatewayError(
                f"Elasticsearch route returned {response.status_code} after {retries + 1} attempts "
                f"(size={body.get('size')}). The query was too slow for the gateway - "
                f"retry with a smaller page size."
            )

        # Anything non-JSON is an error page from the route, not a search result.
        # Parsing it blindly produces an unreadable JSONDecodeError.
        if "application/json" not in response.headers.get("Content-Type", ""):
            snippet = " ".join(response.text.split())[:200]
            raise RuntimeError(f"Elasticsearch returned {response.status_code} ({snippet})")

        payload = response.json()

        if "error" in payload:
            reason = payload["error"].get("root_cause", [{}])[0].get("reason", payload["error"])
            raise RuntimeError(f"Elasticsearch {response.status_code}: {reason}")
        response.raise_for_status()

        return payload

    # Unreachable: the last attempt either returns or raises above. Stating it
    # explicitly keeps the return type a plain dict instead of dict | None,
    # which is what makes every es_search(...)["hits"] a type error.
    raise RuntimeError(f"Elasticsearch gave no usable response after {retries + 1} attempts")


# The `license` field exists on nearly every document, but ~36.8M of them carry
# the placeholder value "undefined". A document only really has a license when
# the field exists AND is not that placeholder.
HAS_REAL_LICENSE = {
    "bool": {
        "must": [{"exists": {"field": "license"}}],
        "must_not": [{"term": {"license": "undefined"}}],
    }
}

HAS_ORIGINAL_LICENSE = {"exists": {"field": "original_license"}}
