"""Decide when two labels mean the same thing.

A Python port of the scoring the browser runs in the page. It exists twice on
purpose: the browser needs it in JavaScript to pick an element, and the
reconciliation pass needs it here to compare what a ticket claims against what a
screen offers — without opening anything. The two must agree, so any change to
one belongs in the other, and `tests/test_qa_textmatch.py` pins the shared
cases.

The rules, and why each is there:

    words only          «es» sits inside «pesroan»; comparing substrings made a
                        scrambled word match the language switch
    prefix ≥ 4          «persona» / «personas», without «casa» / «casarse»
    one edit ≥ 5 chars  a typo in a criterion should not send the click elsewhere
    transposition = 1   «impotrar» for «importar» is the commonest slip, and
                        plain Levenshtein counts it as two
    scored on what was asked, not on the label — an extra word on the button is
    normal, a missing one is not
"""

from __future__ import annotations

import unicodedata
from typing import Iterable, List, Optional, Tuple

# Same threshold the browser uses. Below it, a candidate is not the thing asked
# for and saying «not found» is the honest answer.
MIN_SCORE = 0.6

# Words shorter than this are articles and prepositions; keeping them would make
# almost any pair of labels look alike.
MIN_WORD_LENGTH = 3


def fold(text: str) -> str:
    """Lowercase, unaccented, punctuation-free."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    kept = [ch.lower() if ch.isalnum() else " " for ch in stripped]
    return " ".join("".join(kept).split())


def words(text: str) -> List[str]:
    return [word for word in fold(text).split(" ") if len(word) > MIN_WORD_LENGTH - 1]


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein: a transposition costs one, not two."""
    rows, cols = len(a), len(b)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows + 1):
        table[i][0] = i
    for j in range(cols + 1):
        table[0][j] = j
    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            table[i][j] = min(
                table[i - 1][j] + 1,
                table[i][j - 1] + 1,
                table[i - 1][j - 1] + cost,
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                table[i][j] = min(table[i][j], table[i - 2][j - 2] + 1)
    return table[rows][cols]


def same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and long.startswith(short):
        return True
    if len(short) >= 5 and len(long) - len(short) <= 1:
        return edit_distance(a, b) <= 1
    return False


def score(wanted: str, label: str) -> float:
    """How well `label` answers a request for `wanted`, from 0 to 1."""
    folded_label = fold(label)
    if not folded_label:
        return 0.0
    if folded_label == fold(wanted):
        return 1.0
    wanted_words = words(wanted)
    label_words = words(label)
    if not wanted_words or not label_words:
        return 0.0
    shared = sum(1 for word in label_words if any(same_word(word, x) for x in wanted_words))
    if not shared:
        return 0.0
    asked = shared / len(wanted_words)
    coverage = shared / max(len(label_words), len(wanted_words))
    return 0.7 * asked + 0.3 * coverage


def best_match(wanted: str, candidates: Iterable[str]) -> Optional[Tuple[str, float]]:
    """The closest candidate and its score, or None when nothing is close."""
    best: Optional[Tuple[str, float]] = None
    for candidate in candidates:
        value = score(wanted, candidate)
        if value < MIN_SCORE:
            continue
        if best is None or value > best[1]:
            best = (candidate, value)
    return best
