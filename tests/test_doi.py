"""Offline checks for empty-DOI recovery helpers.

    python -m tests.test_doi
"""
import sys

from src.functions_doi import (
    build_doi_query,
    normalise_doi,
    parse_doi_from_record,
    titles_match,
    validate_doi,
)


def check(label, ok):
    print(f"   {'ok ' if ok else 'FAIL'} {label}")
    return not ok


def main():
    failures = 0

    failures += check(
        "doi.org prefix strips",
        normalise_doi("https://doi.org/10.7939/R3XP6V886") == "10.7939/R3XP6V886",
    )
    failures += check(
        "handle suffix is not a DOI",
        normalise_doi("10402/era.10402") is None,
    )
    failures += check(
        "ISSN-looking token without slash is not a DOI",
        normalise_doi("10.1234") is None,
    )
    failures += check(
        "parse skips empty doi and handle identifiers",
        parse_doi_from_record({
            "doi": [""],
            "identifier": ["1000", "10402/era.10402"],
            "main_entity_of_page": ["http://hdl.handle.net/11089/1000"],
        }) == [],
    )
    failures += check(
        "titles_match substring",
        titles_match("Doppelbegabung of Hector Berlioz",
                     'The "Doppelbegabung" of Hector Berlioz: music and literature'),
    )
    failures += check(
        "titles_match rejects unrelated",
        not titles_match("The Rich Man and the Poor Lazarus",
                         "Unrelated chemistry paper"),
    )
    failures += check(
        "empty_string query is doi == \"\"",
        build_doi_query("empty_string") == {"term": {"doi": ""}},
    )
    failures += check(
        "absent query is field missing, not empty string",
        build_doi_query("absent")
        == {"bool": {"must_not": [{"exists": {"field": "doi"}}]}},
    )
    failures += check(
        "empty_string and absent stay separate",
        build_doi_query("empty_string") != build_doi_query("absent"),
    )

    orig_crossref = sys.modules["src.functions_doi"].fetch_crossref_work
    orig_datacite = sys.modules["src.functions_doi"].fetch_datacite_work
    try:
        sys.modules["src.functions_doi"].fetch_crossref_work = lambda doi: {}
        sys.modules["src.functions_doi"].fetch_datacite_work = lambda doi: {
            "title": ['The "Doppelbegabung" of Hector Berlioz: music and literature'],
        }
        ok, reason = validate_doi(
            "10.7939/R3XP6V886",
            'The "Doppelbegabung" of Hector Berlioz: music and literature',
        )
        failures += check("DataCite accepts when Crossref 404s", ok and reason == "datacite")

        sys.modules["src.functions_doi"].fetch_datacite_work = lambda doi: {}
        ok, reason = validate_doi("10.7939/R3XP6V886", "any title")
        failures += check(
            "unknown to both registries is rejected",
            (not ok) and reason == "not in Crossref or DataCite",
        )
    finally:
        sys.modules["src.functions_doi"].fetch_crossref_work = orig_crossref
        sys.modules["src.functions_doi"].fetch_datacite_work = orig_datacite

    print("\n" + ("all checks passed" if not failures else f"{failures} FAILURES"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
