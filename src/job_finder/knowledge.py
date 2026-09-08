from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .config import ProfileConfig
from .text import tokens


HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Chunk:
    source: str
    title: str
    text: str


class KnowledgeBase:
    def __init__(self, config: ProfileConfig) -> None:
        self.config = config

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError(f"Не найден файл базы знаний: {path}") from exc

    @classmethod
    def _chunks(cls, path: Path) -> list[Chunk]:
        content = cls._read(path)
        chunks: list[Chunk] = []
        title = path.stem
        buffer: list[str] = []
        parent = ""
        for line in content.splitlines():
            heading = HEADING_RE.match(line)
            if heading:
                if buffer:
                    text = "\n".join(buffer).strip()
                    if text:
                        chunks.append(Chunk(path.name, title, text))
                level, heading_text = len(heading.group(1)), heading.group(2).strip()
                if level <= 2:
                    parent = heading_text
                    title = heading_text
                else:
                    title = f"{parent} / {heading_text}" if parent else heading_text
                buffer = [line]
            else:
                buffer.append(line)
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                chunks.append(Chunk(path.name, title, text))
        return chunks

    def _all_knowledge_chunks(self) -> list[Chunk]:
        chunks: list[Chunk] = []
        for path in self.config.knowledge_paths:
            chunks.extend(self._chunks(path))
        return chunks

    def retrieve(self, query: str, *, limit: int = 8, max_chars: int = 32000) -> list[Chunk]:
        query_tokens = tokens(query)
        scored: list[tuple[float, Chunk]] = []
        synonyms = {
            "ai": {"ai", "ии", "llm", "gpt", "gigachat", "агент", "агенты", "genai"},
            "product": {"product", "продукт", "продуктовый", "po", "owner"},
            "project": {"project", "проект", "проекты", "pmp", "delivery"},
            "fintech": {"fintech", "банк", "банка", "лизинг", "кредит", "финансовый"},
            "consulting": {"консалтинг", "консультант", "трансформация", "стратегия", "roadmap"},
        }
        expanded = set(query_tokens)
        for family in synonyms.values():
            if query_tokens & family:
                expanded |= family

        for chunk in self._all_knowledge_chunks():
            chunk_tokens = tokens(f"{chunk.title}\n{chunk.text}")
            overlap = expanded & chunk_tokens
            score = sum(2.0 if t in tokens(chunk.title) else 1.0 for t in overlap)
            title_lower = chunk.title.lower()
            if "общие данные" in title_lower:
                score += 2.5
            if "границы" in title_lower:
                score += 1.5
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].source, item[1].title))

        selected: list[Chunk] = []
        used = 0
        for _, chunk in scored:
            if len(selected) >= limit:
                break
            size = len(chunk.title) + len(chunk.text)
            if selected and used + size > max_chars:
                continue
            selected.append(chunk)
            used += size
        return selected

    def cover_letter_rules(self, max_chars: int = 22000) -> str:
        text = self._read(self.config.cover_letter_rules_path)
        if len(text) > max_chars:
            return text[:max_chars].rstrip() + "\n[памятка сокращена по лимиту]"
        return text

    def health(self) -> dict[str, object]:
        files = [*self.config.knowledge_paths, self.config.cover_letter_rules_path]
        return {
            "files": [
                {"name": path.name, "exists": path.is_file(), "bytes": path.stat().st_size if path.is_file() else 0}
                for path in files
            ]
        }


def render_chunks(chunks: list[Chunk]) -> str:
    return "\n\n".join(f"### {chunk.title}\n{chunk.text}" for chunk in chunks)
