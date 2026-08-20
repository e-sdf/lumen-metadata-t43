# lumen_metadata_t43

Metadata cleaning for [GoTriple](https://www.gotriple.eu). Two problems, both read
straight from the production Elasticsearch cluster (or, for smaller runs, the public
GoTriple REST API):

- **Authors** — repair broken names (an ORCID sitting inside the name field, empty
  names, URLs or bare digits) and decide which profiles in `triple-profiles-prod`
  are the same person.
- **Licenses** — recover a usable license for the 38.2M documents in
  `triple-documents-prod` whose license is `undefined`, `other`, or absent.

Every run writes an Excel report to [data/output/](data/output/).

## Project layout

```
main.ipynb              the notebook: strategies, the ES queries actually run, live timings
run_pipeline.py         interactive CLI - same pipelines, prompt-driven
src/
  pipelines.py          run_license_pipeline() / run_author_pipeline() - fetch, fan out, write Excel
  functions_license.py  license recovery: landing-page scraping, Crossref, OpenAlex, Handle, OAI-PMH
  functions_name.py     name repair (ORCID lookups, recovery from documents) + cluster_authors()
  es_helpers.py         Elasticsearch gateway: .env loading, paging, retries, QUERY_LOG
  rate_limit.py         per-host throttle shared by every worker thread
tests/
  test_clustering.py    offline fixtures for cluster_authors - no network
data/
  queries/queries.es    scratchpad of raw ES queries (for the VS Code Elastic extension)
  output/               generated reports: license_fix_*.xlsx, authors_*_*.xlsx, ...
  error_examples/       sample records showing the data problems being fixed
requirements.txt        dependencies
.env                    ES credentials - git-ignored, never commit
```

## Setup

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
```

Then create `.env` in the project root — [src/es_helpers.py](src/es_helpers.py) reads it
and it is git-ignored:

```
ES_HOST=https://lumen-es-route-lumen-gotriple-production.apps.bst2.paas.psnc.pl
ES_USER=...
ES_PASS=...
```

Only the Elasticsearch source needs credentials; the GoTriple API path works without them.

## Running the notebook

[main.ipynb](main.ipynb) is the readable version of the project: for each strategy it
shows the schema, the Elasticsearch query that was really executed (captured from
`es_helpers.QUERY_LOG`, never retyped), and a timed live run.

**In VS Code** — open [main.ipynb](main.ipynb), pick the `venv` kernel
(`venv/bin/python`, Python 3.14), and *Run All*.

**Headless**, from the project root:

```bash
venv/bin/pip install nbclient nbformat            # once, only for this path
venv/bin/jupyter execute main.ipynb --inplace
```

Two things to know before you run it:

- **Run it from the project root.** Cells read the Excel reports back with relative
  paths like `data/output/license_fix_elastic.xlsx`.
- **The three constants in the second cell drive everything else:**

  ```python
  AUTHOR_NAME     = "Wang, Y."   # the name disambiguated in §1.4
  AUTHOR_SAMPLE   = 100          # author profiles pulled per subsection (§1.1-1.4)
  DOCUMENT_SAMPLE = 100          # documents pulled for license recovery (§2.3)
  ```

  At 100/100 the whole notebook finishes in minutes. Cost is roughly linear in the
  sample size — the exception is phase 2 of clustering, which compares every pair and
  so grows with the square of the record count (see §3).

The cells hit the live cluster and the external APIs (ORCID, Crossref, OpenAlex,
publisher landing pages), so a full pass makes a few hundred throttled HTTP requests
and overwrites the reports in [data/output/](data/output/).

## Running the pipelines from the CLI

```bash
venv/bin/python run_pipeline.py
```

It asks which pipeline (licenses, authors, or both), which source (GoTriple API or
Elasticsearch), which author task, and how many records. Entering nothing for the
count means *all of them* — for anything above 5,000 records it first counts the
population and asks for confirmation, because the run is roughly a record per second
and cannot be resumed.

To skip the prompts, call the pipelines directly:

```python
from src.pipelines import run_license_pipeline, run_author_pipeline

run_license_pipeline(num_docs="500", source="elastic", max_workers=10)
run_author_pipeline(api_params={"size": "200", "q": "Wang, Y."},
                    source="elastic", task="disambiguate", strategy="cluster")
```

Reports land in [data/output/](data/output/) as `license_fix_<source>.xlsx` and
`authors_<task>_<source>.xlsx`.

## Tests

```bash
venv/bin/python -m tests.test_clustering
```

Fixtures for `cluster_authors` covering the merge rules it has to get right, plus a
determinism check (the same fixture shuffled five ways must produce identical
groups). Pure functions, no network — it runs in about a second.
