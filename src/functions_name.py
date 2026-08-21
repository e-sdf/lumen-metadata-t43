import difflib
import re
import time
import requests
import pandas as pd
import threading
import re
import requests
import xml.etree.ElementTree as ET

session = requests.Session()
# Use a session with short timeouts to prevent the multithreading from freezing
enrichment_session = requests.Session()
enrichment_session.headers.update({"User-Agent": "GoTriple-Disambiguation-Bot/1.0"})
# ==========================================
# DISAMBIGUATION REGISTRY & LOCK
# ==========================================
# A thread-safe global list to keep track of seen authors and their publication context
registry_lock = threading.Lock()
AUTHOR_REGISTRY = []

PROFILES_INDEX = "triple-profiles-prod"

# The profiles index is slow: a 1,000-profile page takes longer than the route's
# gateway timeout and comes back as a 504 HTML page. 200 measures around 9s,
# which leaves comfortable headroom.
PAGE_SIZE = 200
MIN_PAGE_SIZE = 25  # floor when shrinking after a gateway timeout
MAX_RESULT_WINDOW = 10000  # Elasticsearch's ceiling on from + size
DOCUMENTS_INDEX = "triple-documents-prod"

# 2,037 profiles carry an empty fullname (none are missing the field entirely).
EMPTY_NAME_QUERY = {"term": {"fullname.keyword": ""}}

# A name can be broken without being empty. Measured counts in the index:
#   empty                                          2,037
#   an ORCID sitting inside the name               2,859
#   the word "orcid" left in the name                600
#   a URL instead of, or beside, a name            1,289
#   nothing but digits                               513
ORCID_PATTERN = ".*[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9X]{4}.*"

# Names carrying an ORCID, in any form: the bare identifier, an orcid.org URL,
# or the identifier tacked onto a real name. The highest-yield repair case -
# ORCID answers with the canonical name almost every time.
ORCID_NAME_QUERY = {
    "bool": {
        "should": [
            {"regexp": {"fullname.keyword": ORCID_PATTERN}},
            {"match": {"fullname": "orcid"}},
        ],
        "minimum_should_match": 1,
    }
}

JUNK_NAME_QUERY = {
    "bool": {
        "should": [
            {"match": {"fullname": "http"}},
            {"regexp": {"fullname.keyword": "[0-9]+"}},
        ],
        "minimum_should_match": 1,
    }
}

MALFORMED_NAME_QUERY = {
    "bool": {
        "should": [EMPTY_NAME_QUERY, ORCID_NAME_QUERY, JUNK_NAME_QUERY],
        "minimum_should_match": 1,
    }
}

# Selections the name-repair task can run over.
NAME_FILTER_QUERIES = {
    "empty": EMPTY_NAME_QUERY,
    "orcid": ORCID_NAME_QUERY,
    "junk": JUNK_NAME_QUERY,
    "malformed": MALFORMED_NAME_QUERY,
}


def classify_name_problem(fullname):
    """What is wrong with this name, so the report can separate the cases."""
    text = str(fullname or "").strip()
    if not text:
        return "empty"

    has_orcid = bool(extract_orcid(text))
    # Strip the ORCID and any URL, then see whether a name was ever there.
    remainder = re.sub(r'https?://\S+', '', text)
    remainder = re.sub(r'(?i)\borcid\b\s*(icon)?\s*:?', '', remainder)
    remainder = re.sub(r'\d{4}-\d{4}-\d{4}-\d{3}[0-9X]', '', remainder)
    letters = re.findall(r'[^\W\d_]', remainder)

    if has_orcid:
        return "orcid only" if len(letters) < 2 else "orcid embedded in name"
    # "Ardeshir Bazrkar orcid" - the word survived but the identifier did not.
    if re.search(r'(?i)\borcid\b', text):
        return "stray orcid word"
    if text.isdigit():
        return "numeric"
    if re.match(r'^https?://', text):
        return "url instead of a name"
    if len(letters) < 2:
        return "no letters"
    return "ok"

def extract_orcid(text):
    if not text or pd.isna(text):
        return None
    clean_text = str(text).replace(" ", "").upper()
    if match := re.search(r'(\d{3,4})-(\d{4})-(\d{4})-(\d{3}[0-9X])', clean_text):
        return f"{match.group(1).zfill(4)}-{match.group(2).zfill(4)}-{match.group(3).zfill(4)}-{match.group(4)}"
    return None

def enrich_author_data_from_documents(docs, known_name=""):
    """
    Takes a list of document strings, detects their type, and queries 
    their source APIs to find an ORCID or a better full name.
    """
    found_orcid = None
    found_name = None
    
    if not isinstance(docs, list):
        return found_orcid, found_name

    for doc in docs:
        doc_str = str(doc)
        
        # 1. DOAJ Articles
        if "doaj.org_article" in doc_str:
            match = re.search(r'doaj\.org_article[:_]([a-zA-Z0-9]+)', doc_str)
            if match:
                doaj_id = match.group(1)
                try:
                    res = enrichment_session.get(f"https://doaj.org/api/v3/articles/{doaj_id}", timeout=3)
                    if res.status_code == 200:
                        authors = res.json().get("bibjson", {}).get("author", [])
                        for author in authors:
                            # Fuzzy check to make sure we are grabbing the right author from the list
                            if known_name.lower().split()[-1] in author.get("name", "").lower():
                                if author.get("orcid_id"):
                                    found_orcid = author["orcid_id"].replace("https://orcid.org/", "")
                                found_name = author.get("name")
                except:
                    pass
                    
        # 2. Handles and DOIs (e.g., 10670_1.stl7fb -> 10670/1.stl7fb)
        elif doc_str.startswith("10"):
            clean_id = doc_str.replace("_", "/", 1) # Replace the first underscore with a slash
            
            # If it has a dot, it's a DOI -> Query Crossref
            if clean_id.startswith("10."):
                try:
                    res = enrichment_session.get(f"https://api.crossref.org/works/{clean_id}", timeout=3)
                    if res.status_code == 200:
                        authors = res.json().get("message", {}).get("author", [])
                        for author in authors:
                            author_last = author.get("family", "").lower()
                            if author_last and author_last in known_name.lower():
                                if author.get("ORCID"):
                                    found_orcid = author["ORCID"].replace("http://orcid.org/", "").replace("https://orcid.org/", "")
                                found_name = f"{author.get('given', '')} {author.get('family', '')}".strip()
                except:
                    pass
                    
        # 3. Standard OAI-PMH Repositories (e.g., ftunivzadar:oai:repozitorij...)
        elif ":oai:" in doc_str:
            try:
                # Extract domain and document ID. Replace aggregator underscores with slashes for the ID
                parts = doc_str.split(":oai:")[-1].split(":")
                if len(parts) == 2:
                    domain = parts[0]
                    doc_id = parts[1].replace("_", "/", 1)
                    
                    # Try standard OAI endpoint
                    oai_url = f"https://{domain}/oai/request?verb=GetRecord&metadataPrefix=oai_dc&identifier=oai:{domain}:{doc_id}"
                    res = enrichment_session.get(oai_url, timeout=3)
                    
                    # If the repository uses modern DSpace (like CORA), it hides the API under /server/
                    if "<ds-app>" in res.text or res.status_code == 404:
                        oai_url = f"https://{domain}/server/oai/request?verb=GetRecord&metadataPrefix=oai_dc&identifier=oai:{domain}:{doc_id}"
                        res = enrichment_session.get(oai_url, timeout=3)

                    if res.status_code == 200 and "<?xml" in res.text:
                        root = ET.fromstring(res.content)
                        # Find creators in the Dublin Core XML
                        for creator in root.findall(".//{http://purl.org/dc/elements/1.1/}creator"):
                            if creator.text and known_name.lower().split()[-1] in creator.text.lower():
                                found_name = creator.text
                                # Note: basic oai_dc rarely contains ORCIDs natively, but we get a pristine name format
            except:
                pass
                
        # Break early if we successfully found an ORCID to save API calls
        if found_orcid:
            break

    return found_orcid, found_name

def clean_messy_name(text, orcid):
    if not text or pd.isna(text):
        return "No Name Available"
    clean_text = str(text)
    if orcid:
        clean_text = clean_text.replace(orcid, "")
    clean_text = re.sub(r'https?://[^\s,]*', '', clean_text)
    clean_text = re.sub(r'(?i)\borcid\b\s*(id)?\s*:?', '', clean_text)
    clean_text = re.sub(r'[|;:#/]+', ' ', clean_text).strip(" -,")
    return clean_text if clean_text else "No Name Found"

def fetch_orcid_name(orcid_id, original_text):
    if not orcid_id:
        return clean_messy_name(original_text, None)
    try:
        response = session.get(f"https://pub.orcid.org/v3.0/{orcid_id}/person", headers={"Accept": "application/json"})
        if response.status_code == 200:
            name_data = response.json().get("name")
            if not name_data: return clean_messy_name(original_text, orcid_id)
            if name_data.get("credit-name"): return name_data["credit-name"]["value"]
            
            given = name_data.get("given-names", {}).get("value", "") if name_data.get("given-names") else ""
            family = name_data.get("family-name", {}).get("value", "") if name_data.get("family-name") else ""
            full_api_name = f"{given} {family}".strip()
            
            return full_api_name if full_api_name else clean_messy_name(original_text, orcid_id)
    except:
        pass
    return clean_messy_name(original_text, orcid_id)

def generate_name_variants(fullname):
    """Generates and segregates standard variants of an author's name."""
    full_variants = {fullname.lower().strip()}
    initial_variants = set()
    
    # Handle "Lastname, Firstname" format
    if "," in fullname:
        parts = [p.strip() for p in fullname.split(",")]
        if len(parts) >= 2:
            last, first = parts[0], parts[1]
            full_variants.add(f"{first} {last}".lower())
            if len(first) > 0:
                initial_variants.add(f"{last}, {first[0]}.".lower())
                initial_variants.add(f"{first[0]}. {last}".lower())
    # Handle "Firstname Lastname" format
    else:
        parts = [p.strip() for p in fullname.split(" ") if p.strip()]
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            full_variants.add(f"{last}, {first}".lower())
            if len(first) > 0:
                initial_variants.add(f"{last}, {first[0]}.".lower())
                initial_variants.add(f"{first[0]}. {last}".lower())
                
    # This specifically returns TWO values, which fixes the unpacking error
    return full_variants, initial_variants

def extract_document_domains(docs):
    """
    Helper function to extract repository domains.
    No aggregators are blocked; all domains are retained.
    """
    domains = set()
    if not isinstance(docs, list):
        return domains
        
    for doc in docs:
        parts = str(doc).split(':')
        for part in parts:
            part_lower = part.strip().lower()
            if "oai" in part_lower or "_" in part or not part_lower:
                continue
            # Skip per-document identifiers - "1849642", "2008.12439" and the
            # like are unique per record, so they were only ever noise here.
            if not re.search(r'[^\W\d_]', part_lower):
                continue
            domains.add(part_lower)
    return domains


def name_is_informative(fullname):
    """Does this name identify a person, or just a surname plus initials?

    "Wang, Y. Y." is shared by thousands of unrelated people, so an exact match
    on it is not evidence of identity. "Pimenta, João Paulo" is.
    """
    text = str(fullname or "").strip()
    if not text:
        return False

    if "," in text:
        given = text.split(",", 1)[1]
    else:
        tokens = text.split()
        given = " ".join(tokens[:-1]) if len(tokens) > 1 else ""

    # A given name spelled out (3+ letters) carries information; initials do not.
    return any(len(token) >= 3 for token in re.findall(r'[^\W\d_]+', given))

import difflib
import time
import threading

import difflib
import time

# Values that dominate a count of author names but are not people: bibliographic
# placeholders, platforms, and role labels.
PLACEHOLDER_NAMES = {
    "", "s.n.", "unknown", "n/a", "na", "anonymous", "anon", "author", "authors",
    "springerlink", "protocol", "et al.", "no name available", "no name found",
}


def count_elastic_authors(name_filter=None, query=None, exact_name=None, index=PROFILES_INDEX):
    """How many profiles a selection matches, before committing to fetching them."""
    from src.es_helpers import es_search

    if exact_name is not None:
        body_query = {"term": {"fullname.keyword": exact_name}}
    elif name_filter in NAME_FILTER_QUERIES:
        body_query = NAME_FILTER_QUERIES[name_filter]
    elif query:
        body_query = {"match": {"fullname": {"query": query, "operator": "and"}}}
    else:
        body_query = {"match_all": {}}

    return es_search({"size": 0, "track_total_hits": True, "query": body_query},
                     index=index, timeout=180)["hits"]["total"]["value"]


def fetch_top_author_names(size=20, min_profiles=2, index=PROFILES_INDEX):
    """The names carried by the most profiles - i.e. where duplicate identities
    are most likely to be hiding.

    Placeholders are filtered out: a raw count is topped by "s.n." (8,578
    profiles), "unknown" (4,022) and "SpringerLink" (2,154) before any real
    person appears.
    """
    from src.es_helpers import es_search

    # Over-fetch, because the placeholders occupy the first several buckets.
    buckets = es_search({
        "size": 0,
        "aggs": {"names": {"terms": {"field": "fullname.keyword", "size": size * 3}}}
    }, index=index, timeout=180)["aggregations"]["names"]["buckets"]

    names = []
    for bucket in buckets:
        name = str(bucket["key"]).strip()
        if name.lower() in PLACEHOLDER_NAMES or bucket["doc_count"] < min_profiles:
            continue
        names.append({"fullname": name, "profiles": bucket["doc_count"]})
        if len(names) >= size:
            break
    return names


def fetch_names_from_documents(profile_ids, chunk_size=50, index=DOCUMENTS_INDEX):
    """Every name form the documents record for these author profile ids.

    `author` is a nested field, so this needs a nested query - a plain term on
    `author.id` silently matches nothing. Returns
    {profile_id: {"names": [...], "documents": {...}}}.

    This is the answer to "can a full name come from the document instead of
    ORCID": yes, and the documents index is the better source - it knows 287
    documents for one profile whose own `author_of` lists 221.
    """
    from src.es_helpers import es_search

    ids = [str(p) for p in dict.fromkeys(profile_ids)]
    found = {}

    for start in range(0, len(ids), chunk_size):
        chunk = ids[start:start + chunk_size]
        wanted = set(chunk)
        try:
            hits = es_search({
                "size": 200,
                "query": {"nested": {
                    "path": "author",
                    "query": {"terms": {"author.id": chunk}},
                    "inner_hits": {"size": 10, "_source": ["author.id", "author.fullname"]}
                }},
                "_source": ["id"]
            }, index=index)["hits"]["hits"]
        except Exception:
            continue  # one bad chunk should not sink the pass

        for hit in hits:
            document_id = hit["_source"].get("id")
            for inner in hit["inner_hits"]["author"]["hits"]["hits"]:
                source = inner["_source"]
                author_id = str(source.get("id"))
                if author_id not in wanted:
                    continue
                entry = found.setdefault(author_id, {"names": [], "documents": set()})
                entry["documents"].add(document_id)
                name = source.get("fullname")
                if looks_like_a_name(name) and name not in entry["names"]:
                    entry["names"].append(str(name).strip())

    return found


def best_name_form(names):
    """Pick the most complete form among the variants documents recorded, e.g.
    prefer "Xu Cui" over "Cui, X. Z.". Ties break alphabetically so the choice
    is deterministic."""
    informative = [n for n in names if name_is_informative(n)]
    candidates = informative or list(names)
    return min(candidates, key=lambda n: (-len(n), n)) if candidates else None


def looks_like_a_name(value):
    """GoTriple blanks a profile's fullname when the source metadata held junk,
    and the document it came from often still carries that junk (numeric author
    labels like "44134"). Recovering those is worse than leaving the name empty."""
    text = str(value or "").strip()
    letters = re.findall(r'[^\W\d_]', text)
    if len(letters) < 2:
        return False

    # Reject identifier-like values dressed up as names, e.g. "Dr. !1035331039".
    digits = sum(character.isdigit() for character in text)
    return digits / len(text) <= 0.3


def resolve_repository_ids(records, chunk_size=100):
    """Numeric "names" that are not people at all, but a repository's own author id.

    WEKO repositories (u-tokyo, niigata and a handful of others) publish their
    internal author id as a second `dc:creator`, immediately after the creator it
    belongs to. BASE harvests that flattened oai_dc view, so GoTriple mints a
    profile per id. The repository's own richer jpcoar record for the same
    document lists the creators and no ids at all:

        jpcoar   creatorName: 増田, 康介
        oai_dc   dc:creator : 増田, 康介 | 161278      <- 161278 is her id, not a person

    The pairing survives into the documents index, where `author` keeps the
    harvested order, so the owner is simply the entry before the id - no request
    to the repository needed. Returns {profile_id: owner's name}, and only where
    the entry before it really is a name.
    """
    from src.es_helpers import es_search

    numeric = {str(r.get("id")): str(r.get("fullname") or "").strip()
               for r in records if str(r.get("fullname") or "").strip().isdigit()}
    if not numeric:
        return {}

    references = []
    for record in records:
        if str(record.get("id")) not in numeric:
            continue
        docs = record.get("author_of") or []
        references.extend(str(d) for d in (docs if isinstance(docs, list) else [docs]))
    references = list(dict.fromkeys(references))

    owners = {}
    for start in range(0, len(references), chunk_size):
        try:
            hits = es_search({
                "size": chunk_size,
                "query": {"terms": {"id": references[start:start + chunk_size]}},
                "_source": ["id", "author"]
            }, index=DOCUMENTS_INDEX)["hits"]["hits"]
        except Exception:
            continue  # a bad chunk should not sink the whole pass

        for hit in hits:
            authors = hit["_source"].get("author") or []
            for position, author in enumerate(authors):
                author_id = str(author.get("id"))
                if author_id not in numeric or author_id in owners or position == 0:
                    continue
                # the id is only an id if the profile's own name is that number too
                if str(author.get("fullname") or "").strip() != numeric[author_id]:
                    continue
                previous = authors[position - 1].get("fullname")
                if looks_like_a_name(previous):
                    owners[author_id] = str(previous).strip()

    return owners


def recover_names_from_documents(records, chunk_size=100):
    """Look each profile's `author_of` documents up in the documents index and
    take the name that document records for this author id.

    Batched: one terms query per chunk of references rather than one per author.
    Returns {profile_id: recovered_name}.
    """
    from src.es_helpers import es_search

    references = []
    for record in records:
        docs = record.get("author_of") or []
        references.extend(str(d) for d in (docs if isinstance(docs, list) else [docs]))
    references = list(dict.fromkeys(references))  # de-duplicate, keep order

    wanted = {str(r.get("id")) for r in records}
    recovered = {}

    for start in range(0, len(references), chunk_size):
        chunk = references[start:start + chunk_size]
        try:
            hits = es_search({
                "size": chunk_size,
                "query": {"terms": {"id": chunk}},
                "_source": ["id", "author"]
            }, index=DOCUMENTS_INDEX)["hits"]["hits"]
        except Exception:
            continue  # a bad chunk should not sink the whole recovery pass

        for hit in hits:
            for author in (hit["_source"].get("author") or []):
                author_id = str(author.get("id"))
                if author_id in wanted and author_id not in recovered:
                    if looks_like_a_name(author.get("fullname")):
                        recovered[author_id] = str(author.get("fullname")).strip()

    return recovered


def resolve_author(record):
    """PHASE 1 - name and ORCID recovery for ONE record, in isolation.

    Knows nothing about any other record, so it is order-independent and safe to
    run in parallel. Sets `id_fix` and `fullname_fix` and returns the record.
    """
    original_text = record.get("fullname", "")
    extracted_orcid = extract_orcid(original_text)

    docs_data = record.get("author_of") or []
    docs_list = docs_data if isinstance(docs_data, list) else [docs_data]

    enriched_name = None
    if not extracted_orcid and docs_list:
        found_orcid, found_name = enrich_author_data_from_documents(docs_list, original_text)
        if found_orcid and (validated := extract_orcid(found_orcid)):
            extracted_orcid = validated
        if found_name:
            enriched_name = found_name

    corrected_name = fetch_orcid_name(extracted_orcid, enriched_name or original_text)

    record["id_fix"] = extracted_orcid
    record["fullname_fix"] = corrected_name.title() if corrected_name else ""
    record["_resolved_name"] = enriched_name or original_text  # what clustering compares

    time.sleep(0.1)  # Respect ORCID API rate limits across threads
    return record


def cluster_authors(records):
    """PHASE 2 - decide which resolved records are the same person.

    Deterministic: the input is sorted first, matches are transitive (union-find)
    rather than first-seen-wins, and the master name for each cluster is chosen
    by rule instead of by arrival order. Pure - no network, no shared state.
    """
    # Sort so the outcome cannot depend on the order futures happened to complete.
    ordered = sorted(records, key=lambda r: (str(r.get("_resolved_name") or ""), str(r.get("id") or "")))

    contexts = []
    for record in ordered:
        docs = record.get("author_of") or []
        docs_list = docs if isinstance(docs, list) else [docs]
        topics = record.get("topic") or []
        orgs = record.get("current_organization") or []
        full_vars, init_vars = generate_name_variants(record.get("_resolved_name") or record.get("fullname", ""))
        contexts.append({
            "record": record,
            "orcid": record.get("id_fix"),
            "full_variants": full_vars,
            "initial_variants": init_vars,
            "topics": set(topics if isinstance(topics, list) else [topics]),
            "organizations": set(orgs if isinstance(orgs, list) else [orgs]),
            "domains": extract_document_domains(docs_list),
            # Exact shared documents: two profiles on the same paper with the
            # same name are the same person. Cheaper and far more precise than
            # the domain overlap, which only says "same repository".
            "documents": {str(d) for d in docs_list},
            "informative": name_is_informative(record.get("_resolved_name") or record.get("fullname", "")),
        })

    parent = list(range(len(contexts)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(len(contexts)):
        for j in range(i + 1, len(contexts)):
            a, b = contexts[i], contexts[j]

            # Two known, different ORCIDs are proof of two different people.
            if a["orcid"] and b["orcid"] and a["orcid"] != b["orcid"]:
                continue
            if a["orcid"] and a["orcid"] == b["orcid"]:
                union(i, j)
                continue

            name_match = bool(a["full_variants"] & b["full_variants"]) or _fuzzy_match(
                a["full_variants"], b["full_variants"])
            initials_match = not name_match and bool(a["initial_variants"] & b["initial_variants"])
            if not (name_match or initials_match):
                continue

            shared_document = bool(a["documents"] & b["documents"])
            org_overlap = bool(a["organizations"] & b["organizations"])
            topic_overlap = bool(a["topics"] & b["topics"])
            domain_overlap = bool(a["domains"] & b["domains"])

            # Co-authorship on the same document, or a shared organisation, is
            # proof enough on its own - even for a name as common as "Wang, Y.".
            if shared_document or org_overlap:
                union(i, j)
                continue

            # Otherwise the only remaining evidence is topic or repository
            # overlap, which is far too weak to identify people: GoTriple topics
            # are a handful of coarse buckets, and a shared repository just means
            # two strangers published in the same place. Accept it only when the
            # name itself is distinctive.
            if name_match and a["informative"] and b["informative"]:
                if topic_overlap or domain_overlap:
                    union(i, j)

    clusters = {}
    for index, context in enumerate(contexts):
        clusters.setdefault(find(index), []).append(context)

    for members in clusters.values():
        master = _pick_master(members)
        for context in members:
            record = context["record"]
            is_master = context is master
            record["is_aka"] = "No" if is_master else "Yes"
            record["aka_of"] = "" if is_master else str(master["record"].get("_resolved_name") or "")
            # `aka_of` is only a name, and two unrelated people can share one.
            # The master's id identifies the cluster unambiguously.
            record["cluster_id"] = str(master["record"].get("id") or "")
            record["cluster_size"] = len(members)

    return [context["record"] for context in contexts]


def _fuzzy_match(variants_a, variants_b, threshold=0.90):
    return any(difflib.SequenceMatcher(None, a, b).ratio() > threshold
               for a in variants_a for b in variants_b)


def _pick_master(members):
    """Canonical record for a cluster: prefer one with an ORCID, then the most
    complete name, then the id - never 'whoever arrived first'."""
    return min(members, key=lambda c: (
        0 if c["orcid"] else 1,
        -len(str(c["record"].get("_resolved_name") or "")),
        str(c["record"].get("id") or ""),
    ))


def process_author(record):
    original_text = record.get("fullname", "")
    extracted_orcid = extract_orcid(original_text)
    
    docs_data = record.get("author_of") or []
    docs_list = docs_data if isinstance(docs_data, list) else [docs_data]
    
    # ==========================================
    # 1. ENRICHMENT PHASE
    # ==========================================
    enriched_name = None
    if not extracted_orcid and docs_list:
        # Only query APIs if we don't already have an ORCID
        found_orcid, found_name = enrich_author_data_from_documents(docs_list, original_text)
        if found_orcid:
            # Validate the returned format just to be safe
            validated_orcid = extract_orcid(found_orcid)
            if validated_orcid:
                extracted_orcid = validated_orcid
        if found_name:
            enriched_name = found_name
            
    # Fallback to the original ORCID logic if the documents didn't provide one
    text_to_query = enriched_name if enriched_name else original_text
    corrected_name = fetch_orcid_name(extracted_orcid, text_to_query)
    
    record["id_fix"] = extracted_orcid
    record["fullname_fix"] = corrected_name.title() if corrected_name else ""
    
    # Generate split variants (using the enriched name if we found a better one via the APIs!)
    full_vars, init_vars = generate_name_variants(enriched_name if enriched_name else original_text)
    
    # ==========================================
    # 2. CONTEXT EXTRACTION
    # ==========================================
    # Safely extract contexts (handling None values to prevent crashes)
    topics_data = record.get("topic") or []
    orgs_data = record.get("current_organization") or []
    
    topics = set(topics_data) if isinstance(topics_data, list) else set([topics_data])
    organizations = set(orgs_data) if isinstance(orgs_data, list) else set([orgs_data])
    document_domains = extract_document_domains(docs_list)
    
    # ==========================================
    # 3. DISAMBIGUATION LOGIC
    # ==========================================
    is_aka = False
    aka_primary_name = ""
    
    with registry_lock:
        for registered in AUTHOR_REGISTRY:
            # --- SHORTCUT RULE: Exact ORCID Match ---
            if extracted_orcid and registered.get('orcid') == extracted_orcid:
                # ... [existing shortcut code] ...
                break
                
            # --- NEW: HARD BLOCKER RULE ---
            # If both profiles have a known ORCID and they do NOT match, they are different people.
            if extracted_orcid and registered.get('orcid') and extracted_orcid != registered.get('orcid'):
                continue # Skip this registry entry entirely
                
            # --- HEURISTIC RULE: Segregated Name Overlap + Context ---
            strong_name_match = False
                
            # --- HEURISTIC RULE: Segregated Name Overlap + Context ---
            strong_name_match = False
            weak_name_match = False
            
            # 1. Check Full Names (Exact Match)
            if full_vars.intersection(registered['full_variants']):
                strong_name_match = True
            else:
                # 2. Check Full Names (Fuzzy Match > 90%)
                for v1 in full_vars:
                    for v2 in registered['full_variants']:
                        if difflib.SequenceMatcher(None, v1, v2).ratio() > 0.90:
                            strong_name_match = True
                            break
                    if strong_name_match: 
                        break
            
            # 3. Check Initials (Only if Full Names failed)
            if not strong_name_match and init_vars.intersection(registered['initial_variants']):
                weak_name_match = True
                
            # Check for proofs of identity
            topic_overlap = bool(topics.intersection(registered['topics']))
            org_overlap = bool(organizations.intersection(registered['organizations']))
            domain_overlap = bool(document_domains.intersection(registered['domains']))
            
            # Decide if merge is safe based on match strength
            if strong_name_match and (topic_overlap or org_overlap or domain_overlap):
                is_aka = True
            elif weak_name_match and org_overlap:
                # Because generic aggregators are NOT blocked, domain_overlap is too risky for initials-only matches.
                # Must share an explicit organization.
                is_aka = True
                
            if is_aka:
                aka_primary_name = registered['primary_name']
                
                # Update contexts
                registered['topics'].update(topics)
                registered['organizations'].update(organizations)
                registered['domains'].update(document_domains)
                registered['full_variants'].update(full_vars)
                registered['initial_variants'].update(init_vars)
                
                if extracted_orcid and not registered.get('orcid'):
                    registered['orcid'] = extracted_orcid
                break
                    
        # Register new distinct author if no match was found
        if not is_aka:
            AUTHOR_REGISTRY.append({
                'primary_name': original_text,
                'orcid': extracted_orcid,
                'full_variants': full_vars,
                'initial_variants': init_vars,
                'topics': topics,
                'organizations': organizations,
                'domains': document_domains
            })
            
    # Apply tags to the final record
    if is_aka:
        record["is_aka"] = "Yes"
        record["aka_of"] = aka_primary_name
        display_name = corrected_name if corrected_name else original_text
        print(f"  -> [DISAMBIGUATION] '{display_name}' identified as an AKA of '{aka_primary_name}'")
    else:
        record["is_aka"] = "No"
        record["aka_of"] = ""
    
    time.sleep(0.1) # Respect ORCID API rate limits across threads
    return record

def fetch_gotriple_authors(params={}):
    response = requests.get("https://api.gotriple.eu/api/authors",
                            params=params,
                            headers={"accept": "application/json"})
    return response.json().get("data", []) if response.status_code == 200 else []


# The index stores several fields in camelCase where the REST API uses
# snake_case. Renaming them keeps the Excel columns comparable across sources.
# The richer ones only appear on the ~779 registered-user profiles, which is why
# a small sample of the index looks like it only has numberOfDocuments and
# registeredUser.
ELASTIC_FIELD_RENAMES = {
    "numberOfDocuments": "number_of_documents",
    "registeredUser": "registered_user",
    "openToCollaboration": "open_to_collaboration",
    "givenName": "given_name",
    "familyName": "family_name",
    "goTripleId": "go_triple_id",
    "knowsLanguage": "knows_language",
    "knowsAbout": "knows_about",
    "hasOccupation": "has_occupation",
    "currentOrganization": "current_organization",
    "currentRole": "current_role",
}


def fetch_elastic_authors(size="1000", query=None, index=PROFILES_INDEX, name_filter=None, exact_name=None):
    """Author profiles straight from Elasticsearch instead of the REST API.

    `query` is the free-text name search, i.e. the API's `q`. Pages with
    search_after on `id`, so batches past the 10,000-hit window work.

    Note the index carries fewer fields than the API: there is no
    `current_organization`, so process_author() loses the organisation-overlap
    signal and falls back to name plus topic/domain evidence.
    """
    from src.es_helpers import es_search, ElasticGatewayError
    from src.functions_license import parse_size

    target = parse_size(size)  # None means "every matching profile"
    authors = []
    search_after = None
    page_size = PAGE_SIZE

    while target is None or len(authors) < target:
        # Annotated: the literal alone infers dict[str, int], so every later
        # body["query"] = {...} would be flagged as assigning a dict to an int.
        body: dict = {"size": page_size if target is None else min(page_size, target - len(authors))}

        if exact_name is not None:
            # Every profile carrying exactly this name - the population where
            # duplicate identities live. Stable `id` cursor, so it can page
            # past 10,000 (the top names have well over that).
            body["query"] = {"term": {"fullname.keyword": exact_name}}
            body["sort"] = [{"id": "asc"}]
            if search_after:
                body["search_after"] = search_after
        elif name_filter in NAME_FILTER_QUERIES:
            # Profiles whose name needs fixing - blank, ORCID-bearing, or junk.
            # Paged by the stable `id` cursor.
            body["query"] = NAME_FILTER_QUERIES[name_filter]
            body["sort"] = [{"id": "asc"}]
            if search_after:
                body["search_after"] = search_after
        elif query:
            # "and" so every term must appear ("Wang Y." must match both), and
            # sort by relevance - sorting on `id` alone would silently return
            # the alphabetically-first matches instead of the best ones.
            body["query"] = {"match": {"fullname": {"query": query, "operator": "and"}}}
            body["sort"] = [{"_score": "desc"}, {"id": "asc"}]

            # from/size, NOT search_after: a `_score` cursor loses float
            # precision in the JSON round-trip and re-serves documents it should
            # have skipped (measured: 1,000 fetched, only 600 distinct).
            body["from"] = len(authors)
            if body["from"] + body["size"] > MAX_RESULT_WINDOW:
                print(f"  (stopping at {len(authors)}: relevance paging cannot go past "
                      f"{MAX_RESULT_WINDOW} results - drop the `q` to scan the whole index)")
                break
        else:
            # No query means no relevance to preserve, so the stable `id` cursor
            # can page arbitrarily deep.
            body["query"] = {"match_all": {}}
            body["sort"] = [{"id": "asc"}]
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
            break  # ran out of matching profiles before reaching `target`

        for hit in hits:
            source = hit["_source"]
            source.setdefault("id", hit["_id"])
            for old, new in ELASTIC_FIELD_RENAMES.items():
                if old in source:
                    source[new] = source.pop(old)
            authors.append(source)

        search_after = hits[-1]["sort"]

    return authors