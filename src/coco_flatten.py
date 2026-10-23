"""Flatten CoCo Telegram exports into one local SQLite table.

The raw exports are private group chats, so this script keeps only what the
analysis needs: chat code, message id, year-month, a salted hash of the author
id (to count distinct authors) and the message text. The SQLite file is
local-only and never committed (see .gitignore).

Usage: python src/coco_flatten.py RAW_DIR OUT_SQLITE
"""
import hashlib
import os
import sqlite3
import sys

import ijson

SALT = os.environ.get("COCO_SALT", "coco-local-salt")


def flatten_text(text):
    if isinstance(text, str):
        return text
    parts = []
    for item in text or []:
        parts.append(item if isinstance(item, str) else item.get("text", ""))
    return "".join(parts)


def author_hash(from_id):
    if not from_id:
        return None
    return hashlib.sha1((SALT + str(from_id)).encode()).hexdigest()[:12]


def main(raw_dir, out_path):
    con = sqlite3.connect(out_path)
    con.execute("DROP TABLE IF EXISTS messages")
    con.execute(
        "CREATE TABLE messages (chat TEXT, msg_id INTEGER, ym TEXT, author TEXT, text TEXT)"
    )
    for name in sorted(os.listdir(raw_dir)):
        if not name.endswith(".json"):
            continue
        chat = name[:-5]
        rows = []
        with open(os.path.join(raw_dir, name), "rb") as f:
            for msg in ijson.items(f, "messages.item"):
                if msg.get("type") != "message":
                    continue
                text = flatten_text(msg.get("text"))
                if not text.strip():
                    continue
                rows.append((chat, int(msg["id"]), str(msg.get("date", ""))[:7],
                             author_hash(msg.get("from_id")), text))
                if len(rows) >= 50000:
                    con.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", rows)
                    rows = []
        con.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", rows)
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM messages WHERE chat=?", (chat,)).fetchone()[0]
        print(f"{chat}: {n} messages", flush=True)
    con.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
