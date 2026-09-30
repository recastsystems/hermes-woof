"""woof_docs: BM25 search over the knowledge pack (standard library only)."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

_INDEX = Path(__file__).parent / "knowledge" / "index.json"
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.+-]*")
_STOP = frozenset("a an and are as at be by for from has have how i in is it its of on or that the this to "
                  "was what when where which with you your can do does not no".split())


def tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(text.lower()):
        t = t.strip(".-+")
        if t and t not in _STOP and len(t) > 1:
            out.append(t)
            if "_" in t or "-" in t:
                out.extend(p for p in re.split(r"[_-]", t) if len(p) > 1 and p not in _STOP)
    return out


@lru_cache(maxsize=1)
def _load():
    try:
        doc = json.loads(_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], {}, 0.0
    chunks = doc.get("chunks", [])
    tfs = []
    df: Counter = Counter()
    total = 0
    for c in chunks:
        tf = Counter(tokens(c["title"] + " " + c["title"] + " " + c["text"]))
        tfs.append(tf)
        df.update(tf.keys())
        total += sum(tf.values())
    avg = total / max(len(chunks), 1)
    return list(zip(chunks, tfs)), df, avg


def search(query: str, k: int = 4) -> list[dict]:
    rows, df, avg = _load()
    if not rows:
        return []
    q = tokens(query)
    n = len(rows)
    k1, b = 1.4, 0.75
    scored = []
    for chunk, tf in rows:
        length = sum(tf.values()) or 1
        s = 0.0
        for term in q:
            f = tf.get(term)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / avg))
        if s > 0:
            scored.append((s, chunk))
    scored.sort(key=lambda x: -x[0])
    return [{"doc": c["doc"], "section": c["title"], "score": round(s, 2), "text": c["text"]}
            for s, c in scored[:k]]


def manifest() -> dict:
    try:
        return json.loads((Path(__file__).parent / "knowledge" / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
