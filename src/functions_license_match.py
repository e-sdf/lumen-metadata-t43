"""Map `original_license` to a GoTriple `lic_*` code and/or SPDX.

Why this exists: `license` is ~92 % `undefined` even when `original_license`
already holds a CC URL, a Cairn token, or an EU-repo *access* string. Scraping
the landing page first (Paulo) mixes those three natures into `scrapped_license`
and pays HTTP for documents that were classifiable locally.

Access is not a licence. `info:eu-repo/semantics/openAccess` says who may read;
it does not say under which terms the work may be reused. That value belongs in
`conditions_of_access`. Creative Commons URLs (http/https, trailing slash,
`/legalcode`, local labels) collapse to one SPDX id and to `lic_creative-commons`.
Repository legalese (HAL authorisation, EconStor Nutzungsbedingungen) is not
SPDX; Cairn is the one synonym that already has a GoTriple code (`lic_cairn`).
"""
from __future__ import annotations

import re

# GoTriple stores every CC flavour under one code. SPDX keeps the flavour.
GOTRIPLE_CC = "lic_creative-commons"
GOTRIPLE_CAIRN = "lic_cairn"

CC_URL_RE = re.compile(
    r"creativecommons\.org/licenses/([a-z0-9-]+)/([0-9.]+)",
    re.I,
)
CC0_URL_RE = re.compile(
    r"creativecommons\.org/publicdomain/zero(?:/([0-9.]+))?",
    re.I,
)
# Local labels ("CC BY-NC-ND: Creative Commons Uznanie… 4.0") often put the
# version later in the string, and a colon where a slash would sit on a URL.
CC_TEXT_RE = re.compile(
    r"\bcc(?:\s|-)?by(?:(?:\s|-)?(nc))?(?:(?:\s|-)?(nd|sa))?(?:[\s/:]+([0-9.]+))?",
    re.I,
)
VERSION_RE = re.compile(r"\b([1-4]\.[0-9])\b")

# EU-repo / common OA vocab → conditions_of_access (COAR access rights).
ACCESS_SYNONYMS = {
    "info:eu-repo/semantics/openaccess": "openAccess",
    "info:eu-repo/semantics/restrictedaccess": "restrictedAccess",
    "info:eu-repo/semantics/closedaccess": "closedAccess",
    "info:eu-repo/semantics/embargoedaccess": "embargoedAccess",
    "openaccess": "openAccess",
    "open access": "openAccess",
    "free access": "openAccess",
    "restrictedaccess": "restrictedAccess",
    "restricted access": "restrictedAccess",
    "closedaccess": "closedAccess",
    "closed access": "closedAccess",
    "embargoedaccess": "embargoedAccess",
}

# Exact / substring repository notices. kind: license | repository | copyright
NOTICE_SYNONYMS = (
    ("cairn", "license", GOTRIPLE_CAIRN, None),
    ("about.hal.science/hal-authorisation", "repository", "", None),
    ("hal-authorisation", "repository", "", None),
    ("econstor.eu/dspace/nutzungsbedingungen", "repository", "", None),
    ("dialnet", "repository", "", None),
    ("rightsstatements.org/vocab/inc", "copyright", "other", None),
    ("all rights reserved", "copyright", "other", None),
    ("wszystkie prawa zastrzeżone", "copyright", "other", None),
)

CC_SPDX = {
    "by": "CC-BY",
    "by-sa": "CC-BY-SA",
    "by-nd": "CC-BY-ND",
    "by-nc": "CC-BY-NC",
    "by-nc-sa": "CC-BY-NC-SA",
    "by-nc-nd": "CC-BY-NC-ND",
}


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _pieces(value):
    """Split combined harvest strings ('openAccess ; https://creativecommons.org/...')."""
    for item in _as_list(value):
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        for part in re.split(r"\s*;\s*", text):
            part = part.strip()
            if part:
                yield part


def _fold(text):
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _cc_spdx(flavour, version):
    key = flavour.lower().strip("-")
    prefix = CC_SPDX.get(key)
    if not prefix:
        return None
    version = (version or "").rstrip(".")
    return f"{prefix}-{version}" if version else prefix


def classify_text(text):
    """Classify one string. Returns a dict; empty strings mean 'not this kind'."""
    raw = str(text or "").strip()
    folded = _fold(raw)
    out = {
        "license": "",
        "spdx": None,
        "access": "",
        "kind": "unmapped",
        "matched_from": raw,
    }
    if not folded or folded.startswith("[unknown"):
        return out

    if cc0 := CC0_URL_RE.search(raw):
        ver = cc0.group(1) or "1.0"
        out.update(license=GOTRIPLE_CC, spdx=f"CC0-{ver}", kind="license")
        return out

    if cc := CC_URL_RE.search(raw):
        spdx = _cc_spdx(cc.group(1), cc.group(2))
        if spdx:
            out.update(license=GOTRIPLE_CC, spdx=spdx, kind="license")
            return out

    # OpenAlex short codes and Paulo's "CC BY 4.0" / "CC BY-NC-ND" display form.
    stripped = folded.replace(" ", "-")
    if stripped in ("cc0", "cc-0"):
        out.update(license=GOTRIPLE_CC, spdx="CC0-1.0", kind="license")
        return out
    if m := re.fullmatch(r"cc-?by(-nc)?(-nd|-sa)?(?:-([0-9.]+))?", stripped):
        flavour = "by" + (m.group(1) or "") + (m.group(2) or "")
        spdx = _cc_spdx(flavour, m.group(3))
        if spdx:
            out.update(license=GOTRIPLE_CC, spdx=spdx, kind="license")
            return out
    if m := CC_TEXT_RE.search(raw):
        flavour = "by"
        if m.group(1):
            flavour += "-nc"
        if m.group(2):
            flavour += "-" + m.group(2).lower()
        version = m.group(3) or (VERSION_RE.search(raw).group(1) if VERSION_RE.search(raw) else None)
        spdx = _cc_spdx(flavour, version)
        if spdx:
            out.update(license=GOTRIPLE_CC, spdx=spdx, kind="license")
            return out

    if folded in ACCESS_SYNONYMS:
        out.update(access=ACCESS_SYNONYMS[folded], kind="access")
        return out
    # Publisher-OA / generic OA strings Paulo's scraper emits.
    if folded.startswith("open access (publisher") or folded in (
        "generic open access",
        "hal authorisation",
    ):
        out.update(access="openAccess", kind="access")
        return out

    for needle, kind, license_code, spdx in NOTICE_SYNONYMS:
        if needle in folded:
            out.update(license=license_code, spdx=spdx, kind=kind)
            if kind == "license":
                return out
            if kind == "access":
                out["access"] = license_code
            return out

    return out


def classify_original_license(value):
    """Fold every original_license value. A CC/Cairn hit wins over access.

    Returns license_fix / conditions_of_access_fix / spdx / kind / leftover.
    kind is license | access | repository | copyright | mixed | unmapped | empty.
    """
    pieces = list(_pieces(value))
    blank = {
        "license_fix": "",
        "conditions_of_access_fix": "",
        "spdx": None,
        "kind": "empty",
        "matched_from": "",
        "leftover": [],
        "license_source": "",
    }
    if not pieces:
        return blank

    license_hit = None
    access_hit = None
    other_kind = None
    leftover = []
    for piece in pieces:
        hit = classify_text(piece)
        if hit["kind"] == "license" and hit["license"]:
            if license_hit is None:
                license_hit = hit
        elif hit["kind"] == "access" and hit["access"]:
            if access_hit is None:
                access_hit = hit
        elif hit["kind"] in ("repository", "copyright"):
            if other_kind is None:
                other_kind = hit
        else:
            leftover.append(piece)

    if license_hit and access_hit:
        kind = "mixed"
    elif license_hit:
        kind = "license"
    elif access_hit:
        kind = "access"
    elif other_kind:
        kind = other_kind["kind"]
    else:
        kind = "unmapped"

    license_fix = ""
    spdx = None
    source = ""
    matched = ""
    if license_hit:
        license_fix = license_hit["license"]
        spdx = license_hit["spdx"]
        source = "original_license"
        matched = license_hit["matched_from"]

    access = access_hit["access"] if access_hit else ""
    if not matched:
        matched = (access_hit or other_kind or {}).get("matched_from") or pieces[0]

    return {
        "license_fix": license_fix,
        "conditions_of_access_fix": access,
        "spdx": spdx,
        "kind": kind,
        "matched_from": matched,
        "leftover": leftover,
        "license_source": source,
    }


def is_usable_license_code(code):
    """True when the code is a GoTriple licence, not access and not a placeholder."""
    text = str(code or "").strip().lower()
    if not text or text in ("other", "undefined", "already classified"):
        return False
    if text.startswith("lic_"):
        return True
    if text.startswith("cc-") or text.startswith("cc ") or text.startswith("cc0"):
        return True
    return False


def apply_original_license(doc):
    """Write license_fix / SPDX / access onto a document dict. No HTTP."""
    classified = classify_original_license(doc.get("original_license"))
    doc["license_fix"] = classified["license_fix"]
    doc["spdx"] = classified["spdx"] or ""
    doc["conditions_of_access_fix"] = classified["conditions_of_access_fix"]
    doc["license_kind"] = classified["kind"]
    doc["license_source"] = classified["license_source"]
    doc["license_matched_from"] = classified["matched_from"]
    return classified


def metadata_has_cc(original_license):
    """True when original_license already encodes a Creative Commons licence.

    Paulo's scraper tested `'creative_commons' in …`, which never matches a
    harvested CC URL (`creativecommons.org/licenses/by/4.0/`).
    """
    hit = classify_original_license(original_license)
    return hit["license_fix"] == GOTRIPLE_CC or bool(hit["spdx"])
