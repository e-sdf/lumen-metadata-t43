import collections
import os
from functools import partial
import numpy as np
import pandas as pd
import concurrent.futures

# Import helper functions from your separated files
from src.functions_license import (
    fetch_gotriple_documents,
    fetch_elastic_documents,
    process_document,
)
from src.functions_doi import fetch_elastic_doi_documents, recover_doi, classify_doi_problem
from src.functions_name import (fetch_gotriple_authors, fetch_elastic_authors, process_author,
                                resolve_author, cluster_authors, recover_names_from_documents, classify_name_problem,
                                fetch_top_author_names, fetch_names_from_documents, best_name_form,
                                resolve_repository_ids, enrich_authors_from_dois)

# Paths are anchored to the project root, so a run works from any working directory.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, 'data', 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Name-repair tasks: task name -> (selection filter, label for the log).
NAME_REPAIR_TASKS = {
    "empty_names":  ("empty",     "empty full names"),
    "orcid_names":  ("orcid",     "names with an ORCID inside them"),
    "junk_names":   ("junk",      "names that are a URL or bare digits"),
    "broken_names": ("malformed", "all broken names (empty, ORCID inside, URLs, numeric)"),
}

LICENSE_MATCH_FIELDS = [
    "id", "provider", "license", "original_license", "publisher",
    "main_entity_of_page", "doi", "headline",
]


def run_license_pipeline(num_docs:str="1000", max_workers:int=8, source:str="gotriple",
                         license_filter:str="unresolved", scrape:bool=True,
                         original_license:str="any"):
    """Recover licenses for documents that are missing one.

    Match `original_license` first (local, no HTTP). Scrape the landing page
    only when that match did not yield a `lic_*` code — unless scrape=False,
    which is the local-only step.

    source: "gotriple" reads through the public REST API (capped at one page),
            "elastic"  queries the production index directly and pages with
                       search_after, so batches beyond 10,000 work.
    original_license: "any" | "present" | "absent" (Elasticsearch only).
    """
    print("\n--- STARTING DOCUMENT LICENSE PIPELINE ---")
    print(f"Source: {source} | filter: license={license_filter} "
          f"| original_license={original_license} | scrape={scrape} | requested: {num_docs}")

    if source == "elastic":
        docs = fetch_elastic_documents(
            size=num_docs, license_filter=license_filter,
            original_license=original_license,
            source_fields=LICENSE_MATCH_FIELDS if not scrape else None,
        )
    elif source == "gotriple":
        docs = fetch_gotriple_documents(size=num_docs, license_filter=license_filter)
    else:
        raise ValueError(f"Unknown source '{source}' - expected 'gotriple' or 'elastic'")

    if not docs:
        print("No documents found to process.")
        return

    print(f"Processing {len(docs)} documents concurrently...")
    processed_docs = []
    worker = partial(process_document, scrape=scrape)

    # Threads are throttled per host in rate_limit.py, so this only bounds how
    # many *different* hosts we talk to at once. scrape=False never opens a socket.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, future in enumerate(concurrent.futures.as_completed(
                {executor.submit(worker, d): d for d in docs}), 1):
            res = future.result()
            processed_docs.append(res)
            print(f"[Doc {i}/{len(docs)}] Processed -> {res.get('scrapped_license')}")

    df_docs = pd.DataFrame(processed_docs)
    desired = ["license_fix", "spdx", "conditions_of_access_fix",
               "license_kind", "license_source", "scrapped_license"]
    if "license" in df_docs.columns:
        loc = df_docs.columns.get_loc("license") + 1
        for col in reversed(desired):
            if col in df_docs.columns:
                df_docs.insert(loc, col, df_docs.pop(col))

    suffix = "match" if not scrape else "fix"
    output_filename = os.path.join(OUTPUT_DIR, f'license_{suffix}_{source}.xlsx')
    df_docs.to_excel(output_filename, index=False)
    print(f"-> License data saved to '{output_filename}'")

    print_license_summary(df_docs)


def run_license_match_pipeline(num_docs:str="500", source:str="elastic"):
    """Step 0: classify original_license locally. No HTTP."""
    return run_license_pipeline(
        num_docs=num_docs, max_workers=8, source=source,
        license_filter="unresolved", scrape=False, original_license="present",
    )


def print_license_summary(df_docs):
    """Report how original_license classified, and (if scraped) what HTTP added."""
    print("\n" + "="*60)
    print("LICENSE RECOVERY SUMMARY")
    print("="*60)

    print(f"-> Documents in batch           : {len(df_docs)}")

    if "license_kind" in df_docs.columns:
        kinds = df_docs["license_kind"].fillna("empty").astype(str)
        licence = df_docs["license_fix"].fillna("").astype(str).str.startswith("lic_")
        print(f"-> Real licence (lic_*)         : {int(licence.sum())} / {len(df_docs)}"
              f" ({licence.mean() * 100:.1f}%)")
        print(f"-> Access only                  : {int((kinds == 'access').sum())}")
        print(f"-> Repository / copyright       : "
              f"{int(kinds.isin(['repository', 'copyright']).sum())}")
        print(f"-> Unmapped / empty             : "
              f"{int(kinds.isin(['unmapped', 'empty']).sum())}")

        print("\n--- Kind ---")
        for value, count in kinds.value_counts().items():
            print(f"     * {value}: {count}")

        if "spdx" in df_docs.columns:
            spdx = df_docs.loc[licence, "spdx"].fillna("").astype(str)
            spdx = spdx[spdx != ""]
            if not spdx.empty:
                print("\n--- SPDX ---")
                for value, count in spdx.value_counts().head(12).items():
                    print(f"     * {value}: {count}")

        sources = df_docs.loc[licence, "license_source"].fillna("").astype(str)
        if not sources.empty:
            print("\n--- What recovered a licence ---")
            for value, count in sources.value_counts().items():
                print(f"     * {value}: {count}")
        print("="*60 + "\n")
        return

    if "scrapped_license" not in df_docs.columns:
        print("-> No scraped license column to report on.")
        print("="*60 + "\n")
        return

    results = df_docs["scrapped_license"].fillna("[unknown, no result]").astype(str)

    already_classified = results == "Already classified"
    searched = results[~already_classified]
    # Anything that isn't an "[unknown, ...]" marker is a license we recovered.
    found = searched[~searched.str.startswith("[unknown") & ~searched.str.startswith("[access")
                     & ~searched.str.startswith("[repository") & ~searched.str.startswith("[copyright")
                     & ~searched.str.startswith("[unmapped") & ~searched.str.startswith("[no original")]

    total_searched = len(searched)
    print(f"-> Already had a license        : {int(already_classified.sum())}")
    print(f"-> Searched (unresolved group)  : {total_searched}")

    if total_searched == 0:
        print("-> Nothing needed searching, so there is no recovery rate to report.")
        print("="*60 + "\n")
        return

    rate = len(found) / total_searched * 100
    print(f"-> Licenses found               : {len(found)} / {total_searched} ({rate:.1f}%)")
    print(f"-> Still unknown                : {total_searched - len(found)} ({100 - rate:.1f}%)")

    print("\n--- What was found ---")
    for value, count in found.value_counts().items():
        print(f"     * {value}: {count} ({count / total_searched * 100:.1f}%)")

    unknowns = searched[~searched.index.isin(found.index)]
    if not unknowns.empty:
        print("\n--- Why the rest failed ---")
        for reason, count in unknowns.value_counts().items():
            print(f"     * {reason}: {count} ({count / total_searched * 100:.1f}%)")

    print("="*60 + "\n")

def _resolve_in_parallel(authors, label="Resolved"):
    """Phase 1 for every record, concurrently. Order-independent by construction."""
    resolved = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(resolve_author, a): a for a in authors}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            res = future.result()
            resolved.append(res)
            print(f"[Author {i}/{len(authors)}] {label}: {res.get('id_fix')} -> {res.get('fullname_fix')}")
    return resolved


def _recover_names(authors):
    """Fix broken names, whatever is wrong with them.

    Four routes, cheapest first:
      0. a bare number that is a repository's author id -> not a person at all
      1. an ORCID sitting inside the name -> ask ORCID for the canonical name
      2. the documents index -> the name a document records for this author id
      3. strip the junk (URLs, "orcid" tokens, stray punctuation) off what is left

    Route 1 and 3 live in resolve_author(); routes 0 and 2 are batched queries.
    """
    print(f"Recovering names for {len(authors)} profiles...")

    for author in authors:
        author["name_problem"] = classify_name_problem(author.get("fullname"))
    problems = collections.Counter(a["name_problem"] for a in authors)
    print("-> what is wrong with them:", dict(problems.most_common()))

    # Route 0: settle the bare numbers that are repository ids before paying for
    # any lookup. There is no name to recover for these - they are not people -
    # so they are answered here and skipped by everything below.
    settled = []
    numeric = [a for a in authors if a["name_problem"] == "numeric"]
    if numeric:
        owners = resolve_repository_ids(numeric)
        print(f"-> {len(owners)}/{len(numeric)} numeric names are a repository's own author id, "
              "not a person")
        for author in numeric:
            if owner := owners.get(str(author.get("id"))):
                author["name_source"] = "repository id"
                author["belongs_to"] = owner
                author["fullname_fix"] = author.get("fullname")  # unchanged: nothing to fix
                author["id_fix"] = None
                settled.append(author)
        authors = [a for a in authors if a.get("name_source") != "repository id"]

    # Route 2, batched: only worth it where there is no ORCID to follow.
    without_orcid = [a for a in authors if a["name_problem"] in ("empty", "numeric", "no letters", "url instead of a name")]
    if without_orcid:
        from_documents = recover_names_from_documents(without_orcid)
        print(f"-> {len(from_documents)}/{len(without_orcid)} recovered from the documents index")
        for author in without_orcid:
            if recovered := from_documents.get(str(author.get("id"))):
                author["fullname"] = recovered
                author["name_source"] = "documents index"

    resolved = _resolve_in_parallel(authors, label="Name") + settled

    for author in resolved:
        if author.get("name_source"):
            continue
        fixed = str(author.get("fullname_fix") or "")
        if author["name_problem"] == "ok":
            author["name_source"] = "unchanged"
        elif (not fixed or fixed in ("No Name Available", "No Name Found")
                or classify_name_problem(fixed) != "ok"):
            # A "fix" that is still a bare number or a URL is not a recovery.
            author["name_source"] = "not recovered"
        elif author.get("id_fix"):
            author["name_source"] = "orcid api"
        else:
            author["name_source"] = "cleaned in place"

    print("\n--- Name recovery by problem ---")
    for problem in sorted(problems):
        group = [a for a in resolved if a["name_problem"] == problem]
        fixed = [a for a in group if a["name_source"] not in
                 ("not recovered", "unchanged", "repository id")]
        ids = [a for a in group if a["name_source"] == "repository id"]
        # a repository id is not a failed recovery: there was no name to recover
        note = f"  ({len(ids)} are repository ids, not people)" if ids else ""
        print(f"     * {problem:24} {len(fixed):>4}/{len(group):<4} recovered{note}")

    return resolved


def _disambiguate_heuristic(authors):
    """The original incremental registry. Kept for comparison - its result
    depends on the order futures complete, so runs are not reproducible."""
    print(f"Processing {len(authors)} authors concurrently (heuristic registry)...")
    processed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(process_author, a): a for a in authors}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            res = future.result()
            processed.append(res)
            print(f"[Author {i}/{len(authors)}] Processed: {res.get('id_fix')} -> {res.get('fullname_fix')}")
    return processed


def _attach_document_evidence(authors):
    """Replace each profile's `author_of` with what the documents index actually
    records for that author id, and note the name forms the documents use.

    `author` is a nested field there, so this reaches records the profile's own
    `author_of` list misses - and it needs no ORCID lookup to get a name.
    """
    ids = [str(a.get("id")) for a in authors if a.get("id")]
    found = fetch_names_from_documents(ids)
    print(f"-> document evidence for {len(found)}/{len(ids)} profiles")

    for author in authors:
        entry = found.get(str(author.get("id")))
        if not entry:
            continue
        author["author_of"] = sorted(entry["documents"] | set(author.get("author_of") or []))
        author["document_names"] = entry["names"]
        if better := best_name_form(entry["names"]):
            author["name_from_documents"] = better
    return authors


def _disambiguate_clustered(authors, enrich_from_dois=True):
    """Resolve every record first, then cluster the whole set in one pass.

    Between the two phases the profiles are asked about by DOI: the profiles
    index carries neither an ORCID nor an organisation for most people, and both
    are decisive here, so the papers themselves are the only place to get them.
    Set enrich_from_dois=False to cluster on the index alone.
    """
    resolved = _resolve_in_parallel(authors)

    if enrich_from_dois:
        print(f"\nAsking the documents' DOIs who wrote them...")
        found = enrich_authors_from_dois(resolved)
        print(f"-> {found['with a doi']}/{found['documents']} documents carry a DOI "
              f"({found['distinct dois']} distinct)")
        print(f"-> ORCID recovered for {found['orcid']} profiles, "
              f"an organisation for {found['organization']}, "
              f"co-authors for {found['with co-authors']}; "
              f"{found['ambiguous']} lookups matched no single author and were dropped")

    print(f"\nClustering {len(resolved)} resolved records...")
    clustered = cluster_authors(resolved)

    merged = sum(1 for r in clustered if r.get("is_aka") == "Yes")
    print(f"-> {len(clustered) - merged} distinct identities, {merged} records merged into them")
    return clustered


def run_doi_pipeline(num_docs: str = "100", max_workers: int = 8, kind: str = "empty_string"):
    """Backfill `doi` when the field is empty or absent. Elasticsearch only.

    kind: empty_string | absent | unusable | with_landing_page
          (see build_doi_query). empty_string is the ingest bug; keep it
          separate from a missing field.
    """
    print("\n--- STARTING DOCUMENT DOI PIPELINE ---")
    print(f"Source: elastic | filter: doi={kind} | requested: {num_docs}")

    docs = fetch_elastic_doi_documents(size=num_docs, kind=kind)
    if not docs:
        print("No documents found to process.")
        return

    for doc in docs:
        doc["doi_problem"] = classify_doi_problem(doc)

    print(f"Processing {len(docs)} documents concurrently...")
    processed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(recover_doi, d): d for d in docs}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            res = future.result()
            processed.append(res)
            print(f"[Doc {i}/{len(docs)}] {res.get('doi_source')} -> {res.get('doi_fix') or '—'}")

    df_docs = pd.DataFrame(processed)
    if "doi" in df_docs.columns and "doi_fix" in df_docs.columns:
        df_docs.insert(df_docs.columns.get_loc("doi") + 1, "doi_fix", df_docs.pop("doi_fix"))
        df_docs.insert(df_docs.columns.get_loc("doi_fix") + 1, "doi_source", df_docs.pop("doi_source"))

    output_filename = os.path.join(OUTPUT_DIR, f"doi_fix_elastic.xlsx")
    df_docs.to_excel(output_filename, index=False)
    print(f"-> DOI data saved to '{output_filename}'")
    print_doi_summary(df_docs)


def print_doi_summary(df_docs):
    print("\n" + "=" * 60)
    print("DOI RECOVERY SUMMARY")
    print("=" * 60)

    sources = df_docs["doi_source"].fillna("not recovered").astype(str)
    recovered = df_docs["doi_fix"].fillna("").astype(str).str.startswith("10.")
    print(f"-> Documents in batch           : {len(df_docs)}")
    print(f"-> DOIs recovered               : {int(recovered.sum())} / {len(df_docs)}"
          f" ({recovered.mean() * 100:.1f}%)")
    print(f"-> Still empty                  : {int((~recovered).sum())}")

    print("\n--- What recovered them ---")
    for value, count in sources[recovered].value_counts().items():
        print(f"     * {value}: {count}")

    print("\n--- Why the rest failed ---")
    for value, count in sources[~recovered].value_counts().items():
        print(f"     * {value}: {count}")
    print("=" * 60 + "\n")


def run_author_pipeline(api_params=None, source:str="gotriple", task:str="disambiguate", strategy:str="cluster"):
    """Two separate jobs over author profiles, chosen with `task`.

    task="empty_names"  Find profiles with no fullname at all and try to recover
                        one - from the documents they are attached to and from
                        the external APIs. The name query is ignored: this is
                        about blank profiles, whoever they are. Elastic only.

    task="disambiguate" Retrieve authors by name (api_params["q"]) and decide
                        which records are the same person, using `strategy`:
          "cluster"     set-wide, deterministic, transitive (union-find), master
                        chosen by rule. Recommended.
          "heuristic"   the original incremental registry: first-seen-wins and
                        order-dependent, so results vary between runs.

    source: "gotriple" reads through the public REST API,
            "elastic"  queries triple-profiles-prod directly. The index has no
                       `current_organization` field, so disambiguation there
                       relies on name plus topic/document/domain overlap only.
    """
    if api_params is None:
        api_params = {}

    print("\n--- STARTING AUTHOR NAME PIPELINE ---")

    # Extract configuration strictly from api_params, using defaults if keys are missing
    size_to_fetch = api_params.get("size", "1000")
    page_to_fetch = api_params.get("page", "1")
    name_query = api_params.get("q")

    if task in NAME_REPAIR_TASKS:
        if source != "elastic":
            raise ValueError(f"task='{task}' needs source='elastic' - the REST API "
                             "cannot select profiles by the state of their name")
        scope, label = NAME_REPAIR_TASKS[task]
        print(f"Task: recover {label} | source: {source} | requested: {size_to_fetch}")
        authors = fetch_elastic_authors(size=size_to_fetch, name_filter=scope)
    elif task == "top_author":
        if source != "elastic":
            raise ValueError("task='top_author' needs source='elastic' - counting profiles "
                             "per name requires an aggregation")
        top = fetch_top_author_names(size=5)
        if not top:
            print("No author names found.")
            return
        target = api_params.get("q") or top[0]["fullname"]
        print(f"Task: disambiguate the most duplicated name | source: {source}")
        print("Most duplicated names:")
        for entry in top:
            marker = "<-" if entry["fullname"] == target else "  "
            print(f"   {marker} {entry['fullname'][:44]:46} {entry['profiles']:>6} profiles")
        print(f"\nPulling up to {size_to_fetch} profiles named exactly {target!r}...")
        authors = fetch_elastic_authors(size=size_to_fetch, exact_name=target)
        authors = _attach_document_evidence(authors)
    elif task == "disambiguate":
        print(f"Task: disambiguate ({strategy}) | source: {source} | q: {name_query!r} | requested: {size_to_fetch}")
        if source == "elastic":
            authors = fetch_elastic_authors(size=size_to_fetch, query=name_query)
        elif source == "gotriple":
            authors = fetch_gotriple_authors(params=api_params)
        else:
            raise ValueError(f"Unknown source '{source}' - expected 'gotriple' or 'elastic'")
    else:
        raise ValueError(f"Unknown task '{task}' - expected one of "
                         f"{sorted(NAME_REPAIR_TASKS)}, 'top_author' or 'disambiguate'")

    if not authors:
        print("No authors found to process.")
        return

    if task in NAME_REPAIR_TASKS:
        processed_authors = _recover_names(authors)
    elif task == "top_author":
        processed_authors = _disambiguate_clustered(authors)
    elif strategy == "heuristic":
        processed_authors = _disambiguate_heuristic(authors)
    elif strategy == "cluster":
        processed_authors = _disambiguate_clustered(authors)
    else:
        raise ValueError(f"Unknown strategy '{strategy}' - expected 'cluster' or 'heuristic'")

    df_authors = pd.DataFrame(processed_authors)
    df_authors = df_authors.drop(columns=['_resolved_name'], errors='ignore')  # internal to clustering

    # Reorder columns to group our generated fixes and aliases neatly
    for col_pair in [('fullname', 'fullname_fix'), ('id', 'id_fix'), ('fullname_fix', 'is_aka'), ('is_aka', 'aka_of')]:
        if col_pair[0] in df_authors.columns and col_pair[1] in df_authors.columns:
            df_authors.insert(df_authors.columns.get_loc(col_pair[0]) + 1, col_pair[1], df_authors.pop(col_pair[1])) # type: ignore
            
    suffix = task if task in NAME_REPAIR_TASKS or task == 'top_author' else f'disambiguation_{strategy}'
    output_filename = os.path.join(OUTPUT_DIR, f'authors_{suffix}_{source}.xlsx')
    df_authors.to_excel(output_filename, index=False)
    print(f"-> Author data saved to '{output_filename}'")

    # ==========================================
    # SUMMARY REPORTING
    # ==========================================
    print("\n" + "="*60)
    print("FINAL DISAMBIGUATION & ENRICHMENT SUMMARY")
    print("="*60)

    # Standardize empty values for accurate counting
    df_authors['id_fix'] = df_authors['id_fix'].replace('', np.nan)
    df_authors['fullname_fix'] = df_authors['fullname_fix'].replace('', np.nan)

    # --- Metrics for Merging Candidates ---
    valid_orcids = df_authors.dropna(subset=['id_fix'])
    repeated_orcids = valid_orcids[valid_orcids.duplicated(subset=['id_fix'], keep=False)]
    
    # Create a "resolved_name" column to group fuzzy matches accurately
    # If is_aka is 'Yes', use the aka_of master name. Otherwise, use their own fullname_fix.
    if 'is_aka' in df_authors.columns and 'aka_of' in df_authors.columns:
        df_authors['resolved_name'] = np.where(df_authors['is_aka'] == 'Yes', df_authors['aka_of'], df_authors['fullname_fix'])
    else:
        df_authors['resolved_name'] = df_authors['fullname_fix']

    # Find duplicates based on the RESOLVED cluster, not the exact raw string
    valid_names = df_authors.dropna(subset=['resolved_name'])
    valid_names = valid_names[~valid_names['resolved_name'].isin(["No Name Available", "No Name Found"])]
    repeated_names = valid_names[valid_names.duplicated(subset=['resolved_name'], keep=False)]

    unique_repeated_orcids = repeated_orcids['id_fix'].nunique() if not repeated_orcids.empty else 0
    unique_repeated_names = repeated_names['resolved_name'].nunique() if not repeated_names.empty else 0

    print(f"-> Candidates sharing an ORCID : {len(repeated_orcids)} records (across {unique_repeated_orcids} unique ORCIDs)")
    
    if unique_repeated_orcids > 0:
        print("   Details:")
        for orcid, group in repeated_orcids.groupby('id_fix'):
            names_used = group['fullname'].dropna().unique()
            print(f"     * ORCID: {orcid} | Appears as: {', '.join(names_used)}")

    print(f"\n-> Candidates sharing a resolved Name : {len(repeated_names)} records (across {unique_repeated_names} unique clusters)")
    
    if unique_repeated_names > 0:
        print("   Details:")
        for name, group in repeated_names.groupby('resolved_name'):
            orcids = group['id_fix'].dropna().unique()
            orcid_str = f" (ORCID's : {', '.join(orcids)})" if len(orcids) > 0 else " (ORCID's : None found)"
            
            # Show the variations that were merged into this cluster
            variations = group['fullname_fix'].dropna().unique()
            variations_str = f" [Variations: {', '.join(variations)}]" if len(variations) > 1 else ""
            
            print(f"     * Master Name: {name} | Found in {len(group)} records{orcid_str}{variations_str}")
            
    print("\n--- Disambiguation Results ---")
    if 'is_aka' in df_authors.columns:
        total_merged = len(df_authors[df_authors['is_aka'] == 'Yes'])
        print(f"-> Total successfully merged   : {total_merged} records flagged as 'AKA'")

    print("-" * 60)

    # --- Metrics for Failed Recoveries ---
    missing_orcid_df = df_authors[df_authors['id_fix'].isna()]
    print(f"-> Failed to recover ORCID     : {len(missing_orcid_df)} records")
    
    missing_name_df = df_authors[df_authors['fullname_fix'].isna() | df_authors['fullname_fix'].isin(["No Name Available", "No Name Found"])]
    print(f"-> Failed to retrieve Name     : {len(missing_name_df)} records")
        
    print("="*60 + "\n")