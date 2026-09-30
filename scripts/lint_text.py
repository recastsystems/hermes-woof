#!/usr/bin/env python3
"""Shipped-text checks for hermes-woof.

Everything in this repository may be published. This script fails (exit 1)
when a file carries:

  * an em dash;
  * wording the project keeps out of shipped text (listed below, spelled in
    pieces so this file passes its own check);
  * names of people, internal machine names, product names that do not belong
    in this plugin, or the engine's former name (kept as hashes below, so this
    file does not spell them);
  * private addresses or home-folder paths;
  * the renderer's optional expensive product family or the flag that turns it on.

Usage: lint_text.py [PATH ...]   (default: the repository root)
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".txt", ".toml", ".js", ".html", ".cfg", ".ini", ""}


def _j(*parts: str) -> str:
    return "".join(parts)


RULES = [
    ("em dash", re.compile(chr(0x2014))),
    ("banned word", re.compile(_j(r"(?i)\bhon", r"est(ly|y)?\b"))),
    ("banned word", re.compile(_j(r"(?i)\bload", r"[- ]bearing\b"))),
    ("banned word", re.compile(_j(r"(?i)\bra", r"ils\b"))),
    ("banned number", re.compile(_j(r"(?<![\w.])9", r"5(?![\w.])"))),
    ("banned number", re.compile(_j(r"(?i)\bninety[- ]fi", r"ve\b"))),
    ("private address", re.compile(_j(r"\b192\.16", r"8\.\d+\.\d+"))),
    ("home path", re.compile(_j(r"(?i)/home/[a-z]|[a-z]:[\\/]+users[\\/]"))),
    ("expensive product family", re.compile(_j(r"(?i)e", r"cape|--hea", r"vy\b|\bhea", r"vy (group|product|import|stack|family)"))),
]

# Words kept out of shipped text, as the first 16 hex digits of the SHA-256 of the lower-case
# word. A line is checked word by word and by adjacent pairs joined (so a first and last name,
# a name split by a hyphen, and a name with its number are caught). Words are runs of letters
# or runs of digits.
HASHED = {
    "person name": {"c850c75d30a9360f", "b2471d941bf88836", "779562b086bb57bf", "e4bb33e05498befc",
                    "693b286515bd1dd0", "347c61d48abf5288"},
    "internal machine": {"ca12f31b8cbf5f29", "15b18a7243257695", "d2b8f62a7e335bbd", "195842043a9ab55a",
                         "f1fc9a0dcd5f80b5"},
    "other product name": {"80d078c7dc84b074"},
    "former engine name": {"f8a0675540b6b1de"},
}
_WORD = re.compile(r"[a-z]+|[0-9]+")


def word_hash(word: str) -> str:
    return hashlib.sha256(word.lower().encode("utf-8")).hexdigest()[:16]


def hashed_hits(line: str) -> list[str]:
    """Labels of the kept-out words in LINE, in HASHED order."""
    words = _WORD.findall(line.lower())
    if not words:
        return []
    found = {word_hash(w) for w in set(words) | {a + b for a, b in zip(words, words[1:])}}
    return [label for label, hashes in HASHED.items() if found & hashes]


def files(paths):
    for p in paths:
        p = Path(p)
        if p.is_file():
            yield p
            continue
        for f in sorted(p.rglob("*")):
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            if f.is_file() and (f.suffix.lower() in TEXT_SUFFIXES):
                yield f


def check_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        rel = path.resolve().relative_to(ROOT)
    except ValueError:
        rel = path
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        labels = [label for label, rx in RULES if rx.search(line)] + hashed_hits(line)
        for label in labels:
            problems.append(f"{rel}:{n}: {label}: {line.strip()[:120]}")
    return problems


def main(argv):
    targets = argv or [str(ROOT)]
    problems = []
    for f in files(targets):
        problems += check_file(f)
    for p in problems[:200]:
        print(p)
    if problems:
        print(f"lint_text: {len(problems)} problem(s)")
        return 1
    print("lint_text: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
