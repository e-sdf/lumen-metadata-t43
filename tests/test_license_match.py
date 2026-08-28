"""Offline checks for original_license → lic_* / SPDX / access.

    python -m tests.test_license_match
"""
import sys

from src.functions_license_match import classify_original_license, classify_text


def check(label, ok):
    print(f"   {'ok ' if ok else 'FAIL'} {label}")
    return 0 if ok else 1


def main():
    failures = 0

    cc = classify_text("https://creativecommons.org/licenses/by/4.0/")
    failures += check("https CC BY 4.0 -> SPDX + lic_cc",
                      cc["spdx"] == "CC-BY-4.0" and cc["license"] == "lic_creative-commons")

    alias = classify_text("http://creativecommons.org/licenses/by-nc-nd/4.0/legalcode")
    failures += check("http + legalcode canonicalises",
                      alias["spdx"] == "CC-BY-NC-ND-4.0")

    access = classify_text("info:eu-repo/semantics/openAccess")
    failures += check("EU-repo openAccess is access, not a licence",
                      access["kind"] == "access" and access["access"] == "openAccess"
                      and not access["license"])

    casing = classify_text("info:eu-repo/semantics/OpenAccess")
    failures += check("OpenAccess casing still access",
                      casing["access"] == "openAccess")

    cairn = classify_text("Cairn")
    failures += check("Cairn -> lic_cairn",
                      cairn["license"] == "lic_cairn" and cairn["kind"] == "license")

    hal = classify_text("https://about.hal.science/hal-authorisation-v1/")
    failures += check("HAL authorisation is repository legalese",
                      hal["kind"] == "repository" and not hal["license"])

    paulo_oa = classify_text("Open Access (Publisher: MDPI)")
    failures += check("Paulo OA-publisher string is access",
                      paulo_oa["kind"] == "access")

    combo = classify_original_license(
        "info:eu-repo/semantics/openAccess ; https://creativecommons.org/licenses/by/4.0/legalcode"
    )
    failures += check("combined value splits access + CC",
                      combo["kind"] == "mixed"
                      and combo["license_fix"] == "lic_creative-commons"
                      and combo["spdx"] == "CC-BY-4.0"
                      and combo["conditions_of_access_fix"] == "openAccess")

    empty = classify_original_license(["", None])
    failures += check("empty original_license is empty", empty["kind"] == "empty")

    polish = classify_text(
        "CC BY-NC-ND: Creative Commons Uznanie autorstwa - Uzycie niekomercyjne - Bez utworow zaleznych 4.0"
    )
    failures += check("Polish CC BY-NC-ND 4.0 label -> SPDX",
                      polish["spdx"] == "CC-BY-NC-ND-4.0")

    under = classify_text(
        "Content available online under CC BY-NC-SA 3.0 (http://creativecommons.org/licenses/by-nc-sa/3.0/)"
    )
    failures += check("embedded CC URL wins",
                      under["spdx"] == "CC-BY-NC-SA-3.0")

    from src.functions_license_match import metadata_has_cc
    failures += check("CC URL counts as CC in metadata (not creative_commons token)",
                      metadata_has_cc("https://creativecommons.org/licenses/by/4.0/"))
    failures += check("creative_commons token is not required",
                      not metadata_has_cc("undefined"))

    reserved = classify_text("All rights reserved")
    failures += check("all rights reserved is copyright, not a licence code",
                      reserved["kind"] == "copyright" and reserved["license"] == "other")
    reserved_doc = classify_original_license("All rights reserved")
    failures += check("copyright does not fill license_fix",
                      reserved_doc["license_fix"] == "" and reserved_doc["kind"] == "copyright")

    from src.functions_license import build_license_query, process_document

    present = build_license_query("unresolved", original_license="present")
    absent = build_license_query("unresolved", original_license="absent")
    failures += check(
        "present query requires a non-empty original_license",
        "must" in present.get("bool", {}) and present != absent,
    )
    failures += check(
        "absent query is the HTTP remainder (must_not the present clause)",
        "must_not" in absent.get("bool", {}),
    )
    failures += check(
        "absent and present wrap the same unresolved base",
        present["bool"]["must"][0] == absent["bool"]["must"][0],
    )
    local = process_document({
        "license": ["undefined"],
        "original_license": ["https://creativecommons.org/licenses/by/4.0/"],
        "main_entity_of_page": ["https://example.invalid/never-hit"],
    }, scrape=False)
    failures += check("process_document matches locally and skips HTTP",
                      local["license_fix"] == "lic_creative-commons"
                      and local["license_source"] == "original_license"
                      and local["spdx"] == "CC-BY-4.0")

    access_only = process_document({
        "license": ["undefined"],
        "original_license": ["info:eu-repo/semantics/openAccess"],
    }, scrape=False)
    failures += check("openAccess does not fill license_fix",
                      access_only["license_fix"] == ""
                      and access_only["conditions_of_access_fix"] == "openAccess"
                      and access_only["license_kind"] == "access")

    no_ol = process_document({
        "license": ["undefined"],
        "original_license": [],
        "main_entity_of_page": ["https://example.invalid/never-hit"],
    }, scrape=False)
    failures += check("absent original_license stays empty without HTTP",
                      no_ol.get("license_fix", "") in ("", None)
                      and "no original_license" in str(no_ol.get("scrapped_license", "")))

    print("\n" + ("all checks passed" if not failures else f"{failures} FAILURES"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
