"""Estimate precision of the two-word GICR queries (agreement and analytic).

The RNC distance condition counts words and ignores punctuation, so
«...м. Нагорная. Тренер: Дмитрий» can match «feminine adjective + тренер».
In CoCo such matches are excluded by splitting messages into clauses
(src/coco_search.py). For GICR we fetch a sample of concordance lines for
every two-word query and record the share of lines where the two matched
words are NOT separated by punctuation. The analysis multiplies raw GICR
counts by this share.

Output: data/gicr_precision.csv
Usage: python src/gicr_precision.py [--sample 40]
"""
import argparse
import csv
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import time

import requests

from gicr_collect import API, LEXICON, OUT as COUNTS, ROOT, build_queries

OUT = os.path.join(ROOT, "data", "gicr_precision.csv")
# Sampling a frequent two-word query takes up to two minutes, so only the queries that
# carry most of the hits are sampled (about 87% of agreement hits, 95% of analytic hits);
# the others get the mean precision of their query type in the analysis.
MIN_HITS = {"agree_adj": 500, "agree_verb": 500, "analytic": 100}
PAIR_TYPES = {"agree_adj", "agree_verb", "analytic"}
PUNCT = re.compile(r"[^\s\-]")

log = logging.getLogger("gicr-precision")


def snippet_is_clean(snippet):
    """True if the matched words are adjacent with only spaces/hyphens between them."""
    words = [w for seq in snippet["sequences"] for w in seq["words"]]
    hit_positions = [i for i, w in enumerate(words) if w.get("displayParams", {}).get("hit")]
    if len(hit_positions) < 2:
        return False
    between = words[hit_positions[0] + 1: hit_positions[-1]]
    return not any(w["type"] == "PLAIN" and PUNCT.search(w["text"]) for w in between)


def sample_query(words, sample):
    body = {
        "corpus": {"type": "GICR"},
        "lexGramm": {"sectionValues": [{"subsectionValues": words}]},
        "params": {"pageParams": {"page": 0, "docsPerPage": sample, "snippetsPerDoc": 1}},
    }
    for attempt in range(1, 6):
        resp = requests.post(API, json=body, timeout=600)
        if resp.status_code != 429:
            break
        time.sleep(int(resp.headers.get("Retry-After", 20 * attempt)))
    resp.raise_for_status()
    snippets = [sg["snippets"][0]
                for group in resp.json().get("groups", [])
                for doc in group["docs"]
                for sg in doc["snippetGroups"] if sg["snippets"]]
    return len(snippets), sum(snippet_is_clean(s) for s in snippets)


def main(sample, workers):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with open(LEXICON, encoding="utf-8") as f:
        lexicon = list(csv.DictReader(f))
    with open(COUNTS, encoding="utf-8") as f:
        hits = {(r["base"], r["query_type"]): int(r["hits"]) for r in csv.DictReader(f)}
    queries = [q for q in build_queries(lexicon)
               if q[1] in PAIR_TYPES and hits.get((q[0], q[1]), 0) >= MIN_HITS[q[1]]]
    log.info("sampling %d two-word queries", len(queries))
    rows = []
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(sample_query, q[4], sample): q for q in queries}
        for fut in as_completed(futures):
            base, qtype = futures[fut][0], futures[fut][1]
            try:
                n, clean = fut.result()
            except (requests.RequestException, ValueError, KeyError):
                log.warning("sampling failed for %s / %s", base, qtype, exc_info=True)
                continue
            rows.append({"base": base, "query_type": qtype, "sampled": n, "clean": clean})
            log.info("%-14s %-10s %3d/%3d clean", base, qtype, clean, n)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["base", "query_type", "sampled", "clean"])
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["base"], r["query_type"])))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=40)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    main(args.sample, args.workers)
