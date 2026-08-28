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

def _author_in_source_record(known_name, people):
    """The one author of a source record this profile can be, or None.

    Records with a cast of hundreds are refused outright. A 2,273-author CMS
    paper lists dozens of people sharing any given initial, so its creator list
    cannot say which of them a profile is - the same reason CO_AUTHOR_LIMIT
    exists on the co-author side. Beyond that the rule is
    match_author_in_work()'s: same family name, given names that do not
    contradict, and a unique match or nothing.
    """
    people = [person for person in people if str(person.get("name") or "").strip()]
    if not people or len(people) > CO_AUTHOR_LIMIT:
        return None
    return match_author_in_work(known_name, people)


def enrich_author_data_from_documents(docs, known_name=""):
    """
    Takes a list of document strings, detects their type, and queries
    their source APIs to find an ORCID or a better full name.

    Each source is reduced to the same {name, orcid} shape and handed to
    _author_in_source_record(), so one matching rule covers all three. Matching
    on a loose substring of the name is what once turned a "Wang, Y." profile
    into "Pakhotin, Y.": "Wang, Y." splits to the initial "y.", which is a
    substring of 39 of that paper's 2,273 creators, and the last one seen won.
    """
    found_orcid = None
    found_name = None

    if not isinstance(docs, list):
        return found_orcid, found_name

    for doc in docs:
        doc_str = str(doc)
        match = None

        # 1. DOAJ Articles
        if "doaj.org_article" in doc_str:
            article = re.search(r'doaj\.org_article[:_]([a-zA-Z0-9]+)', doc_str)
            if article:
                doaj_id = article.group(1)
                try:
                    res = enrichment_session.get(f"https://doaj.org/api/v3/articles/{doaj_id}", timeout=3)
                    if res.status_code == 200:
                        match = _author_in_source_record(known_name, [
                            {"name": author.get("name"),
                             "orcid": (author.get("orcid_id") or "").replace("https://orcid.org/", "") or None}
                            for author in res.json().get("bibjson", {}).get("author", []) or []])
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
                        match = _author_in_source_record(known_name, [
                            {"name": f"{author.get('given', '')} {author.get('family', '')}".strip(),
                             "orcid": (author.get("ORCID") or "").rsplit("/", 1)[-1] or None}
                            for author in res.json().get("message", {}).get("author", []) or []])
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
                        # Note: basic oai_dc rarely contains ORCIDs natively, but we get a pristine name format
                        match = _author_in_source_record(known_name, [
                            {"name": creator.text, "orcid": None}
                            for creator in root.findall(".//{http://purl.org/dc/elements/1.1/}creator")])
            except:
                pass

        if match:
            found_name = match["name"]
            if match.get("orcid"):
                found_orcid = match["orcid"]

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

# ==========================================================================
# MALFORMED FULL NAMES - repair for the kinds beyond empty/ORCID/URL/digits
# ==========================================================================
# Only the first is a repair, but the rest are answers too: a value that is not
# a person cannot be turned into one, and saying so is worth more than writing a
# plausible-looking name over it. Every outcome is a decision, none is a shrug.
FIXED = "fixed"                    # changed, and what came out is a usable name
ALREADY_OK = "already usable"      # nothing to repair
SEVERAL = "several people"         # one field holding a whole author list
NOT_A_PERSON = "not a person"      # a placeholder, or an organisation
UNFIXABLE = "cannot be fixed"      # too little left, or a character lost for good

MALFORMED_OUTCOMES = [FIXED, ALREADY_OK, SEVERAL, NOT_A_PERSON, UNFIXABLE]

# Exact values that stand in for a missing name. Matched case-insensitively on
# the whole stripped value, never as a substring - "Unknown" is a placeholder,
# "Unknown, J." is somebody whose surname was lost.
PLACEHOLDER_VALUES = {
    "", "-", "--", "---", ".", "..", "...", "?", "??", "n/a", "n.a.", "na", "nn", "n.n.",
    "s.n.", "s. n.", "sine nomine", "unknown", "unknown author", "not available",
    "no name available", "no name found", "anonymous", "anon", "anonyme", "anónimo",
    "author", "authors", "autor", "et al.", "et al", "various", "various authors",
    "springerlink", "protocol", "null", "none", "undefined",
}

# A word that makes the value an organisation rather than a person. Kept to
# unambiguous ones: "Institute" is never a surname, "Center" is not either, but
# "Bank" and "Press" are, so they are left out.
INSTITUTION_WORDS = {
    "university", "universite", "universität", "universidad", "universidade", "università",
    "universiteit", "uniwersytet", "institute", "institut", "instituto", "istituto",
    "college", "school", "faculty", "faculté", "akademie", "academy", "académie",
    "laboratory", "laboratoire", "observatory", "observatoire", "museum", "musée",
    "ministry", "ministère", "department", "departamento", "consortium", "collaboration",
    "association", "foundation", "fondation", "fundación", "society", "gmbh", "inc.",
    "ltd", "llc", "s.a.", "corporation", "company", "commission", "committee", "council",
    "agency", "organisation", "organization", "centre", "center", "centro", "hospital",
    "clinic", "cnrs", "inserm", "unesco", "who",
}

# Mojibake: UTF-8 bytes that were read as cp1252 once, so "é" became "Ã©". The
# round-trip only reverses cleanly when that is really what happened, which is
# what makes it safe to attempt on every value.
MOJIBAKE_MARKERS = ("Ã", "â€", "â‚¬", "Â", "Ð", "ð", "�")


def fix_encoding(text):
    """Undo a mojibake round-trip and normalise invisible characters.

    Returns the text unchanged when the repair does not apply, so it is safe to
    run over every name rather than only the ones that look broken.
    """
    fixed = str(text)
    for _ in range(3):  # doubly-encoded values need more than one pass
        if not any(marker in fixed for marker in MOJIBAKE_MARKERS):
            break
        try:
            candidate = fixed.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate == fixed:
            break
        fixed = candidate

    fixed = fixed.replace(" ", " ").replace("​", "").replace("﻿", "")
    # U+FFFD is a character that was lost in transit. It is deliberately NOT
    # deleted here: dropping it turns "Löve" into "Lve", which is a wrong
    # name rather than a broken one. repair_malformed_name() refuses instead.
    fixed = "".join(character for character in fixed
                    if character.isprintable() or character.isspace())
    return re.sub(r"\s+", " ", fixed).strip()


def fix_casing(text):
    """Title-case a name that arrived in one case, and leave every other alone.

    `str.title()` is wrong for names - it produces "Mcnally" and "O'brien" - so
    the internal capital after Mc/Mac/O' is put back, and particles that belong
    in lower case stay there.
    """
    letters = [character for character in text if character.isalpha()]
    if len(letters) <= 3 or not (text.isupper() or text.islower()):
        return text

    PARTICLES = {"de", "da", "do", "dos", "das", "del", "della", "di", "van", "von", "der",
                 "den", "ter", "le", "la", "el", "bin", "ibn", "y", "e"}
    words = []
    for position, word in enumerate(text.lower().split()):
        if position and word.strip(".,") in PARTICLES:
            words.append(word)
            continue
        word = word[:1].upper() + word[1:]
        word = re.sub(r"\b(Mc|Mac|O')([a-z])", lambda m: m.group(1) + m.group(2).upper(), word)
        word = re.sub(r"(-)([a-z])", lambda m: m.group(1) + m.group(2).upper(), word)
        words.append(word)
    return " ".join(words)


def repair_malformed_name(text):
    """Best name this value can yield, and what happened to it.

    Returns (name, outcome). The steps run in the order the damage accumulates:
    the encoding is repaired first so everything after it sees real characters,
    then whatever is attached to the name is stripped, then what is left is
    judged - because a value can only be called a placeholder or an institution
    once the URL and the email have gone.
    """
    original = "" if text is None or (isinstance(text, float) and pd.isna(text)) else str(text)
    working = fix_encoding(original)

    if original.strip().lower() in PLACEHOLDER_VALUES:
        return "", NOT_A_PERSON

    # Attachments: an identifier or a contact detail sitting beside the name.
    working = re.sub(r"https?://\S+|www\.\S+", " ", working)
    working = re.sub(r"\S*@\S+", " ", working)
    working = re.sub(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", " ", working)
    working = re.sub(r"(?i)\borcid\b\s*(icon|id)?\s*:?", " ", working)
    # Digits that are not part of a name: lifespans, record ids, list positions.
    working = re.sub(r"\(\s*\d[\d\s.\-]*\)", " ", working)        # "Rui Sun (177521)"
    working = re.sub(r"\b\d{3,4}\s*-\s*\d{3,4}\b", " ", working)  # "1444-1522"
    working = re.sub(r"\d+", " ", working)

    # A whole author list in one field is not a name to repair. Splitting it into
    # profiles is a different job - one record has to become several - so it is
    # reported rather than guessed at.
    people = [part for part in re.split(r"\s*(?:;|::|\|)\s*", working)
              if len(re.findall(r"[^\W\d_]", part)) >= 2]
    if len(people) > 1:
        return _tidy(" ; ".join(_tidy(part) for part in people)), SEVERAL

    working = _tidy(re.sub(r"[|;#/\\<>\[\]{}*]+|::|,{2,}", " ", working))

    if working.lower() in PLACEHOLDER_VALUES:
        return "", NOT_A_PERSON
    if {word.strip(".,()").lower() for word in working.split()} & INSTITUTION_WORDS:
        return "", NOT_A_PERSON
    if "�" in working:
        return "", UNFIXABLE      # a letter was lost in transit; any guess is invention
    if len(re.findall(r"[^\W\d_]", working)) < 2:
        return "", UNFIXABLE

    repaired = fix_casing(working)
    return repaired, (FIXED if repaired != original.strip() else ALREADY_OK)


def _tidy(text):
    """Collapse whitespace and drop stranded punctuation - but never the full stop
    that ends an initial, so "Wang, Y." does not come back as "Wang, Y"."""
    text = re.sub(r"\s+", " ", str(text))
    text = re.sub(r"\.{2,}", ".", text)
    text = text.strip(" ,-–—:'\"").lstrip(". ")
    return text.strip(" ,-–—:'\"")


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


def _is_same_person(original, enriched):
    """May `enriched` replace `original`, or would that rename the person?

    Enrichment is allowed to *complete* a name ("Wang, Y." -> "Wang, Yun"), never
    to swap it for somebody else's. A profile whose name was already a name and
    comes back with a different family name is a mismatch in the source record,
    and keeping it is worse than keeping the initials: the wrong name then
    travels on to enrich_authors_from_dois(), where a *rare* wrong name passes
    the unique-match test that the true common one would have failed - which is
    how four "Wang, Y." profiles ended up holding Yuri Gershtein's ORCID.

    Names that were junk or blank have nothing to protect, so recovery from the
    document is exactly what should happen there.
    """
    if not looks_like_a_name(original):
        return True
    return _name_key(original)[0] == _name_key(enriched)[0]


def trusted_name(record):
    """The name to match a record against a paper's author list.

    Phase 1's `_resolved_name` where it is a refinement of the real name, and
    the untouched `fullname` where it is a rename - so no invented name can be
    laundered into an ORCID no matter which pass produced it.
    """
    original = str(record.get("fullname") or "")
    resolved = str(record.get("_resolved_name") or "")
    if resolved and _is_same_person(original, resolved):
        return resolved
    return original or resolved


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
        if found_name and _is_same_person(original_text, found_name):
            enriched_name = found_name

    corrected_name = fetch_orcid_name(extracted_orcid, enriched_name or original_text)

    record["id_fix"] = extracted_orcid
    record["fullname_fix"] = corrected_name.title() if corrected_name else ""
    record["_resolved_name"] = enriched_name or original_text  # what clustering compares

    time.sleep(0.1)  # Respect ORCID API rate limits across threads
    return record


# Above this many authors a paper says nothing about who anyone is: two different
# Y. Wangs inside the same 800-author collaboration share every co-author on it.
CO_AUTHOR_LIMIT = 25


def fetch_document_context(document_ids, chunk_size=100):
    """What the documents index knows about each document: its DOI and everyone
    credited on it.

    The DOI is the document's real identity - the same work is harvested from
    several repositories and lands in GoTriple under a different id each time
    (one paper as ftinsu:…, ftceafr:… and ftuniversailles:…), so two profiles on
    the same paper can look unrelated. The author list is the other half: who a
    person publishes *with* is the strongest evidence available here that two
    records are the same person, and it costs nothing extra to read.
    """
    from src.es_helpers import es_search

    ids = [str(d) for d in dict.fromkeys(document_ids) if str(d).strip()]
    context = {}
    for start in range(0, len(ids), chunk_size):
        try:
            hits = es_search({"size": chunk_size,
                              "query": {"terms": {"id": ids[start:start + chunk_size]}},
                              "_source": ["id", "doi", "author", "date_published"]},
                             index=DOCUMENTS_INDEX)["hits"]["hits"]
        except Exception:
            continue  # a bad chunk should not sink the pass
        for hit in hits:
            source = hit["_source"]
            context[source["id"]] = {
                "doi": [str(d).strip().lower() for d in (source.get("doi") or []) if str(d).strip()],
                "authors": [str(a.get("fullname")) for a in (source.get("author") or [])
                            if looks_like_a_name(a.get("fullname"))],
                "date": str(source.get("date_published") or ""),
            }
    return context


def fetch_document_dois(document_ids, chunk_size=100):
    """Just the DOIs, for callers that do not need the rest of the context."""
    return {document: found["doi"]
            for document, found in fetch_document_context(document_ids, chunk_size).items()
            if found["doi"]}


def co_author_keys(name, authors):
    """The co-authors of one paper, as "family|initial" keys.

    Keyed loosely so "Xu, X. H." and "X. Xu" are the same person, and never
    including anyone who shares the profile's own family name: two unrelated
    Y. Wangs must not be joined by the presence of a third Wang.
    """
    family = _name_key(name)[0]
    keys = set()
    for author in authors:
        other_family, other_given = _name_key(author)
        if not other_family or other_family == family:
            continue
        keys.add(f"{other_family}|{other_given[:1]}")
    return keys


_doi_authors_cache = {}
_doi_authors_lock = threading.Lock()


def authors_from_doi(doi, timeout=20):
    """Who a DOI says wrote the paper: name, ORCID and affiliations, per author.

    OpenAlex first - it carries an ORCID for nearly every work and an institution
    for almost all of them - with Crossref as the fallback. Cached, because one
    DOI is shared by every profile attached to that paper.
    """
    from src.rate_limit import polite_get

    key = str(doi).strip().lower()
    with _doi_authors_lock:
        if key in _doi_authors_cache:
            return _doi_authors_cache[key]

    people = []
    try:
        response = polite_get(enrichment_session, f"https://api.openalex.org/works/https://doi.org/{key}",
                              timeout=timeout)
        if response is not None and response.status_code == 200:
            for authorship in response.json().get("authorships", []) or []:
                author = authorship.get("author") or {}
                people.append({
                    "name": str(author.get("display_name") or "").strip(),
                    "orcid": (author.get("orcid") or "").rsplit("/", 1)[-1] or None,
                    "organizations": [str(i.get("display_name")) for i in (authorship.get("institutions") or [])
                                      if i.get("display_name")],
                })
    except Exception:
        people = []

    if not people:
        try:
            response = polite_get(enrichment_session, f"https://api.crossref.org/works/{key}", timeout=timeout)
            if response is not None and response.status_code == 200:
                for author in response.json().get("message", {}).get("author", []) or []:
                    name = f"{author.get('given', '')} {author.get('family', '')}".strip()
                    if not name:
                        continue
                    people.append({
                        "name": name,
                        "orcid": (author.get("ORCID") or "").rsplit("/", 1)[-1] or None,
                        "organizations": [str(a.get("name")) for a in (author.get("affiliation") or [])
                                          if a.get("name")],
                    })
        except Exception:
            pass

    with _doi_authors_lock:
        _doi_authors_cache[key] = people
    return people


def _name_key(value):
    """(family, given) lowercased, whichever way round the name was written."""
    text = re.sub(r"[.·]", " ", str(value or "")).strip()
    if "," in text:
        family, _, given = text.partition(",")
    else:
        parts = [p for p in text.split() if p]
        if len(parts) < 2:
            return text.lower(), ""
        family, given = parts[-1], " ".join(parts[:-1])
    return family.strip().lower(), given.strip().lower()


def match_author_in_work(name, people):
    """The one author of a work this profile can be, or None.

    Same family name, and given names that do not contradict - "Y." matches
    "Yang" but "Yang" does not match "Yun". **Only a unique match counts**: these
    papers routinely carry several Wangs, and picking one of them would invent
    evidence rather than find it.
    """
    family, given = _name_key(name)
    if not family:
        return None

    candidates = []
    for person in people:
        other_family, other_given = _name_key(person.get("name"))
        if other_family != family:
            continue
        if given and other_given:
            short, long_ = sorted((given, other_given), key=len)
            initial_only = len(short.replace(" ", "")) <= 1 or short.endswith(".")
            if not (long_.startswith(short) if not initial_only else long_.startswith(short[0])):
                continue
        candidates.append(person)

    return candidates[0] if len(candidates) == 1 else None


def enrich_authors_from_dois(records, max_workers=8):
    """Ask each profile's documents, by DOI, who wrote them - and keep what comes
    back only where it can belong to exactly one author of that paper.

    Two things come out of it, both of which clustering already knows how to use:
    an ORCID (proof of identity, and proof of *non*-identity between two profiles
    holding different ones) and an affiliation (a shared organisation is a merge
    rule on its own). Sets `id_fix`, `current_organization` and `doi` on the
    records in place, and returns a summary of what was found.
    """
    import concurrent.futures

    references = []
    for record in records:
        docs = record.get("author_of") or []
        references.extend(str(d) for d in (docs if isinstance(docs, list) else [docs]))

    context = fetch_document_context(references)
    for record in records:
        docs = record.get("author_of") or []
        docs = docs if isinstance(docs, list) else [docs]
        found = [context.get(str(d), {}) for d in docs]
        record["doi"] = sorted({doi for entry in found for doi in entry.get("doi", [])})
        # Who this profile published with, from the documents index alone. Papers
        # with a cast of hundreds are skipped: everyone on one shares everyone else.
        record["co_authors"] = sorted({
            key for entry in found if 0 < len(entry.get("authors", [])) <= CO_AUTHOR_LIMIT
            for key in co_author_keys(trusted_name(record), entry["authors"])})
        # The same papers, named the way clustering keys its `documents` set, so it
        # can subtract them: being on one is not evidence of being the same person.
        record["crowded_documents"] = sorted(
            {str(document) for document, entry in zip(docs, found)
             if len(entry.get("authors", [])) > CO_AUTHOR_LIMIT}
            | {f"doi:{doi}" for entry in found if len(entry.get("authors", [])) > CO_AUTHOR_LIMIT
               for doi in entry.get("doi", [])})

    distinct = sorted({doi for record in records for doi in record["doi"]})
    summary = {"documents": len(references),
               "with a doi": sum(1 for entry in context.values() if entry["doi"]),
               "distinct dois": len(distinct),
               "with co-authors": sum(1 for record in records if record["co_authors"]),
               "orcid": 0, "organization": 0, "ambiguous": 0}
    if not distinct:
        return summary

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(authors_from_doi, distinct))  # fills the cache, politely and in parallel

    for record in records:
        name = trusted_name(record)
        organizations = set(record.get("current_organization") or [])
        for doi in record["doi"]:
            people = authors_from_doi(doi)
            if not people:
                continue
            match = match_author_in_work(name, people)
            if match is None:
                summary["ambiguous"] += 1
                continue
            if match["orcid"] and not record.get("id_fix"):
                record["id_fix"] = match["orcid"]
                record["orcid_source"] = f"doi:{doi}"
                summary["orcid"] += 1
            if match["organizations"]:
                organizations |= set(match["organizations"])
        if organizations != set(record.get("current_organization") or []):
            record["current_organization"] = sorted(organizations)
            summary["organization"] += 1

    return summary


def cluster_authors(records):
    """PHASE 2 - decide which resolved records are the same person.

    Deterministic: the input is sorted first, matches are transitive (union-find)
    rather than first-seen-wins, and the master name for each cluster is chosen
    by rule instead of by arrival order. Pure - no network, no shared state.
    """
    # Sort so the outcome cannot depend on the order futures happened to complete.
    ordered = sorted(records, key=lambda r: (trusted_name(r), str(r.get("id") or "")))

    contexts = []
    for record in ordered:
        docs = record.get("author_of") or []
        docs_list = docs if isinstance(docs, list) else [docs]
        topics = record.get("topic") or []
        orgs = record.get("current_organization") or []
        full_vars, init_vars = generate_name_variants(trusted_name(record))
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
            #
            # The DOI counts as the same kind of evidence, and reaches further:
            # one paper is harvested from several repositories and lands under a
            # different GoTriple id each time, so co-authors on it can look
            # unrelated until the DOI puts the copies back together.
            #
            # Papers with a cast of hundreds are dropped, for the same reason
            # CO_AUTHOR_LIMIT drops them on the co-author side: a CMS paper lists
            # twenty different Wangs, so "both are on it" says nothing about which
            # of them either profile is.
            "documents": ({str(d) for d in docs_list}
                          | {f"doi:{d}" for d in (record.get("doi") or [])})
                         - set(record.get("crowded_documents") or []),
            "co_authors": set(record.get("co_authors") or []),
            "informative": name_is_informative(trusted_name(record)),
        })

    parent = list(range(len(contexts)))
    # The ORCID a whole component carries, keyed by its root. "Two different
    # ORCIDs are two different people" has to hold for the *cluster*, not just for
    # the pair being looked at: refusing to join A and B directly achieves nothing
    # if A joins an ORCID-less C on a shared paper and C then joins B. Measured on
    # 500 "Wang, Y." profiles, that chain built one cluster of 130 records holding
    # 18 distinct ORCIDs.
    component_orcid = {index: context["orcid"] for index, context in enumerate(contexts)
                       if context["orcid"]}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri == rj:
            return
        left, right = component_orcid.get(ri), component_orcid.get(rj)
        if left and right and left != right:
            return  # the merge would put two people in one identity
        root, absorbed = min(ri, rj), max(ri, rj)
        parent[absorbed] = root
        if left or right:
            component_orcid[root] = left or right
        component_orcid.pop(absorbed, None)

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

            # The same document, or a shared organisation, is proof enough on its
            # own - even for a name as common as "Wang, Y.".
            if shared_document or org_overlap:
                union(i, j)
                continue

            # Two papers by different people who happen to share a name will not
            # share a circle of collaborators; two papers by the same person
            # usually do. One shared co-author can be coincidence (a common
            # surname, a large group), so two are required, and papers with a
            # cast of hundreds were excluded when the sets were built.
            if len(a["co_authors"] & b["co_authors"]) >= 2:
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
            record["aka_of"] = "" if is_master else trusted_name(master["record"])
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
        -len(trusted_name(c["record"])),
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
        # Enrichment may complete a name, never swap it for another person's.
        if found_name and _is_same_person(original_text, found_name):
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
            # This used to `break` with an empty body, which left is_aka False and
            # sent the record on to be registered as a brand new author - the
            # strongest evidence there is, silently discarded. Two records with the
            # same ORCID are the same person, so the merge happens here.
            if extracted_orcid and registered.get('orcid') == extracted_orcid:
                is_aka = True
                aka_primary_name = registered['primary_name']
                registered['topics'].update(topics)
                registered['organizations'].update(organizations)
                registered['domains'].update(document_domains)
                registered['full_variants'].update(full_vars)
                registered['initial_variants'].update(init_vars)
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