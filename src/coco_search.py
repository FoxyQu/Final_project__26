"""Find feminine reference strategies in CoCo (Corpus of Correspondence).

Mirrors the GICR queries of src/gicr_collect.py on the local SQLite built by
src/coco_flatten.py:

  suffix      - any inflected form of a feminitive lemma from data/lexicon.csv;
  suffix_nomsg - the same lemma in nom.sg only (comparable with the agreement frames);
  agree_adj  - feminine adjective/pronoun (nom.sg) right before the base: «наша врач»;
  agree_verb  - base followed by a feminine past-tense verb: «врач сказала»;
  analytic    - «женщина» + base: «женщина врач», «женщина-врач»;
  base_total  - all forms of the masculine base.

Only aggregated counts go to the public repository (data/coco_counts*.csv).
Matched contexts are written to a local file for manual checking and are never
committed, because CoCo consists of private group chats.

Usage: python src/coco_search.py COCO_SQLITE LOCAL_HITS_CSV
"""
import csv
import logging
import os
import re
import sqlite3
import sys
from collections import Counter
from functools import lru_cache

import pymorphy3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEXICON = os.path.join(ROOT, "data", "lexicon.csv")
OUT_TOTAL = os.path.join(ROOT, "data", "coco_counts.csv")
OUT_BY_CHAT = os.path.join(ROOT, "data", "coco_counts_by_chat.csv")
OUT_SIZES = os.path.join(ROOT, "data", "coco_size.csv")

TOKEN = re.compile(r"[а-я]+(?:-[а-я]+)?")
# adjacency must not cross punctuation or line breaks: «м. Нагорная\nТренер: Дмитрий»
# is a template, not «нагорная тренер»
CLAUSE = re.compile(r"[\n\r.,!?;:()\[\]\"«»—–/|*•]+")
FEM_ENDINGS = "(?:а|ы|и|е|у|ой|ою|ей|ею|ам|ям|ами|ями|ах|ях|я|ю)?"
BASE_ENDINGS = "(?:а|у|ом|е|ы|ов|ам|ами|ах|и|ей|ем|ю|я|ям|ями|ях)?"

# public tables name chats only by topic, not by their internal export codes
CHAT_TOPIC = {
    "btennis": "sport_1", "football": "sport_2", "volleyball": "sport_3", "hokkey": "sport_4",
    "cs": "games_1", "dota": "games_2", "gta": "games_3",
    "dubki_res": "dormitory_1", "finac": "dormitory_2", "miphi": "dormitory_3", "msu": "dormitory_4",
    "blackpink": "fandom_1", "domsovy": "fandom_2", "hellboss": "fandom_3", "shabash": "fandom_4",
}

log = logging.getLogger("coco")
morph = pymorphy3.MorphAnalyzer()


def norm(text):
    return text.lower().replace("ё", "е")


def gen_plural(lemma):
    """Zero-ending genitive plural: журналистка → журналисток, авторша → авторш."""
    if lemma.endswith("ка"):
        stem = lemma[:-2]
        return stem + ("ек" if stem[-1] in "жшчщ" else "ок")
    if lemma.endswith("ня"):
        return lemma[:-1] + "ь"
    return lemma[:-1]


def fem_pattern(lemma):
    lemma = norm(lemma)
    stem = lemma[:-1]
    return re.compile(rf"^(?:{stem}{FEM_ENDINGS}|{gen_plural(lemma)})$")


@lru_cache(maxsize=200000)
def top_tag(word):
    return morph.parse(word)[0].tag


def is_fem_modifier(word):
    tag = top_tag(word)
    return tag.POS in {"ADJF", "PRTF"} and {"femn", "sing", "nomn"} <= tag.grammemes


def is_fem_past(word):
    tag = top_tag(word)
    return tag.POS == "VERB" and {"past", "femn"} <= tag.grammemes


def base_pattern(base):
    """All case forms of the masculine base; soft-sign stems drop ь (учитель → учителя)."""
    if base.endswith("ь"):
        return re.compile(rf"^{base[:-1]}(?:ь|я|ю|ем|е|и|ей|ям|ями|ях)$")
    return re.compile(rf"^{base}{BASE_ENDINGS}$")


def load_lexicon():
    """Index lexicon entries by the first 4 letters, so each token costs one dict lookup."""
    with open(LEXICON, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    index = {}
    for r in rows:
        base = norm(r["base"])
        index.setdefault(base[:4], []).append(("suffix", base, r["fem_lemma"], r["suffix"],
                                               fem_pattern(r["fem_lemma"])))
    bases = sorted({norm(r["base"]) for r in rows})
    for base in bases:
        index.setdefault(base[:4], []).append(("base_total", base, base, "masculine_base",
                                               base_pattern(base)))
    return index, set(bases)


def match_message(tokens, index, base_set):
    """Yield (base, query_type, item, label, token_index) for one message."""
    for i, tok in enumerate(tokens):
        for qtype, base, item, label, pat in index.get(tok[:4], ()):
            if pat.match(tok):
                yield base, qtype, item, label, i
                if qtype == "suffix" and tok == norm(item):
                    yield base, "suffix_nomsg", item, label, i
        if "-" in tok:
            left, right = tok.split("-", 1)
            if left == "женщина" and right in base_set:
                yield right, "analytic", f"женщина {right}", "analytic", i
            continue
        if tok not in base_set:
            continue
        if i > 0 and tokens[i - 1] == "женщина":
            yield tok, "analytic", f"женщина {tok}", "analytic", i
        elif i > 0 and is_fem_modifier(tokens[i - 1]):
            yield tok, "agree_adj", tok, "agreement", i
        if i + 1 < len(tokens) and is_fem_past(tokens[i + 1]):
            yield tok, "agree_verb", tok, "agreement", i


def main(db_path, local_hits_path):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    index, base_set = load_lexicon()
    prefilter = re.compile("|".join(sorted(set(index) | {"женщин"})))
    total, by_chat = Counter(), Counter()
    words, messages, chats = 0, 0, set()

    con = sqlite3.connect(db_path)
    with open(local_hits_path, "w", encoding="utf-8", newline="") as hits_file:
        hits = csv.writer(hits_file)
        hits.writerow(["chat", "ym", "base", "query_type", "item", "context"])
        for chat, ym, text in con.execute("SELECT chat, ym, text FROM messages"):
            low = norm(text)
            messages += 1
            chats.add(chat)
            if messages % 500000 == 0:
                log.info("%d messages scanned", messages)
            if not prefilter.search(low):
                words += len(TOKEN.findall(low))
                continue
            for clause in CLAUSE.split(low):
                tokens = TOKEN.findall(clause)
                words += len(tokens)
                for base, qtype, item, label, i in match_message(tokens, index, base_set):
                    total[(base, qtype, item, label)] += 1
                    by_chat[(CHAT_TOPIC.get(chat, "other"), base, qtype, item, label)] += 1
                    if qtype != "base_total":
                        hits.writerow([chat, ym, base, qtype, item,
                                       " ".join(tokens[max(0, i - 6): i + 7])])

    with open(OUT_TOTAL, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["base", "query_type", "item", "label", "hits"])
        for key, n in sorted(total.items()):
            w.writerow([*key, n])
    with open(OUT_BY_CHAT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["chat", "base", "query_type", "item", "label", "hits"])
        for key, n in sorted(by_chat.items()):
            w.writerow([*key, n])
    with open(OUT_SIZES, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["corpus", "chats", "messages", "words"])
        w.writerow(["CoCo", len(chats), messages, words])
    log.info("done: %d chats, %d messages, %d words", len(chats), messages, words)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
