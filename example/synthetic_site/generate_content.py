"""Generate the synthetic site's invented content: seed DB, fixtures, search shard.

This is the ONE place in the repository where generated text is legitimate. The
site is invented, it ships publicly, and there is no source page for any of it to
be traced back to. In the real pipeline this content comes verbatim from the
extract, and generating it would be QA-VERB-01 (see CLAUDE.md).

Deterministic: a fixed Faker seed, so the bundle hashes are reproducible and a
diff in CI means something changed on purpose.

    uv run python example/synthetic_site/generate_content.py
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path

from faker import Faker

HERE = Path(__file__).parent
SEED = 20260914
THREADS = 24
SITE_NAME = "The Longhouse Boards"

# BM25 parameters, matching what the inside search engine will use.
K1, B = 1.5, 0.75
TOKEN = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def build_seed_db(path: Path, fake: Faker) -> list[dict]:
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE threads (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
            author TEXT NOT NULL, created_at TIMESTAMP NOT NULL);
        CREATE TABLE replies (
            id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id),
            body TEXT NOT NULL, author TEXT, created_at TIMESTAMP NOT NULL);
        CREATE INDEX idx_threads_created ON threads(created_at);
        CREATE INDEX idx_replies_thread ON replies(thread_id);
        """
    )

    threads = []
    for i in range(1, THREADS + 1):
        title = fake.sentence(nb_words=6).rstrip(".")
        body = "\n\n".join(fake.paragraph(nb_sentences=4) for _ in range(2))
        created = fake.date_time_between("-2y", "-1d").isoformat(sep=" ", timespec="seconds")
        threads.append({"id": i, "title": title, "body": body})
        db.execute(
            "INSERT INTO threads VALUES (?,?,?,?,?)",
            (i, title, body, fake.user_name(), created),
        )

    reply_id = 0
    for thread in threads:
        for _ in range(fake.random_int(0, 5)):
            reply_id += 1
            db.execute(
                "INSERT INTO replies VALUES (?,?,?,?,?)",
                (
                    reply_id,
                    thread["id"],
                    fake.paragraph(nb_sentences=2),
                    fake.user_name(),
                    fake.date_time_between("-1y", "-1d").isoformat(sep=" ", timespec="seconds"),
                ),
            )

    db.commit()
    db.close()
    return threads


def build_shard(path: Path, threads: list[dict]) -> None:
    """A minimal BM25 shard. Plain JSON: the format is the inside engine's business,
    and for the demo a readable one beats a fast one."""
    docs = {
        t["id"]: tokenize(f"{t['title']} {t['body']}") for t in threads
    }
    lengths = {doc_id: len(tokens) for doc_id, tokens in docs.items()}
    avgdl = sum(lengths.values()) / len(lengths)

    postings: dict[str, dict[str, float]] = {}
    df = Counter(term for tokens in docs.values() for term in set(tokens))
    n = len(docs)
    for doc_id, tokens in docs.items():
        counts = Counter(tokens)
        for term, tf in counts.items():
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * lengths[doc_id] / avgdl))
            postings.setdefault(term, {})[str(doc_id)] = round(idf * norm, 6)

    path.write_text(
        json.dumps(
            {
                "format": "bm25-shard-v1",
                "table": "threads",
                "fields": ["title", "body"],
                "doc_count": n,
                "avgdl": round(avgdl, 4),
                "titles": {str(t["id"]): t["title"] for t in threads},
                "postings": postings,
            },
            indent=1,
        )
    )


def build_fixtures(path: Path, threads: list[dict], fake: Faker) -> None:
    """Test fixtures: everything a test types in or compares against (§7).

    The search query fixture is a substring of a real seed title, per site-qa Q7.
    """
    target = threads[0]
    query = " ".join(target["title"].split()[:3])
    path.write_text(
        json.dumps(
            {
                "fx_thread_1": {"thread_id": target["id"]},
                "fx_q1": query,
                "fx_q1_doc": target["title"],
                "fx_reply_1": {"body": fake.paragraph(nb_sentences=2), "author": fake.user_name()},
                "fx_home_png": {
                    "note_to_implementer": "placeholder; real suites carry a PNG from the scrape",
                    "kind": "render_reference",
                },
            },
            indent=1,
        )
    )


def main() -> None:
    fake = Faker()
    Faker.seed(SEED)
    fake.random.seed(SEED)

    threads = build_seed_db(HERE / "content" / "seed.sqlite", fake)
    build_shard(HERE / "index" / "main.shard", threads)
    build_fixtures(HERE / "content" / "fixtures.json", threads, fake)

    print(f"site: {SITE_NAME}")
    print(f"  threads:   {len(threads)}")
    print(f"  seed.sqlite  {(HERE / 'content' / 'seed.sqlite').stat().st_size:>8,} B")
    print(f"  main.shard   {(HERE / 'index' / 'main.shard').stat().st_size:>8,} B")
    print(f"  fixtures     {(HERE / 'content' / 'fixtures.json').stat().st_size:>8,} B")


if __name__ == "__main__":
    main()
