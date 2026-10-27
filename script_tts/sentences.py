"""Keep source spans intact so positioning never relies on normalized speech text."""
from __future__ import annotations

import re
from dataclasses import dataclass

_TITLES = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "fig", "no", "e.g", "i.e"}
_CLOSERS = '\"\u201d\u2019\')]}\u00bb'


@dataclass(frozen=True)
class Sentence:
    start: int
    end: int
    text: str


def split_sentences(text: str, *, line_breaks: bool = True) -> list[Sentence]:
    result: list[Sentence] = []
    start, i, n = 0, 0, len(text)

    def emit(end: int) -> None:
        nonlocal start
        a, b = start, end
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a < b:
            result.append(Sentence(a, b, text[a:b]))
        start = end

    while i < n:
        c = text[i]
        # A paragraph or explicit line break is a useful boundary for plain text/PDFs.
        if c in "\r\n\u2029" and (line_breaks or c == "\u2029" or (i + 1 < n and text[i + 1] == "\n")):
            emit(i)
            start = i + 1
        elif c in ".!?\u3002\uff01\uff1f":
            end = i + 1
            while end < n and text[end] in ".!?\u3002\uff01\uff1f" + _CLOSERS:
                end += 1
            boundary = end == n or text[end].isspace() or c in "\u3002\uff01\uff1f"
            if c == ".":
                word = re.search(r"([\w.]+)$", text[start:i])
                token = word.group(1).lower() if word else ""
                if token in _TITLES or (len(token) == 1 and token.isalpha()):
                    boundary = False
                if i and i + 1 < n and text[i - 1].isdigit() and text[i + 1].isdigit():
                    boundary = False
            if boundary:
                emit(end)
                i = end - 1
        i += 1
    emit(n)
    return result


def speech_chunks(text: str, limit: int = 320) -> list[str]:
    """Bound inference length, retaining one document anchor for the whole sentence."""
    text = " ".join(text.replace("\x07", " ").split())
    parts = []
    while len(text) > limit:
        cut = max(text.rfind(mark, 0, limit + 1) for mark in ("; ", ", ", ": "))
        if cut >= limit // 3:
            cut += 1  # keep the punctuation with the words it follows
        if cut < limit // 3:
            cut = text.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts
