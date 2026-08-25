import re
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from src.functions_license_match import (
    apply_original_license,
    classify_original_license,
    metadata_has_cc,
)
from src.rate_limit import USER_AGENT, CONTACT_EMAIL, polite_get, polite_head

# Global session to maintain connection pooling
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5"
})

# Crossref rewards clients that identify themselves: an honest User-Agent with a
# contact address puts us in the polite pool instead of the throttled anonymous one.
api_session = requests.Session()
api_session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json"
})

# The same DOI shows up across records; never pay for it twice.
_crossref_cache = {}
_crossref_cache_lock = threading.Lock()
_openalex_cache = {}
_openalex_cache_lock = threading.Lock()
_handle_cache = {}
_handle_cache_lock = threading.Lock()

# Classic DSpace first, then the DSpace 7 layout (functions_name.py hits the same pair).
OAI_PATHS = ("/oai/request", "/server/oai/request")
DC_RIGHTS = "{http://purl.org/dc/elements/1.1/}rights"

ELASTIC_INDEX = "triple-documents-prod"
# Per Elasticsearch request, regardless of the total requested. 1,000 documents
# measures 15-18s against a route that gives up around 30s, so 500 keeps a
# margin; a page that still times out is halved down to MIN_PAGE_SIZE.
PAGE_SIZE = 500
MIN_PAGE_SIZE = 25

# `license` is an array, and a document can carry several of these at once
# (537,761 hold both "other" and "undefined"). They all mean the same thing —
# no usable license — so they form one case group rather than separate ones.
UNRESOLVED_LICENSE_VALUES = ["other", "undefined", ""]

# Filter names that select that whole group instead of a single literal value.
UNRESOLVED_FILTER_ALIASES = ("unresolved", "missing", "none", "unlicensed")


def is_unresolved_license(current_license):
    """True when a document's license array holds nothing usable: empty, absent,
    or any of "other" / "undefined" / "" in any combination."""
    if current_license is None:
        return True

    values = current_license if isinstance(current_license, list) else [current_license]
    if not values:
        return True

    return any(str(value).strip().lower() in UNRESOLVED_LICENSE_VALUES for value in values)

KNOWN_OA_PUBLISHERS = [
    "mdpi", "frontiers", "plos", "public library of science", 
    "biomed central", "hindawi", "open book publishers", 
    "copernicus publications", "peerj", "doaj"
]

def format_cc_license(text):
    if not isinstance(text, str):
        return text
    if text.startswith("[unknown"):
        return text
    match = re.search(r'creativecommons\.org/licenses/([a-z-]+)/([0-9.]+)', text, re.IGNORECASE)
    if match:
        return f"CC {match.group(1).upper()} {match.group(2)}"
    match_cc0 = re.search(r'creativecommons\.org/publicdomain/zero/([0-9.]+)', text, re.IGNORECASE)
    if match_cc0:
        return f"CC0 {match_cc0.group(1)}"

    # OpenAlex reports short codes without a version, e.g. "cc-by-nc-nd", "cc0".
    stripped = text.strip().lower()
    if stripped == "cc0":
        return "CC0"
    if re.fullmatch(r'cc-by(-nc)?(-nd|-sa)?', stripped):
        return "CC " + stripped[3:].upper().replace("-", "-")

    return text

def fetch_crossref_license(doi):
    with _crossref_cache_lock:
        if doi in _crossref_cache:
            return _crossref_cache[doi]

    result = None
    try:
        res = polite_get(
            api_session,
            f"https://api.crossref.org/works/{doi}",
            timeout=10,
            params={"mailto": CONTACT_EMAIL}
        )
        if res is not None and res.status_code == 200:
            licenses = res.json().get("message", {}).get("license", [])
            if licenses:
                result = licenses[0].get("URL", "[unknown, Crossref API returned no URL]")
    except Exception:
        pass

    with _crossref_cache_lock:
        _crossref_cache[doi] = result
    return result


def fetch_openalex_license(doi):
    """Second opinion for DOIs Crossref has no license for. OpenAlex aggregates
    OA status from many sources and returns short codes like "cc-by-nc"."""
    with _openalex_cache_lock:
        if doi in _openalex_cache:
            return _openalex_cache[doi]

    result = None
    try:
        res = polite_get(
            api_session,
            f"https://api.openalex.org/works/doi:{doi}",
            timeout=20,
            params={"mailto": CONTACT_EMAIL}
        )
        if res is not None and res.status_code == 200:
            work = res.json()
            for location in ("best_oa_location", "primary_location"):
                if licence := (work.get(location) or {}).get("license"):
                    result = licence
                    break
    except Exception:
        pass

    with _openalex_cache_lock:
        _openalex_cache[doi] = result
    return result


def extract_dois(doc):
    """DOIs the index already knows about, from `doi` and from `identifier`
    (which mixes DOIs with ISSNs and other identifiers)."""
    candidates = []
    for field in ("doi", "identifier"):
        values = doc.get(field) or []
        if not isinstance(values, list):
            values = [values]
        candidates.extend(str(v).strip() for v in values)

    seen = set()
    dois = []
    for value in candidates:
        if value.startswith("10.") and value not in seen:
            seen.add(value)
            dois.append(value)
    return dois


def extract_handle(url):
    """The Handle System id (e.g. "11568/867404") out of either a hdl.handle.net
    link or a repository's own /handle/ URL."""
    match = re.search(r'(?:hdl\.handle\.net|/handle)/([^?#\s]+)', str(url))
    return match.group(1).strip("/'\"]") if match else None


def resolve_handle(handle):
    """Ask the Handle System where a handle actually points. Cached: the same
    repository shows up across many documents."""
    with _handle_cache_lock:
        if handle in _handle_cache:
            return _handle_cache[handle]

    target = None
    try:
        res = polite_get(api_session, f"https://hdl.handle.net/api/handles/{handle}", timeout=20)
        if res is not None and res.status_code == 200:
            urls = [v["data"]["value"] for v in res.json().get("values", []) if v.get("type") == "URL"]
            target = urls[0] if urls else None
    except Exception:
        pass

    with _handle_cache_lock:
        _handle_cache[handle] = target
    return target


def _pick_rights(values):
    """Choose the most useful dc:rights value. Repositories emit several at once,
    e.g. ['info:eu-repo/semantics/openAccess', 'license:Creative commons',
    'license uri:http://creativecommons.org/licenses/by-nc-nd/4.0/']."""
    for value in values:
        if 'creativecommons.org' in value.lower() or 'rightsstatements.org' in value.lower():
            match = re.search(r'https?://\S+', value)
            return match.group(0) if match else value

    # "license uri:" is often an internal repository id rather than a URL
    # (e.g. "license uri:iris.pu00"), so only take it when it really is one.
    for value in values:
        if value.lower().startswith('license uri:'):
            candidate = value.split(':', 1)[1].strip()
            if candidate.startswith('http'):
                return candidate

    # Controlled vocabulary next: comparable across repositories, unlike the
    # free-text licence names each one invents.
    for value in values:
        if match := re.search(r'info:eu-repo/semantics/(\w+)', value):
            label = re.sub(r'(?<!^)(?=[A-Z])', ' ', match.group(1)).title()
            return f"{label} (OAI-PMH)"

    for value in values:
        if value.lower().startswith('license:'):
            return f"OAI-PMH: {value.split(':', 1)[1].strip()}"

    return f"OAI-PMH: {values[0]}" if values else None


def fetch_oai_rights(url):
    """Last resort for repository pages that block scrapers: their OAI-PMH
    endpoint is usually left open even when the web front end returns 403."""
    if isinstance(url, list):
        url = url[0] if url else None
    url = str(url).strip("[]'\"")

    handle = extract_handle(url)
    if not handle:
        return None

    target = resolve_handle(handle) or str(url)
    host = urlparse(target).netloc
    if not host:
        return None

    for path in OAI_PATHS:
        try:
            res = polite_get(api_session, f"https://{host}{path}", timeout=25, params={
                "verb": "GetRecord", "metadataPrefix": "oai_dc",
                "identifier": f"oai:{host}:{handle}"
            })
            if res is None or res.status_code != 200 or "<error" in res.text:
                continue

            root = ET.fromstring(res.content)
            values = [e.text.strip() for e in root.iter(DC_RIGHTS) if e.text and e.text.strip()]
            if values:
                return _pick_rights(values)
        except Exception:
            continue

    return None


def fetch_license_by_doi(dois):
    """Last resort when the landing page cannot be read: ask the metadata APIs.
    Crossref first (authoritative, richer URLs), then OpenAlex."""
    for doi in dois:
        if licence := fetch_crossref_license(doi):
            return licence
    for doi in dois:
        if licence := fetch_openalex_license(doi):
            return licence
    return None

def find_possible_license(url, publisher_name, original_license):
    if isinstance(url, list):
        url = url[0] if url else None
        
    url_str = str(url).strip("[]'\"").lower()
    
    if not url_str.startswith('http'):
        return "[unknown, no valid URL provided]"

    has_cc_in_metadata = metadata_has_cc(original_license)

    if publisher_name and any(oa_pub in str(publisher_name).lower() for oa_pub in KNOWN_OA_PUBLISHERS):
        return f"Open Access (Publisher: {publisher_name})"

    if ('hal.science' in url_str or 'hal.archives-ouvertes.fr' in url_str or '.hal.' in url_str) and not has_cc_in_metadata:
        return "HAL Authorisation"

    if 'doi.org/' in url_str:
        doi = url_str.split('doi.org/')[-1]
        if crossref_lic := fetch_crossref_license(doi):
            return crossref_lic

    try:
        # One streamed GET instead of HEAD + GET: half the requests per host, and
        # we still drop non-HTML before downloading the body.
        response = polite_get(session, url_str, timeout=15, allow_redirects=True, stream=True)
        if response is None:
            return "[unknown, connection failed]"

        if 'text/html' not in response.headers.get('Content-Type', '').lower():
            response.close()
            return "[unknown, skipped: not HTML]"

        if response.status_code == 429:
            response.close()
            return "[unknown, rate limited after retries]"

        if response.status_code not in [200, 202]:
            response.close()
            return f"[unknown, HTTP Error {response.status_code}]"

        soup = BeautifulSoup(response.text, 'html.parser')
        
        if meta_rights := soup.find('meta', attrs={'name': lambda x: x and x.lower() == 'dc.rights'}): # type: ignore
            return meta_rights.get('content', '')
            
        if link_license := soup.find('link', rel='license'):
            return link_license.get('href', '')

        for a_tag in soup.find_all('a', href=True):
            if 'creativecommons.org/licenses' in a_tag['href'].lower() or 'rightsstatements.org' in a_tag['href'].lower(): # type: ignore
                return a_tag['href'] 
        
        if cc_url_match := re.search(r'(https?://(?:www\.)?creativecommons\.org/licenses/[a-z0-9./-]+)', response.text, re.IGNORECASE):
            return cc_url_match.group(1)
            
        if cc_text := re.search(r'\b(CC[- ]BY[-\w]*|Creative Commons Attribution[-\w\s]*)\b', soup.get_text(separator=' '), re.IGNORECASE):
            return f"Text Match: {cc_text.group(0).strip()}"
            
        # Parenthesised rather than backslash-continued: a trailing comment after
        # a `\` breaks the continuation and is a syntax error.
        is_open_access = (
            soup.find(lambda tag: tag.has_attr('alt') and 'open access' in tag['alt'].lower())  # type: ignore
            or soup.find(lambda tag: tag.has_attr('class') and any('open-access' in c.lower() for c in tag.get('class', [])))  # type: ignore
            or re.search(r'<(meta|span|div)[^>]*content=["\']Open Access["\'][^>]*>', response.text, re.IGNORECASE)
        )
        if is_open_access:
            return "Generic Open Access"
            
        return "[unknown, no obvious license found]"
    except requests.exceptions.Timeout:
        return "[unknown, timeout]"
    except Exception:
        return "[unknown, connection failed]"

def process_document(doc, scrape=True):
    """Classify `original_license` first; scrape only what is still unresolved.

    scrape=False is the local step (minutes, no HTTP). scrape=True is step 1:
    page / Crossref / OAI, and only when original_license did not already yield
    a GoTriple `lic_*` code. An OA badge or HAL authorisation is access, not a
    recovered licence.
    """
    page_url = doc.get("main_entity_of_page")
    current_license = doc.get("license")
    classified = apply_original_license(doc)

    if not is_unresolved_license(current_license):
        doc["scrapped_license"] = "Already classified"
        return doc

    if classified["license_fix"]:
        doc["scrapped_license"] = classified["spdx"] or classified["license_fix"]
        return doc

    if not scrape:
        if classified["kind"] == "access":
            access = classified["conditions_of_access_fix"] or "openAccess"
            doc["scrapped_license"] = f"[access: {access}, not a licence]"
        elif classified["kind"] in ("repository", "copyright"):
            doc["scrapped_license"] = f"[{classified['kind']}, not a licence code]"
        elif classified["kind"] == "unmapped":
            doc["scrapped_license"] = "[unmapped original_license]"
        else:
            doc["scrapped_license"] = "[no original_license]"
        return doc

    result = find_possible_license(page_url, doc.get("publisher"), doc.get("original_license"))

    # Landing pages behind Cloudflare or an institutional block (403), and
    # pages with nothing useful in them, can still be resolved through the
    # metadata APIs when the index knows a DOI for the document.
    if str(result).startswith("[unknown"):
        if dois := extract_dois(doc):
            if licence := fetch_license_by_doi(dois):
                result = licence

    # Repository handles with no DOI: the OAI-PMH endpoint usually answers
    # even when the landing page blocks us.
    if str(result).startswith("[unknown"):
        if licence := fetch_oai_rights(page_url):
            result = licence

    if not str(result).startswith("[unknown"):
        scraped = classify_original_license(result)
        if scraped["license_fix"]:
            doc["license_fix"] = scraped["license_fix"]
            doc["spdx"] = scraped["spdx"] or ""
            doc["license_kind"] = scraped["kind"]
            doc["license_source"] = "scrape"
            doc["license_matched_from"] = scraped["matched_from"]
            if scraped["conditions_of_access_fix"] and not doc.get("conditions_of_access_fix"):
                doc["conditions_of_access_fix"] = scraped["conditions_of_access_fix"]
        elif scraped["kind"] == "access":
            doc["license_kind"] = "access"
            doc["license_source"] = "scrape (access, not a licence)"
            if scraped["conditions_of_access_fix"]:
                doc["conditions_of_access_fix"] = scraped["conditions_of_access_fix"]
        elif scraped["kind"] in ("repository", "copyright"):
            doc["license_kind"] = scraped["kind"]
            doc["license_source"] = f"scrape ({scraped['kind']}, not a licence)"

    doc["scrapped_license"] = format_cc_license(result)
    return doc

def parse_size(size):
    """A batch size, or None for "everything that matches".

    Accepts "all", "" and None as everything - the pipelines let the user press
    Enter to mean exactly that.
    """
    if size is None:
        return None
    text = str(size).strip().lower()
    if text in ("", "all", "*"):
        return None
    return int(text)


def count_elastic_documents(license_filter="unresolved", original_license="any",
                            index=ELASTIC_INDEX):
    """How many documents a filter matches, before committing to fetching them."""
    from src.es_helpers import es_search
    return es_search({"size": 0, "track_total_hits": True,
                      "query": build_license_query(license_filter,
                                                   original_license=original_license)},
                     index=index, timeout=180)["hits"]["total"]["value"]


def _has_original_license_clause():
    """A non-empty original_license. Empty string is treated as absent.

    `exists` is not enough on its own: harvest writes "" on some records, and
    Notion's empty case is missing-or-[]-or-"".
    """
    return {
        "bool": {
            "must": [{"exists": {"field": "original_license"}}],
            "must_not": [{"term": {"original_license": ""}}],
        }
    }


def build_license_query(license_filter="unresolved", original_license="any"):
    """Translate a license filter into an Elasticsearch query.

    "unresolved" selects the whole no-usable-license group: any of
    "other" / "undefined" / "" in the array, plus the 167 documents where the
    field is absent entirely. Any other value is matched literally.

    original_license: "any" | "present" | "absent". "present" is the local
    matching population (step 0). "absent" is the HTTP-only remainder (step 1).
    """
    if license_filter in (None, "any", "all"):
        base = {"match_all": {}}
    elif license_filter in UNRESOLVED_FILTER_ALIASES:
        base = {
            "bool": {
                "should": [
                    {"terms": {"license": UNRESOLVED_LICENSE_VALUES}},
                    {"bool": {"must_not": [{"exists": {"field": "license"}}]}}
                ],
                "minimum_should_match": 1
            }
        }
    else:
        base = {"term": {"license": license_filter}}

    if original_license in (None, "any", "all"):
        return base
    if original_license == "present":
        return {"bool": {"must": [base, _has_original_license_clause()]}}
    if original_license == "absent":
        return {"bool": {"must": [base], "must_not": [_has_original_license_clause()]}}
    raise ValueError(f"Unknown original_license filter '{original_license}'")


def fetch_elastic_documents(size="1000", license_filter="unresolved",
                            original_license="any", index=ELASTIC_INDEX,
                            source_fields=None):
    """Fetch documents straight from Elasticsearch instead of the GoTriple REST API.

    Pages with `search_after` on the `id` keyword field, so batches larger than
    the 10,000-hit window work. Returns the same `_source` dictionaries the
    GoTriple API returns, so process_document() consumes either source unchanged.
    """
    # imported lazily: only the elastic path needs .env
    from src.es_helpers import es_search, ElasticGatewayError

    target = parse_size(size)  # None means "every matching document"
    documents = []
    search_after = None
    page_size = PAGE_SIZE

    while target is None or len(documents) < target:
        body = {
            "size": page_size if target is None else min(page_size, target - len(documents)),
            "query": build_license_query(license_filter, original_license=original_license),
            "sort": [{"id": "asc"}]
        }
        if source_fields:
            body["_source"] = source_fields
        if search_after:
            body["search_after"] = search_after

        try:
            hits = es_search(body, index=index)["hits"]["hits"]
        except ElasticGatewayError:
            # The gateway gave up on this page. Halve it and try again rather
            # than losing the whole run.
            if page_size <= MIN_PAGE_SIZE:
                raise
            page_size = max(MIN_PAGE_SIZE, page_size // 2)
            print(f"  (elastic timed out, retrying with page size {page_size})")
            continue

        if not hits:
            break  # ran out of matching documents before reaching `target`

        for hit in hits:
            source = hit["_source"]
            source.setdefault("id", hit["_id"])
            documents.append(source)

        search_after = hits[-1]["sort"]

    return documents


def fetch_gotriple_documents(page="1", size="1000", license_filter="unresolved"):
    # The API's fq takes a comma-separated list as an OR, so the unresolved group
    # maps onto it directly. (Its totals are lower than the index's: the API
    # de-duplicates and the empty value matches nothing either way.)
    if license_filter in UNRESOLVED_FILTER_ALIASES:
        fq_value = ",".join(UNRESOLVED_LICENSE_VALUES)
    elif license_filter in (None, "any", "all"):
        fq_value = None
    else:
        fq_value = license_filter

    params = {
        "include_duplicates": "false", "page": page, "size": size,
        "sort": "most_recent:desc"
    }
    if fq_value is not None:
        params["fq"] = f"license={fq_value}"

    response = polite_get(api_session, "https://api.gotriple.eu/api/documents",
                          params=params, timeout=30)
    if response is None or response.status_code != 200:
        return []
    return response.json().get("data", [])