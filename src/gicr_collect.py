"""Collect frequencies of feminine reference strategies from GICR (VK) in the RNC.

GICR 2.0 is part of the Russian National Corpus (ruscorpora.ru); the
concordance endpoint of the public API answers without a token and returns
`queryStats` (number of hits and texts). For every base in data/lexicon.csv
the script asks for:

  suffix      - every feminitive lemma (all inflected forms), e.g. авторка;
  suffix_nomsg - the same lemma in nom.sg only (comparable with the agreement frames);
  agree_adj  - feminine adjective/pronoun + base in nom.sg: «молодая врач»;
  agree_verb  - base in nom.sg + feminine past-tense verb: «врач сообщила»;
  analytic    - «женщина» + base, both nom.sg: «женщина врач / женщина-врач»;
  base_total  - all forms of the masculine base (for normalisation).

Results are appended to data/gicr_counts.csv, so an interrupted run resumes.

Usage: python src/gicr_collect.py [--workers 3]
"""
import argparse
import csv
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

API = "https://ruscorpora.ru/api/v1/lex-gramm/concordance"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEXICON = os.path.join(ROOT, "data", "lexicon.csv")
OUT = os.path.join(ROOT, "data", "gicr_counts.csv")
FIELDS = ["base", "query_type", "item", "label", "hits", "texts"]

log = logging.getLogger("gicr")


def lex_value(word):
    """Search both spellings for words with ё (режиссёр / режиссер)."""
    return f"{word}|{word.replace('ё', 'е')}" if "ё" in word else word


def word(lex=None, gramm=None, dist=None):
    cond = []
    if lex:
        cond.append({"fieldName": "lex", "text": {"v": lex_value(lex)}})
    if gramm:
        cond.append({"fieldName": "gramm", "text": {"v": gramm}})
    if dist:
        cond.append({"fieldName": "dist", "intRange": {"begin": dist[0], "end": dist[1]}})
    return {"conditionValues": cond}


def build_queries(lexicon):
    queries = []
    for row in lexicon:
        queries.append((row["base"], "suffix", row["fem_lemma"], row["suffix"],
                        [word(lex=row["fem_lemma"])]))
        queries.append((row["base"], "suffix_nomsg", row["fem_lemma"], row["suffix"],
                        [word(lex=row["fem_lemma"], gramm="nom,sg")]))
    for base in sorted({row["base"] for row in lexicon}):
        queries += [
            (base, "agree_adj", base, "agreement",
             [word(gramm="(A|APRO),f,nom,sg"), word(lex=base, gramm="nom,sg", dist=(1, 1))]),
            (base, "agree_verb", base, "agreement",
             [word(lex=base, gramm="nom,sg"), word(gramm="V,praet,f", dist=(1, 1))]),
            (base, "analytic", f"женщина {base}", "analytic",
             [word(lex="женщина", gramm="nom,sg"), word(lex=base, gramm="nom,sg", dist=(1, 1))]),
            (base, "base_total", base, "masculine_base", [word(lex=base)]),
        ]
    return queries


def run_query(words, retries=6):
    body = {
        "corpus": {"type": "GICR"},
        "lexGramm": {"sectionValues": [{"subsectionValues": words}]},
        "params": {"pageParams": {"page": 0, "docsPerPage": 1, "snippetsPerDoc": 1}},
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(API, json=body, timeout=300)
            if resp.status_code == 429:
                # the API rate-limits bursts; wait as asked or back off
                wait = int(resp.headers.get("Retry-After", 20 * attempt))
                log.info("rate-limited, waiting %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            stats = resp.json().get("queryStats") or {}
            return stats.get("wordUsageCount", 0), stats.get("textCount", 0)
        except (requests.RequestException, ValueError):
            log.warning("query failed (attempt %d/%d)", attempt, retries, exc_info=True)
            time.sleep(10 * attempt)
    return None, None


def main(workers):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with open(LEXICON, encoding="utf-8") as f:
        lexicon = list(csv.DictReader(f))
    done = set()
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            done = {(r["base"], r["query_type"], r["item"]) for r in csv.DictReader(f)}
    todo = [q for q in build_queries(lexicon) if (q[0], q[1], q[2]) not in done]
    log.info("%d queries to run (%d already done)", len(todo), len(done))

    new_file = not os.path.exists(OUT)
    with open(OUT, "a", encoding="utf-8", newline="") as f, ThreadPoolExecutor(workers) as pool:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        futures = {pool.submit(run_query, q[4]): q for q in todo}
        for fut in as_completed(futures):
            base, qtype, item, label, _ = futures[fut]
            hits, texts = fut.result()
            if hits is None:
                log.error("giving up on %s / %s / %s", base, qtype, item)
                continue
            writer.writerow({"base": base, "query_type": qtype, "item": item,
                             "label": label, "hits": hits, "texts": texts})
            f.flush()
            log.info("%-14s %-10s %-22s %8d", base, qtype, item, hits)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=3)
    main(parser.parse_args().workers)
