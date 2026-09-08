from __future__ import annotations

import html
import re


TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t]+")
BLANK_RE = re.compile(r"\n{3,}")
# Дефис и слеш разделяют смысловые слова: «LLM-платформа» → «llm», «платформа».
WORD_RE = re.compile(r"[a-zа-яё0-9][a-zа-яё0-9+.#]*", re.IGNORECASE)


STOPWORDS = {
    "для", "или", "как", "что", "это", "при", "под", "над", "без", "все", "его", "она",
    "они", "мы", "вы", "где", "когда", "который", "также", "будет", "есть", "работа",
    "опыт", "требования", "обязанности", "команда", "компании", "компания", "нужно", "может",
    "from", "with", "and", "the", "this", "that", "are", "you", "your", "our", "job",
}


def plain_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</\s*(p|li|div|h[1-6])\s*>", "\n", value, flags=re.IGNORECASE)
    value = TAG_RE.sub("", value)
    value = html.unescape(value).replace("\r", "")
    value = "\n".join(SPACE_RE.sub(" ", line).strip() for line in value.splitlines())
    return BLANK_RE.sub("\n\n", value).strip()


def tokens(value: str) -> set[str]:
    return {word.lower() for word in WORD_RE.findall(plain_text(value)) if len(word) > 2 and word.lower() not in STOPWORDS}


def compact(value: str, limit: int = 1500) -> str:
    value = plain_text(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"
