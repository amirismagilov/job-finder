# region MODULE_CONTRACT [DOMAIN(10): GroundedGeneration; CONCEPT(10): StructuredOutput, AIBoundary; TECH(9): OpenAICompatibleHTTP]
## @file llm.py
## @brief Provider-neutral structured LLM adapter for relevance and grounded text only.
## @modulecontract
## @purpose Use AI for the three authorized semantic tasks while keeping network actions and policy outside model control.
## @scope OpenAI-compatible chat completions, strict result parsing and grounding checks.
## @input Sanitized vacancy/chat excerpts and selected knowledge chunks.
## @output RelevanceDecision or GeneratedText.
## @invariants Secrets, transport commands, snake_case/camelCase identifiers and URLs are removed before model calls; unsupported claims fail validation.
## @changes LAST_CHANGE: [v0.2.1 — Closed identifier leaks for exact id and camelCase chat/vacancy/message fields.]
## @modulemap
## CLASS 9[Structured relevance result] => RelevanceDecision
## CLASS 9[Structured generated text result] => GeneratedText
## CLASS 10[OpenAI-compatible authorized AI operations] => LLMProvider
## FUNC 10[Removes credentials, URLs and all identifier spellings] => sanitize_ai_context
## FUNC 10[Checks confidence, unsupported facts and Sber confidentiality] => validate_grounded_text
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: LLM, OpenAI compatible, structured JSON, grounded, unsupported claims, Sber confidentiality
# STRUCTURE: sanitized context -> strict prompt -> JSON parse -> typed result -> grounding validator

from dataclasses import dataclass
import json
import logging
import re
import ssl
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import LLMConfig


logger = logging.getLogger(__name__)
SENSITIVE_KEYS = {"url", "alternateurl", "headers", "cookie", "csrf", "xsrf", "token", "authorization", "pairingsecret", "apikey"}
IDENTIFIER_KEYS = {
    "id",
    "chatid",
    "vacancyid",
    "messageid",
    "participantid",
    "resumeid",
    "topicid",
    "participantsids",
    "idempotencykey",
    "countryids",
    "unusedresumeids",
}
URL_RE = re.compile(r"https?://\S+", re.I)


class SecretStore(Protocol):
    def get(self, account: str) -> str | None: ...


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelevanceDecision:
    score: int
    reasons: tuple[str, ...]
    gaps: tuple[str, ...]
    decision: str
    confidence: float
    unknowns: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedText:
    text: str
    confidence: float
    used_facts: tuple[str, ...]
    unknowns: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    grounded: bool
    needs_attention: bool = False


# region FUNC_sanitize_ai_context [DOMAIN(10): AISafety; CONCEPT(10): IdentifierMinimization; TECH(8): RecursiveProjection]
## @purpose Remove credentials, URLs and all contract identifier spellings before any context reaches an LLM.
## @io Any -> bounded identifier-free Any
## @complexity 8
def sanitize_ai_context(value: Any) -> Any:
    """nested context -> normalize key spelling -> drop exact sensitive/identifier keys -> redact URLs."""
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered_name = name.lower()
            normalized = re.sub(r"[^a-z0-9]", "", name.lower())
            # BUG_FIX_CONTEXT: suffix-only snake_case filtering missed exact `id` and camelCase
            # contract keys; exact normalized names remove identifiers without matching words such as `identity`.
            camel_identifier = bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*Ids?", name)) and ("Id" in name)
            if normalized in SENSITIVE_KEYS or normalized in IDENTIFIER_KEYS or lowered_name.endswith("_id") or camel_identifier:
                continue
            output[name] = sanitize_ai_context(item)
        return output
    if isinstance(value, list):
        return [sanitize_ai_context(v) for v in value[:100]]
    if isinstance(value, str):
        return URL_RE.sub("[ссылка удалена]", value)[:40_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]
# endregion FUNC_sanitize_ai_context


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise LLMError(f"LLM вернул некорректное поле {name}")
    return tuple(item.strip() for item in value if item.strip())


# region FUNC_validate_grounded_text [DOMAIN(10): AISafety; CONCEPT(10): Grounding; TECH(8): DeterministicValidation]
## @purpose Block low-confidence, overlong, ungrounded or confidentiality-violating model text before any write.
## @io GeneratedText, source text, limits, employer -> None or LLMError
## @complexity 7
def validate_grounded_text(result: GeneratedText, source_text: str, *, max_length: int, minimum_confidence: float, employer: str = "") -> None:
    text = result.text.strip()
    if not text or len(text) > max_length:
        raise LLMError("Сгенерированный текст пуст или превышает лимит")
    if not result.grounded or result.confidence < minimum_confidence or result.unsupported_claims:
        raise LLMError("Модель не подтвердила grounded-результат")
    source_lower = source_text.lower()
    for fact in result.used_facts:
        fact_tokens = {token for token in re.findall(r"[a-zа-яё0-9]{4,}", fact.lower())}
        if fact_tokens and len([token for token in fact_tokens if token in source_lower]) < max(1, len(fact_tokens) // 3):
            raise LLMError("used_facts содержит неподтверждённый факт")
    for number in re.findall(r"\b\d{2,}(?:[.,]\d+)?%?\b", text):
        if number not in source_text:
            raise LLMError("Текст содержит неподтверждённую числовую метрику")
    if "сбер" in employer.lower():
        lowered = text.lower()
        if "истра" in lowered or "сбер университет" in lowered or "sber university" in lowered:
            raise LLMError("Нарушено правило конфиденциальности для Сбера")
# endregion FUNC_validate_grounded_text


# region CLASS_LLMProvider [DOMAIN(10): GroundedGeneration; CONCEPT(10): AuthorizedAITasks; TECH(9): JSONHTTP]
## @purpose Provide exactly relevance scoring, cover letters and recruiter replies through structured JSON.
class LLMProvider:
    def __init__(self, config: LLMConfig, secrets: SecretStore) -> None:
        self.config = config
        self.secrets = secrets

    def _complete(self, task: str, context: dict[str, Any], required: str) -> dict[str, Any]:
        safe_context = sanitize_ai_context(context)
        system = (
            "Ты работаешь только с переданными подтверждёнными данными кандидата. Нельзя придумывать опыт, навыки, "
            "метрики, даты или обязательства. ИИ не управляет сетью и не предлагает HTTP-действия. "
            f"Задача: {task}. Верни только JSON. Обязательные поля: {required}."
        )
        body = {
            "model": self.config.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(safe_context, ensure_ascii=False)},
            ],
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = self.secrets.get(self.config.api_key_account)
        if key:
            headers["Authorization"] = "Bearer " + key
        logger.info("[IMP:9][LLMProvider][GROUNDING] Structured authorized AI task started: %s", task)
        request = Request(self.config.endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.config.timeout_seconds, context=ssl.create_default_context()) as response:
                raw = response.read(1_000_001)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise LLMError("LLM endpoint недоступен") from exc
        if len(raw) > 1_000_000:
            raise LLMError("Ответ LLM превышает лимит")
        try:
            response_body = json.loads(raw)
            content = response_body["choices"][0]["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("LLM вернул ответ неизвестной схемы") from exc
        if not isinstance(result, dict):
            raise LLMError("LLM обязан вернуть JSON-объект")
        return result

    def assess_relevance(self, vacancy: dict[str, Any], knowledge: str) -> RelevanceDecision:
        raw = self._complete(
            "Оцени соответствие вакансии опыту кандидата по шкале 0-100",
            {"vacancy": vacancy, "candidate_facts": knowledge},
            "score,reasons,gaps,decision,confidence,unknowns",
        )
        try:
            score, confidence = int(raw["score"]), float(raw["confidence"])
            decision = str(raw["decision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError("Некорректная оценка релевантности") from exc
        if not 0 <= score <= 100 or not 0 <= confidence <= 1 or decision not in {"apply", "skip"}:
            raise LLMError("Оценка релевантности вне допустимого диапазона")
        return RelevanceDecision(score, _string_tuple(raw.get("reasons"), "reasons"), _string_tuple(raw.get("gaps"), "gaps"), decision, confidence, _string_tuple(raw.get("unknowns", []), "unknowns"))

    def generate_cover_letter(self, vacancy: dict[str, Any], knowledge: str, rules: str) -> GeneratedText:
        return self._generated(self._complete(
            "Напиши сопроводительное письмо на русском строго по правилам; для работодателя Сбер анонимизируй кейсы и не называй Истру или Сбер Университет",
            {"vacancy": vacancy, "candidate_facts": knowledge, "cover_letter_rules": rules},
            "text,confidence,used_facts,unknowns,unsupported_claims,grounded,needs_attention",
        ))

    def generate_chat_reply(self, vacancy: dict[str, Any], messages: list[dict[str, Any]], knowledge: str) -> GeneratedText:
        return self._generated(self._complete(
            "Ответь рекрутеру по-русски. Если факта нет, не угадывай: дай нейтральную формулировку и перечисли unknowns",
            {"vacancy": vacancy, "chat_history": messages, "candidate_facts": knowledge},
            "text,confidence,used_facts,unknowns,unsupported_claims,grounded,needs_attention",
        ))

    @staticmethod
    def _generated(raw: dict[str, Any]) -> GeneratedText:
        try:
            return GeneratedText(
                str(raw["text"]).strip(), float(raw["confidence"]), _string_tuple(raw["used_facts"], "used_facts"),
                _string_tuple(raw["unknowns"], "unknowns"), _string_tuple(raw["unsupported_claims"], "unsupported_claims"),
                raw["grounded"] is True, raw.get("needs_attention") is True,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError("Некорректный структурированный текст LLM") from exc
# endregion CLASS_LLMProvider
