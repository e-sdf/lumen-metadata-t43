"""Recover a DOI for documents whose `doi` field is empty or absent.

Cheapest evidence first. A regex over the whole landing page is never a source:
pages cite other papers. Bibliographic Crossref (title + author, no DOI in hand)
is also out — homonyms and preprint vs VoR. Those two are documented as limits,
not as routes.

A candidate is checked on Crossref *or* DataCite: institutional repositories
mint DataCite prefixes (e.g. 10.7939) that Crossref `/works/{doi}` 404s.
"""
from __future__ import annotations

import json
import re
import threading
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from urllib.parse import quote, urlparse, unquote

from bs4 import BeautifulSoup

from src.functions_license import (
    ELASTIC_INDEX,
    MIN_PAGE_SIZE,
    OAI_PATHS,
    PAGE_SIZE,
    api_session,
    extract_handle,
    parse_size,
    resolve_handle,
    session,
)
from src.rate_limit import CONTACT_EMAIL, polite_get

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
DC_IDENTIFIER = "{http://purl.org/dc/elements/1.1/}identifier"

# Hosts whose HTML is a known dead end in the licence pipeline (Cloudflare 403).
# Provider APIs below are used instead; we never GET the article HTML there.
SKIP_HTML_HOSTS = ("doaj.org",)

_crossref_work_cache = {}
_crossref_work_lock = threading.Lock()
_datacite_work_cache = {}
_datacite_work_lock = threading.Lock()


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _strings(value):
    for item in _as_list(value):
        text = str(item).strip()
        if text:
            yield text


def document_title(doc):
    """The first headline string, enough to check a Crossref hit is the same work."""
    for item in _as_list(doc.get("headline")):
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
        else:
            text = str(item).strip()
        if text:
            return text
    return ""


def first_http_url(doc):
    for field in ("main_entity_of_page", "url"):
        for text in _strings(doc.get(field)):
            if text.startswith("http"):
                return text.strip("[]'\"")
    return None


def normalise_doi(value):
    """Strip resolver prefixes and trailing punctuation. None if it is not a DOI."""
    text = unquote(str(value or "")).strip()
    text = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", text, flags=re.I)
    text = text.strip().strip(".,;)]}>\"'")
    match = DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(0).rstrip(".,;)]}>\"'")
    # ISSNs look like 10.xxxx until the slash; require a real suffix.
    if "/" not in doi:
        return None
    return doi


def dois_in_text(*chunks):
    found, seen = [], set()
    for chunk in chunks:
        for match in DOI_RE.finditer(unquote(str(chunk or ""))):
            doi = normalise_doi(match.group(0))
            if doi and doi not in seen:
                seen.add(doi)
                found.append(doi)
    return found


def classify_doi_problem(doc):
    """Empty-string vs missing field — the analysis keeps them separate."""
    raw = doc.get("doi")
    if raw is None:
        return "field absent"
    values = [str(v).strip() for v in _as_list(raw)]
    if not values:
        return "field absent"
    if any(normalise_doi(v) for v in values):
        return "ok"
    if all(v == "" for v in values):
        return "empty string"
    return "unusable"


def parse_doi_from_record(doc):
    """Route 0: no HTTP. The DOI is often already in another field or in the URL."""
    chunks = []
    for field in ("identifier", "main_entity_of_page", "url"):
        chunks.extend(_strings(doc.get(field)))
    # A stored `doi` of "" is not evidence; skip it.
    return dois_in_text(*chunks)


def _hal_id(doc):
    blob = " ".join([str(doc.get("id") or ""), first_http_url(doc) or ""])
    match = re.search(r"hal-(\d+)", blob, re.I)
    return f"hal-{match.group(1)}" if match else None


def _doaj_id(doc):
    blob = " ".join([str(doc.get("id") or ""), first_http_url(doc) or ""])
    match = re.search(r"doaj\.org(?:/article|:article)[:/]([a-f0-9]+)", blob, re.I)
    return match.group(1) if match else None


def fetch_doi_from_hal(hal_id):
    """HAL's own search API, not the landing page. The HTML is JS-heavy and slow."""
    try:
        res = polite_get(
            api_session,
            "https://api.archives-ouvertes.fr/search/",
            timeout=20,
            params={"q": f'halId_s:"{hal_id}"', "fl": "doiId_s,title_s", "wt": "json", "rows": 1},
        )
        if res is None or res.status_code != 200:
            return []
        docs = (res.json().get("response") or {}).get("docs") or []
        if not docs:
            return []
        return dois_in_text(*_as_list(docs[0].get("doiId_s")))
    except Exception:
        return []


def fetch_doi_from_doaj(article_id):
    """DOAJ HTML is Cloudflare-blocked in the licence pipeline. The JSON API is not."""
    try:
        res = polite_get(
            api_session,
            f"https://doaj.org/api/v3/articles/{article_id}",
            timeout=20,
        )
        if res is None or res.status_code != 200:
            return []
        identifiers = (res.json().get("bibjson") or {}).get("identifier") or []
        dois = []
        for entry in identifiers:
            if str(entry.get("type") or "").lower() == "doi":
                if doi := normalise_doi(entry.get("id")):
                    dois.append(doi)
        return dois
    except Exception:
        return []


def fetch_doi_from_oai(url):
    """Same OAI GetRecord path as licence recovery, reading dc:identifier."""
    handle = extract_handle(url)
    if not handle:
        return []

    target = resolve_handle(handle) or str(url)
    host = urlparse(target).netloc
    if not host:
        return []

    for path in OAI_PATHS:
        try:
            res = polite_get(
                api_session,
                f"https://{host}{path}",
                timeout=25,
                params={
                    "verb": "GetRecord",
                    "metadataPrefix": "oai_dc",
                    "identifier": f"oai:{host}:{handle}",
                },
            )
            if res is None or res.status_code != 200 or "<error" in res.text:
                continue
            root = ET.fromstring(res.content)
            values = [e.text.strip() for e in root.iter(DC_IDENTIFIER) if e.text and e.text.strip()]
            if dois := dois_in_text(*values):
                return dois
        except Exception:
            continue
    return []


def _dois_from_jsonld(node):
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("doi", "@id", "sameAs", "identifier", "url", "id"):
                found.extend(dois_in_text(value if isinstance(value, str) else json.dumps(value)))
            found.extend(_dois_from_jsonld(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_dois_from_jsonld(item))
    elif isinstance(node, str):
        found.extend(dois_in_text(node))
    return found


def scrape_doi_from_html(url):
    """Meta tags and JSON-LD only. The body cites other papers; we do not search it.

    Handle URLs are resolved first: hdl.handle.net often 500s, the IR landing
    page is where citation_doi actually lives.
    """
    if handle := extract_handle(url):
        url = resolve_handle(handle) or url
    host = urlparse(url).netloc.lower()
    if any(blocked in host for blocked in SKIP_HTML_HOSTS):
        return [], "skipped: doaj html"
    if url.lower().split("?")[0].endswith(".pdf"):
        return [], "skipped: pdf"

    try:
        response = polite_get(session, url, timeout=15, allow_redirects=True, stream=True)
        if response is None:
            return [], "connection failed"
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type:
            response.close()
            return [], "skipped: not HTML"
        if response.status_code == 429:
            response.close()
            return [], "rate limited"
        if response.status_code not in (200, 202):
            response.close()
            return [], f"HTTP {response.status_code}"

        soup = BeautifulSoup(response.text, "html.parser")
    except Exception:
        return [], "connection failed"

    ordered = []

    for name in ("citation_doi", "dc.identifier", "prism.doi"):
        tag = soup.find("meta", attrs={"name": lambda value, n=name: bool(value) and value.lower() == n})
        if tag and tag.get("content"):
            ordered.extend(dois_in_text(tag.get("content")))

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        ordered.extend(_dois_from_jsonld(payload))

    # Deduplicate, keep order: citation_doi first.
    seen, unique = set(), []
    for doi in ordered:
        if doi not in seen:
            seen.add(doi)
            unique.append(doi)
    return unique, "html meta/json-ld"


def fetch_crossref_work(doi):
    with _crossref_work_lock:
        if doi in _crossref_work_cache:
            return _crossref_work_cache[doi]

    work = None
    try:
        res = polite_get(
            api_session,
            f"https://api.crossref.org/works/{doi}",
            timeout=15,
            params={"mailto": CONTACT_EMAIL},
        )
        if res is not None and res.status_code == 200:
            work = res.json().get("message") or {}
        elif res is not None and res.status_code == 404:
            work = {}
    except Exception:
        work = None

    with _crossref_work_lock:
        _crossref_work_cache[doi] = work
    return work


def titles_match(ours, theirs, threshold=0.55):
    a = re.sub(r"<[^>]+>", "", str(ours or "")).casefold().strip()
    b = re.sub(r"<[^>]+>", "", str(theirs or "")).casefold().strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def fetch_datacite_work(doi):
    """Institutional repositories mint DataCite DOIs. Crossref 404 is not 'not a DOI'."""
    key = str(doi).lower()
    with _datacite_work_lock:
        if key in _datacite_work_cache:
            return _datacite_work_cache[key]

    work = None
    try:
        res = polite_get(
            api_session,
            f"https://api.datacite.org/dois/{quote(doi, safe='')}",
            timeout=15,
        )
        if res is not None and res.status_code == 200:
            attrs = ((res.json().get("data") or {}).get("attributes") or {})
            titles = attrs.get("titles") or []
            title = ""
            if titles and isinstance(titles[0], dict):
                title = str(titles[0].get("title") or "").strip()
            work = {"title": [title] if title else []}
        elif res is not None and res.status_code == 404:
            work = {}
    except Exception:
        work = None

    with _datacite_work_lock:
        _datacite_work_cache[key] = work
    return work


def _title_from_work(work):
    titles = (work or {}).get("title") or [""]
    return titles[0] if titles else ""


def validate_doi(doi, title):
    """Accept if Crossref *or* DataCite knows the DOI and the titles are the same work."""
    crossref = fetch_crossref_work(doi)
    if crossref:
        theirs = _title_from_work(crossref)
        if title and not titles_match(title, theirs):
            return False, "title mismatch"
        return True, "crossref"

    datacite = fetch_datacite_work(doi)
    if datacite:
        theirs = _title_from_work(datacite)
        if title and not titles_match(title, theirs):
            return False, "title mismatch"
        return True, "datacite"

    if crossref is None and datacite is None:
        return False, "registry unreachable"
    return False, "not in Crossref or DataCite"


def recover_doi(doc):
    """Four routes, cheapest first. Sets doi_fix / doi_source on the document."""
    problem = classify_doi_problem(doc)
    doc["doi_problem"] = problem
    title = document_title(doc)
    url = first_http_url(doc)

    if problem == "ok":
        doc["doi_fix"] = normalise_doi(_as_list(doc.get("doi"))[0])
        doc["doi_source"] = "already present"
        return doc

    last_rejection = None

    def _accept(candidates, source):
        nonlocal last_rejection
        for doi in candidates:
            ok, reason = validate_doi(doi, title)
            if ok:
                doc["doi_fix"] = doi
                doc["doi_source"] = source
                return True
            last_rejection = f"rejected: {reason}"
        return False

    if _accept(parse_doi_from_record(doc), "parsed from url/identifier"):
        return doc

    if (hal_id := _hal_id(doc)) and _accept(fetch_doi_from_hal(hal_id), "hal api"):
        return doc

    if (doaj_id := _doaj_id(doc)) and _accept(fetch_doi_from_doaj(doaj_id), "doaj api"):
        return doc

    if url and extract_handle(url) and _accept(fetch_doi_from_oai(url), "oai dc:identifier"):
        return doc

    if not url:
        doc["doi_fix"] = ""
        doc["doi_source"] = "no landing page"
        return doc

    scraped, scrape_note = scrape_doi_from_html(url)
    if scraped and _accept(scraped, "html meta/json-ld"):
        return doc

    doc["doi_fix"] = ""
    if last_rejection:
        doc["doi_source"] = last_rejection
    elif scrape_note and scrape_note != "html meta/json-ld":
        doc["doi_source"] = scrape_note
    else:
        doc["doi_source"] = "not recovered"
    return doc


def build_doi_query(kind="empty_string"):
    """kind: empty_string | absent | unusable | with_landing_page."""
    empty = {"term": {"doi": ""}}
    absent = {"bool": {"must_not": [{"exists": {"field": "doi"}}]}}
    unusable = {"bool": {"should": [empty, absent], "minimum_should_match": 1}}
    has_page = {"exists": {"field": "main_entity_of_page"}}

    if kind == "empty_string":
        return empty
    if kind == "absent":
        return absent
    if kind == "unusable":
        return unusable
    if kind == "with_landing_page":
        return {"bool": {"must": [unusable, has_page]}}
    raise ValueError(f"Unknown doi filter '{kind}'")


def count_elastic_doi_documents(kind="empty_string", index=ELASTIC_INDEX):
    from src.es_helpers import es_search

    return es_search(
        {"size": 0, "track_total_hits": True, "query": build_doi_query(kind)},
        index=index,
        timeout=180,
    )["hits"]["total"]["value"]


DOI_SOURCE_FIELDS = [
    "id", "provider", "doi", "identifier", "main_entity_of_page",
    "url", "headline", "date_published",
]


def fetch_elastic_doi_documents(size="100", kind="empty_string", index=ELASTIC_INDEX):
    """search_after paging, same pattern as fetch_elastic_documents()."""
    from src.es_helpers import ElasticGatewayError, es_search

    target = parse_size(size)
    documents = []
    search_after = None
    page_size = min(PAGE_SIZE, 200)

    while target is None or len(documents) < target:
        body = {
            "size": page_size if target is None else min(page_size, target - len(documents)),
            "query": build_doi_query(kind),
            "_source": DOI_SOURCE_FIELDS,
            "sort": [{"id": "asc"}],
        }
        if search_after:
            body["search_after"] = search_after

        try:
            hits = es_search(body, index=index)["hits"]["hits"]
        except ElasticGatewayError:
            if page_size <= MIN_PAGE_SIZE:
                raise
            page_size = max(MIN_PAGE_SIZE, page_size // 2)
            print(f"  (elastic timed out, retrying with page size {page_size})")
            continue

        if not hits:
            break

        for hit in hits:
            source = hit["_source"]
            source.setdefault("id", hit["_id"])
            documents.append(source)

        search_after = hits[-1]["sort"]

    return documents


def fetch_elastic_doi_by_id(doc_id, index=ELASTIC_INDEX):
    """One document by `_id` or `id`, for the worked Handle / DataCite case."""
    from src.es_helpers import es_search

    for query in (
        {"ids": {"values": [doc_id]}},
        {"term": {"id": doc_id}},
    ):
        hits = es_search(
            {"size": 1, "query": query, "_source": DOI_SOURCE_FIELDS},
            index=index,
        )["hits"]["hits"]
        if hits:
            source = hits[0]["_source"]
            source.setdefault("id", hits[0]["_id"])
            return source
    return None
