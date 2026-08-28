"""Offline checks for malformed-name classify + repair.

    python -m tests.test_name_repair
"""
from src.functions_name import (
    classify_name_problem,
    repair_malformed_name,
    is_bare_numeric_name,
    NAME_FILTER_QUERIES,
)


def check(label, ok):
    print(f"   {'ok ' if ok else 'FAIL'} {label}")
    return 0 if ok else 1


def main():
    failures = 0

    # --- classify: one fixture per note code we repair this week ---
    cases = [
        ("email", "Ivanov, Stanislav; stanislav.ivanov@vumk.eu"),
        ("placeholder", "s.n."),
        ("encoding", "Cristina Del\xa0Biaggio"),
        ("punctuation", "Smith, J.; Jones, A. :: Brown, K."),
        ("digits", "Nebrija, Antonio de, 1444-1522"),
        ("orcid_code", "0000-0002-8447-4734"),
        ("orcid_word", "orcid:0000-0001-6893-4035"),
        ("url", "http://nationalgeographic.grid.id"),
        ("empty", ""),
        ("institution", "University of Alberta"),
        ("abnormal_case", "EDGAR V. ANDALECIO"),
        ("single_token", "Plato"),
        ("ok", "Wang, Y."),
    ]
    for code, sample in cases:
        failures += check(f"classify {code!r}", classify_name_problem(sample) == code)

    local_id = "Rui Sun (177521)"
    failures += check("local id is digits", classify_name_problem(local_id) == "digits")
    failures += check("placeholder NN", classify_name_problem("NN") == "placeholder")
    failures += check("placeholder unknown", classify_name_problem("unknown") == "placeholder")

    # ORCID pattern before generic digits (unlike P0, where digits won).
    bare = "0000-0002-8447-4734"
    failures += check("bare ORCID is orcid_code, not digits",
                      classify_name_problem(bare) == "orcid_code"
                      and not is_bare_numeric_name(bare))
    embedded = "Wang, Y. 0000-0002-1825-0097"
    failures += check("ORCID before digits on mixed name",
                      classify_name_problem(embedded) == "orcid_code")

    # Lifespan is digits, not a bare numeric / repository id.
    lifespan = "Nebrija, Antonio de, 1444-1522"
    failures += check("lifespan is not bare numeric",
                      classify_name_problem(lifespan) == "digits"
                      and not is_bare_numeric_name(lifespan))
    failures += check("all-digit name is bare numeric",
                      is_bare_numeric_name("177521")
                      and classify_name_problem("177521") == "digits")

    # --- repair ---
    email = repair_malformed_name("Ivanov, Stanislav; stanislav.ivanov@vumk.eu")
    failures += check("strip email, keep person",
                      email["problem"] == "email"
                      and email["action"] == "cleaned"
                      and email["repaired"] == "Ivanov, Stanislav")

    dummy = repair_malformed_name("s.n.")
    failures += check("placeholder dropped",
                      dummy["action"] == "dropped" and dummy["repaired"] is None)

    encoding = repair_malformed_name("Cristina Del\xa0Biaggio")
    failures += check("NFC + nbsp -> space",
                      encoding["problem"] == "encoding"
                      and encoding["repaired"] == "Cristina Del Biaggio")

    punct = repair_malformed_name("Smith, J.; Jones, A. :: Brown, K.")
    failures += check("punctuation keeps first person token",
                      punct["problem"] == "punctuation"
                      and punct["repaired"] == "Smith, J."
                      and punct["action"] == "cleaned")

    years = repair_malformed_name(lifespan)
    failures += check("strip trailing lifespan years",
                      years["repaired"] == "Nebrija, Antonio de"
                      and years["action"] == "cleaned")

    paren = repair_malformed_name(local_id)
    failures += check("strip parenthetical local id",
                      paren["repaired"] == "Rui Sun"
                      and paren["action"] == "cleaned")

    work_date = repair_malformed_name(
        "Ab historia proprie figurativa: Visual Images as Exegetical Instruments, 1400-1700")
    failures += check("trailing work-date looks like a lifespan (partial / stripped)",
                      work_date["problem"] == "digits"
                      and "1400-1700" not in (work_date["repaired"] or ""))

    paren_years = repair_malformed_name("Adolphe Joly (1820-1878)")
    failures += check("parenthetical lifespan at end",
                      paren_years["repaired"] == "Adolphe Joly"
                      and paren_years["action"] == "cleaned")

    html_ent = repair_malformed_name("&Aacute;. Lopez-Urrutia")
    failures += check("HTML entity semicolon is not an author separator",
                      html_ent["repaired"] and "Lopez" in html_ent["repaired"])

    orcid_only = repair_malformed_name(bare)
    failures += check("bare ORCID -> lookup, not stripped as digits",
                      orcid_only["problem"] == "orcid_code"
                      and orcid_only["action"] == "orcid_lookup"
                      and orcid_only["orcid"] == "0000-0002-8447-4734"
                      and orcid_only["repaired"] is None)

    inst = repair_malformed_name("University of Alberta")
    failures += check("institution classify-only, not repaired",
                      inst["action"] == "classify_only"
                      and inst["repaired"] == "University of Alberta")

    case = repair_malformed_name("EDGAR V. ANDALECIO")
    failures += check("abnormal_case classify-only", case["action"] == "classify_only")

    mono = repair_malformed_name("Plato")
    failures += check("single_token classify-only", mono["action"] == "classify_only")

    ok = repair_malformed_name("Wang, Y.")
    failures += check("ok name unchanged", ok["problem"] == "ok" and ok["repaired"] == "Wang, Y.")

    # Existing filters must still be selectable.
    for key in ("empty", "orcid", "junk", "malformed"):
        failures += check(f"filter {key} still present", key in NAME_FILTER_QUERIES)
    failures += check("new repairable filter", "repairable" in NAME_FILTER_QUERIES)

    from src.functions_name import NO_AUTHOR_QUERY
    failures += check(
        "no-author query is must_not nested author",
        "nested" in NO_AUTHOR_QUERY["bool"]["must_not"][0],
    )

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
