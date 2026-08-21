"""Fixtures for cluster_authors - the cases the merge rules have to get right.

    venv/bin/python -m tests.test_clustering

Each fixture is a set of profiles plus the identity grouping we expect. The
point is that clustering is a pure function, so these run offline in a second
and let the thresholds be tuned without a full API pass.
"""

import random
import sys

from src.functions_name import cluster_authors, name_is_informative


def profile(pid, name, topics=(), docs=(), orgs=(), orcid=None, dois=(), co_authors=()):
    return {
        "id": pid, "fullname": name, "_resolved_name": name, "id_fix": orcid,
        "topic": list(topics), "author_of": list(docs), "current_organization": list(orgs),
        "doi": list(dois), "co_authors": list(co_authors),
    }


def groups_of(records):
    """Cluster and return the identity groups as a comparable set of frozensets.

    Groups by `cluster_id`, not by name: two unrelated people can share a name,
    so grouping on `aka_of` would merge distinct clusters in the assertion.
    """
    members = {}
    for record in cluster_authors(records):
        members.setdefault(record["cluster_id"], set()).add(record["id"])
    return {frozenset(v) for v in members.values()}


# --- fixtures -------------------------------------------------------------

FIXTURES = [
    (
        "common name, nothing shared -> stays apart",
        [profile("a", "Wang, Y. Y.", topics=["geo"], docs=["doc1"]),
         profile("b", "Wang, Y. Y.", topics=["geo"], docs=["doc2"])],
        {frozenset({"a"}), frozenset({"b"})},
    ),
    (
        "common name, same repository only -> still apart",
        [profile("a", "Wang, Y.", topics=["geo"], docs=["ftosti:oai:x:1"]),
         profile("b", "Wang, Y.", topics=["geo"], docs=["ftosti:oai:x:2"])],
        {frozenset({"a"}), frozenset({"b"})},
    ),
    (
        "common name, co-authors on the same document -> merge",
        [profile("a", "Wang, Y. Y.", docs=["shared-doc"]),
         profile("b", "Wang, Y-Y", docs=["shared-doc"])],
        {frozenset({"a", "b"})},
    ),
    (
        "common name, shared organisation -> merge",
        [profile("a", "Wang, Y.", orgs=["Tsinghua"], docs=["d1"]),
         profile("b", "Wang, Y.", orgs=["Tsinghua"], docs=["d2"])],
        {frozenset({"a", "b"})},
    ),
    (
        "same paper under two harvested ids, joined by the DOI -> merge",
        [profile("a", "Wang, Y.", docs=["ftinsu:oai:HAL:hal-1"], dois=["10.1000/xyz"]),
         profile("b", "Wang, Y.", docs=["ftceafr:oai:HAL:hal-1"], dois=["10.1000/xyz"])],
        {frozenset({"a", "b"})},
    ),
    (
        "two co-authors in common -> merge",
        [profile("a", "Wang, Y.", docs=["d1"], co_authors=["xu|x", "wei|f", "feng|x"]),
         profile("b", "Wang, Y.", docs=["d2"], co_authors=["xu|x", "wei|f", "zou|p"])],
        {frozenset({"a", "b"})},
    ),
    (
        "one co-author in common is coincidence -> apart",
        [profile("a", "Wang, Y.", docs=["d1"], co_authors=["chen|j", "li|x"]),
         profile("b", "Wang, Y.", docs=["d2"], co_authors=["chen|j", "zhu|m"])],
        {frozenset({"a"}), frozenset({"b"})},
    ),
    (
        "distinctive name plus topic overlap -> merge",
        [profile("a", "Pimenta, João Paulo", topics=["hist"], docs=["d1"]),
         profile("b", "João Paulo Pimenta", topics=["hist"], docs=["d2"])],
        {frozenset({"a", "b"})},
    ),
    (
        "distinctive name, nothing in common -> apart",
        [profile("a", "Pimenta, João Paulo", topics=["hist"], docs=["d1"]),
         profile("b", "João Paulo Pimenta", topics=["chem"], docs=["d2"])],
        {frozenset({"a"}), frozenset({"b"})},
    ),
    (
        "same ORCID beats everything -> merge",
        [profile("a", "Wang, Y.", docs=["d1"], orcid="0000-0002-1825-0097"),
         profile("b", "Y. Wang", docs=["d2"], orcid="0000-0002-1825-0097")],
        {frozenset({"a", "b"})},
    ),
    (
        "different ORCIDs are never merged, however alike",
        [profile("a", "Pimenta, João Paulo", topics=["hist"], docs=["d"], orcid="0000-0002-1825-0097"),
         profile("b", "Pimenta, João Paulo", topics=["hist"], docs=["d"], orcid="0000-0003-1111-2222")],
        {frozenset({"a"}), frozenset({"b"})},
    ),
    (
        "transitive: a-b and b-c means one identity, not two pairs",
        [profile("a", "Pimenta, João Paulo", topics=["hist"], docs=["d1"]),
         profile("b", "João Paulo Pimenta", topics=["hist"], docs=["d2"]),
         profile("c", "Pimenta, João Paulo", topics=["hist"], docs=["d3"])],
        {frozenset({"a", "b", "c"})},
    ),
]


def main():
    failures = 0

    print("name_is_informative:")
    for name, expected in [("Wang, Y. Y.", False), ("Wang, Y-Y", False), ("Wang, Y., Y.", False),
                           ("Pimenta, João Paulo", True), ("Pedro Paulo Pimenta", True),
                           ("Marušić, Ana", True), ("", False)]:
        got = name_is_informative(name)
        ok = got == expected
        failures += not ok
        print(f"   {'ok ' if ok else 'FAIL'} {name!r:26} -> {got}")

    print("\nclustering fixtures:")
    for label, records, expected in FIXTURES:
        got = groups_of([dict(r) for r in records])
        ok = got == expected
        failures += not ok
        print(f"   {'ok ' if ok else 'FAIL'} {label}")
        if not ok:
            print(f"        expected {sorted(map(sorted, expected))}")
            print(f"        got      {sorted(map(sorted, got))}")

    print("\ndeterminism (same fixture, 5 shuffles):")
    for label, records, _ in FIXTURES:
        seen = set()
        for seed in range(5):
            shuffled = [dict(r) for r in records]
            random.Random(seed).shuffle(shuffled)
            seen.add(frozenset(groups_of(shuffled)))
        ok = len(seen) == 1
        failures += not ok
        print(f"   {'ok ' if ok else 'FAIL'} {label}")

    print("\n" + ("all checks passed" if not failures else f"{failures} FAILURES"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
