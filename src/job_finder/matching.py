from __future__ import annotations

from typing import Any

from .text import plain_text


POSITIVE_FAMILIES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("AI/GenAI", 18, ("llm", "genai", "gpt", "gigachat", "искусственн", "машинн", "ai ", "ai-", "ии-", "rag", "агентн")),
    ("управление проектами", 15, ("руководитель проект", "руководитель ai", "руководитель ии", "project manager", "program manager", "pmp", "delivery manager", "delivery lead")),
    ("продукт", 12, ("product owner", "product manager", "продуктов", "менеджер продукта", "product lead")),
    ("трансформация/консалтинг", 10, ("трансформац", "консалтинг", "консультант", "стратег", "roadmap")),
    ("FinTech/B2B", 8, ("fintech", "банк", "лизинг", "кредит", "b2b", "enterprise")),
    ("платформы и интеграции", 7, ("платформ", "интеграц", "микросервис", "rest", "kafka", "bpm")),
    ("управление командой", 6, ("управление команд", "руководство команд", "team lead", "кросс-функцион")),
    ("B2G/GovTech", 5, ("b2g", "govtech", "государствен", "фстэк", "нко")),
)

GAP_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hands-on ML/Data Science", ("data scientist", "ml engineer", "обучение моделей", "pytorch", "tensorflow")),
    ("коробочные ERP/CRM", ("sap", "salesforce", "bitrix24", "1с:crm", "внедрение erp")),
    ("ITIL/ITSM", ("itil", "itsm", "service manager")),
    ("телеком BSS", (" bss", "биллинг оператора", "телеком")),
    ("массовая b2c-аналитика", ("когортный анализ", "retention", "монетизац", "arpu")),
)


def vacancy_text(vacancy: dict[str, Any]) -> str:
    snippet = vacancy.get("snippet") or {}
    parts = [
        str(vacancy.get("name") or ""),
        plain_text(vacancy.get("description")),
        plain_text(snippet.get("requirement")),
        plain_text(snippet.get("responsibility")),
        " ".join(str(skill.get("name", "")) for skill in vacancy.get("key_skills") or []),
        str((vacancy.get("employer") or {}).get("name") or ""),
    ]
    return "\n".join(parts).lower()


def score_vacancy(vacancy: dict[str, Any]) -> dict[str, Any]:
    text = vacancy_text(vacancy)
    title = str(vacancy.get("name") or "").lower()
    score = 20
    reasons: list[str] = []
    gaps: list[str] = []
    for label, weight, needles in POSITIVE_FAMILIES:
        matched = [needle for needle in needles if needle in text]
        if matched:
            title_bonus = min(5, sum(1 for needle in matched if needle in title) * 2)
            score += weight + title_bonus
            reasons.append(label)
    for label, needles in GAP_FAMILIES:
        if any(needle in text for needle in needles):
            gaps.append(label)
            score -= 8
    if vacancy.get("salary"):
        score += 3
        reasons.append("указана зарплата")
    if vacancy.get("response_letter_required"):
        reasons.append("требуется сопроводительное")
    score = max(0, min(score, 100))
    return {"score": score, "reasons": reasons, "gaps_to_check": gaps}


def salary_label(salary: dict[str, Any] | None) -> str | None:
    if not salary:
        return None
    from_value = salary.get("from")
    to_value = salary.get("to")
    currency = salary.get("currency") or ""
    gross = "до вычета налогов" if salary.get("gross") else "на руки"
    if from_value and to_value:
        amount = f"{from_value:,}–{to_value:,}".replace(",", " ")
    elif from_value:
        amount = f"от {from_value:,}".replace(",", " ")
    elif to_value:
        amount = f"до {to_value:,}".replace(",", " ")
    else:
        return None
    return f"{amount} {currency}, {gross}".strip()


def normalize_vacancy(vacancy: dict[str, Any], *, include_description: bool = False) -> dict[str, Any]:
    employer = vacancy.get("employer") or {}
    area = vacancy.get("area") or {}
    result: dict[str, Any] = {
        "id": str(vacancy.get("id") or ""),
        "name": vacancy.get("name"),
        "employer": employer.get("name"),
        "area": area.get("name"),
        "salary": salary_label(vacancy.get("salary")),
        "published_at": vacancy.get("published_at"),
        "alternate_url": vacancy.get("alternate_url"),
        "response_letter_required": vacancy.get("response_letter_required"),
        "has_test": vacancy.get("has_test"),
        "relations": vacancy.get("relations") or [],
        "match": score_vacancy(vacancy),
    }
    snippet = vacancy.get("snippet") or {}
    if snippet:
        result["snippet"] = {
            "requirement": plain_text(snippet.get("requirement")),
            "responsibility": plain_text(snippet.get("responsibility")),
        }
    if include_description:
        result.update(
            {
                "description": plain_text(vacancy.get("description")),
                "experience": (vacancy.get("experience") or {}).get("name"),
                "employment": (vacancy.get("employment") or {}).get("name"),
                "schedule": (vacancy.get("schedule") or {}).get("name"),
                "key_skills": [skill.get("name") for skill in vacancy.get("key_skills") or [] if skill.get("name")],
                "suitable_resumes_url": vacancy.get("suitable_resumes_url"),
                "negotiations_url": vacancy.get("negotiations_url"),
            }
        )
    return result
