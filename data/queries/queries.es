// GoTriple production Elasticsearch — scratchpad
//
// Host is NOT read from this file. Set it once per workspace:
//   Cmd+Shift+P -> "Elastic: Set Host" -> https://USER:PASSWORD@lumen-es-route-lumen-gotriple-production.apps.bst2.paas.psnc.pl
//
GET /_search
{
  "size": 0,
  "aggs": {
    "indices": { "terms": { "field": "_index", "size": 30 } }
  }
}

// --- One whole document, to see the available fields ---
GET /triple-documents-prod/_search
{
  "size": 1
}

// --- How the 39.9M documents break down by license ---
GET /triple-documents-prod/_search
{
  "size": 0,
  "aggs": {
    "license": { "terms": { "field": "license", "size": 20 } }
  }
}

// --- The population the license pipeline targets: license = "other" ---
GET /triple-documents-prod/_search
{
  "size": 5,
  "query": { "term": { "license": "other" } },
  "_source": ["id", "headline", "license", "publisher", "main_entity_of_page", "doi"]
}

// --- Which repositories feed the "other" bucket? ---
// Note: hits.total caps at 10000 unless you ask for the real number.
GET /triple-documents-prod/_search
{
  "size": 0,
  "track_total_hits": true,
  "query": { "term": { "license": "other" } },
  "aggs": {
    "provider": { "terms": { "field": "provider", "size": 30 } },
    "has_publisher": { "filter": { "exists": { "field": "publisher" } } }
  }
}

// --- Publisher names, for tuning KNOWN_OA_PUBLISHERS in functions_license.py ---
// `publisher` is a text field with no usable keyword subfield on this index, so
// terms aggregations return nothing. Sample the raw values instead.
GET /triple-documents-prod/_search
{
  "size": 20,
  "query": {
    "bool": {
      "filter": [
        { "term": { "license": "other" } },
        { "exists": { "field": "publisher" } }
      ]
    }
  },
  "_source": ["publisher", "provider", "main_entity_of_page"]
}

// --- Author profiles (23.6M docs) ---
GET /triple-profiles-prod/_search
{
  "size": 1
}

// ==========================================================================
// LICENSE COVERAGE
// ==========================================================================
// Careful: the `license` field is present on almost every document (39,884,230
// of 39,884,397), but 36,796,242 of those carry the placeholder value
// "undefined". Counting with `exists` alone would report ~100% coverage.
// "Has a license" therefore means: field exists AND value != "undefined".

// --- How many documents have no license, and no original_license? ---
GET /triple-documents-prod/_search
{
  "size": 0,
  "track_total_hits": true,
  "aggs": {
    "license_exists":       { "filter": { "exists": { "field": "license" } } },
    "license_undefined":    { "filter": { "term": { "license": "undefined" } } },
    "license_real": {
      "filter": {
        "bool": {
          "must":     [ { "exists": { "field": "license" } } ],
          "must_not": [ { "term": { "license": "undefined" } } ]
        }
      }
    },
    "original_license_exists":  { "filter": { "exists": { "field": "original_license" } } },
    "original_license_missing": {
      "filter": { "bool": { "must_not": [ { "exists": { "field": "original_license" } } ] } }
    }
  }
}

// --- Coverage of both fields, broken down by provider ---
// One aggregation answers both questions per repository: how many of its
// documents carry a real license, and how many carry an original_license.
// Divide each sub-count by its bucket's doc_count for the coverage rate, or by
// the grand total for that provider's share of all licensed documents.
GET /triple-documents-prod/_search
{
  "size": 0,
  "track_total_hits": true,
  "aggs": {
    "by_provider": {
      "terms": { "field": "provider", "size": 50, "order": { "_count": "desc" } },
      "aggs": {
        "with_license": {
          "filter": {
            "bool": {
              "must":     [ { "exists": { "field": "license" } } ],
              "must_not": [ { "term": { "license": "undefined" } } ]
            }
          }
        },
        "with_original_license": {
          "filter": { "exists": { "field": "original_license" } }
        }
      }
    }
  }
}

// --- Which repositories have NO original_license at all? ---
// A bucket_selector keeps only the providers whose original_license count is 0.
GET /triple-documents-prod/_search
{
  "size": 0,
  "aggs": {
    "by_provider": {
      "terms": { "field": "provider", "size": 50 },
      "aggs": {
        "with_original_license": {
          "filter": { "exists": { "field": "original_license" } }
        },
        "none_at_all": {
          "bucket_selector": {
            "buckets_path": { "n": "with_original_license._count" },
            "script": "params.n == 0"
          }
        }
      }
    }
  }
}
