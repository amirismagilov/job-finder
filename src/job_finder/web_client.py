# region MODULE_CONTRACT [DOMAIN(10): HHWebAdapter; CONCEPT(9): HARContract, Normalization; TECH(8): ProjectedJSON, MonotonicClock]
## @file web_client.py
## @brief Strict business adapter over typed browser-projected data.
## @modulecontract
## @purpose Convert vacancy/application/chat operations into typed commands, throttle reads, and reject every body outside the browser projector contract.
## @scope Search, vacancy details, application preflight/write, recruiter chats and shared deterministic read cadence.
## @input Business identifiers and generated text.
## @output Normalized dictionaries and semantic write receipts.
## @invariants No URL, header or raw HTML enters the backend; application write always follows fresh preflight; every read shares one deterministic throttler.
## @rationale
## Q: Why is HTML parsing absent from the backend?
## A: Session pages can embed credentials in arbitrary markup; only bounded public fields projected inside the browser may cross the bridge.
## @changes LAST_CHANGE: [v0.2.1 — Enforced projected-only schemas, deterministic read throttling and optional country preflight data.]
## @modulemap
## CLASS 10[HH web business boundary] => HHWebClient
## CLASS 9[Immutable application eligibility result] => ApplicationPreflight
## CLASS 9[Common monotonic read cadence] => ReadThrottler
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: hh web client, browser-projected vacancy, response popup, resume hash, chatik, read throttler, idempotency
# STRUCTURE: business call -> shared read cadence -> WebAction -> strict projected schema -> normalized result

from dataclasses import dataclass
import threading
import time
from typing import Any, Protocol
import uuid

from .web_contract import CommandEnvelope, ResponseClass, ResponseEnvelope, WebAction, classify_response, new_command


class BridgeTransport(Protocol):
    def enqueue(self, command: CommandEnvelope) -> ResponseEnvelope: ...


class WebClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApplicationPreflight:
    vacancy_id: str
    allowed: bool
    reason: str | None
    resume_hash: str | None
    letter_max_length: int
    country_ids: tuple[str, ...] = ()


READ_ACTIONS = frozenset({
    WebAction.SEARCH_VACANCIES,
    WebAction.GET_VACANCY,
    WebAction.GET_RESPONSE_POPUP,
    WebAction.LIST_CHATS,
    WebAction.GET_CHAT_DATA,
})


def _json_body(response: ResponseEnvelope, name: str) -> dict[str, Any]:
    if not isinstance(response.body, dict):
        raise WebClientError(f"Неизвестная схема {name}")
    return response.body


def parse_search_body(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise WebClientError("Неизвестная схема проекции результатов поиска")
    return [dict(item) for item in body["items"]]


def parse_vacancy_body(body: Any, vacancy_id: str) -> dict[str, Any]:
    if not isinstance(body, dict) or body.get("id") != vacancy_id:
        raise WebClientError("Неизвестная схема проекции вакансии")
    return dict(body)


# region CLASS_ReadThrottler [DOMAIN(9): HHWebAdapter; CONCEPT(9): DeterministicCadence; TECH(8): MonotonicClock]
## @purpose Apply one shared minimum interval to all read actions without jitter, stealth or action-specific exceptions.
class ReadThrottler:
    def __init__(self, minimum_interval_seconds: float, *, clock: Any = time.monotonic, sleep: Any = time.sleep) -> None:
        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))
        self._clock = clock
        self._sleep = sleep
        self._last_started: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        """serialized monotonic timestamp -> exact remaining delay -> next read-start timestamp."""
        with self._lock:
            now = float(self._clock())
            if self._last_started is not None:
                remaining = self.minimum_interval_seconds - (now - self._last_started)
                if remaining > 0:
                    # BUG_FIX_CONTEXT: minimum_read_interval_seconds existed only in configuration;
                    # a shared deterministic sleeper now enforces it before every browser read.
                    self._sleep(remaining)
                    now = float(self._clock())
            self._last_started = now
# endregion CLASS_ReadThrottler


# region CLASS_HHWebClient [DOMAIN(10): HHWebAdapter; CONCEPT(10): PreflightAndWrite; TECH(9): TypedBridge]
## @purpose Offer deterministic HH operations while delegating session-bound fetches to the paired extension.
class HHWebClient:
    def __init__(self, bridge: BridgeTransport, *, read_throttler: ReadThrottler | None = None) -> None:
        self.bridge = bridge
        self.read_throttler = read_throttler or ReadThrottler(0)

    def _call(self, action: WebAction, **params: Any) -> ResponseEnvelope:
        if action in READ_ACTIONS:
            self.read_throttler.wait()
        try:
            response = self.bridge.enqueue(new_command(action, params))
            outcome = classify_response(action, response)
        except WebClientError:
            raise
        except (TypeError, KeyError, ValueError) as exc:
            # BUG_FIX_CONTEXT: Coercion/parsing exceptions previously escaped the worker's contract-drift
            # handler; all browser schema failures now use the single WebClientError boundary.
            raise WebClientError("Ответ browser bridge нарушил web-контракт") from exc
        if outcome is not ResponseClass.SUCCESS:
            raise WebClientError(f"Web-контракт отклонён: {outcome.value}")
        return response

    def search_vacancies(self, *, text: str, area: str, period: int = 14, page: int = 0, per_page: int = 10) -> dict[str, Any]:
        response = self._call(WebAction.SEARCH_VACANCIES, text=text, area=area)
        try:
            items = parse_search_body(response.body)[: max(1, min(int(per_page), 20))]
            return {"items": items, "found": int(response.body["found"])}
        except (TypeError, KeyError, ValueError) as exc:
            raise WebClientError("Проекция поиска нарушила web-контракт") from exc

    def get_vacancy(self, vacancy_id: str) -> dict[str, Any]:
        response = self._call(WebAction.GET_VACANCY, vacancy_id=vacancy_id)
        return parse_vacancy_body(response.body, vacancy_id)

    def application_preflight(self, vacancy_id: str) -> ApplicationPreflight:
        body = _json_body(self._call(WebAction.GET_RESPONSE_POPUP, vacancy_id=vacancy_id), "response popup")
        status = body.get("responseStatus")
        if not isinstance(status, dict):
            raise WebClientError("response popup не содержит responseStatus")
        if status.get("alreadyApplied"):
            return ApplicationPreflight(vacancy_id, False, "already_applied", None, 0)
        if status.get("responseImpossible"):
            return ApplicationPreflight(vacancy_id, False, "response_impossible", None, 0)
        test = status.get("test") or {}
        short = status.get("shortVacancy") or {}
        if (isinstance(test, dict) and test.get("hasTests")) or (isinstance(short, dict) and short.get("userTestPresent")):
            return ApplicationPreflight(vacancy_id, False, "vacancy_test", None, 0)
        popup = body.get("responsePopup") or {}
        if isinstance(popup, dict) and popup.get("startedWithQuestion"):
            return ApplicationPreflight(vacancy_id, False, "external_or_additional_questions", None, 0)
        resumes = status.get("resumes")
        unused = status.get("unusedResumeIds") or []
        if not isinstance(resumes, dict) or not unused:
            return ApplicationPreflight(vacancy_id, False, "no_available_resume", None, 0)
        chosen: str | None = None
        for resume_id in unused:
            resume = resumes.get(str(resume_id)) or resumes.get(resume_id)
            if isinstance(resume, dict) and resume.get("hash") and not resume.get("isIncomplete") and not resume.get("forbidden"):
                chosen = str(resume["hash"])
                break
        if not chosen:
            return ApplicationPreflight(vacancy_id, False, "no_available_resume", None, 0)
        maximum = status.get("letterMaxLength", 10_000)
        if not isinstance(maximum, int) or not 1 <= maximum <= 10_000:
            raise WebClientError("Некорректный letterMaxLength")
        countries = tuple(body.get("countryIds", []))
        return ApplicationPreflight(vacancy_id, True, None, chosen, maximum, countries)

    def apply(self, vacancy_id: str, resume_hash: str, letter: str, *, country_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        params: dict[str, Any] = {"vacancy_id": vacancy_id, "resume_hash": resume_hash, "letter": letter}
        if country_ids:
            params["country_ids"] = list(country_ids)
        body = _json_body(self._call(WebAction.APPLY, **params), "application result")
        return {"success": True, "topic_id": str(body.get("topic_id") or ""), "chat_id": str(body.get("chat_id") or "")}

    def list_chats(self, *, unread_only: bool = True) -> list[dict[str, Any]]:
        body = _json_body(self._call(WebAction.LIST_CHATS, unread_only=unread_only), "chats")
        chats = body.get("chats")
        items = chats.get("items") if isinstance(chats, dict) else body.get("items")
        if not isinstance(items, list):
            raise WebClientError("Список чатов имеет неизвестную схему")
        result: list[dict[str, Any]] = []
        for item in items:
            result.append({"id": item["id"], "unread_count": item["unreadCount"]})
        return result

    def chat_messages(self, chat_id: str) -> dict[str, Any]:
        body = _json_body(self._call(WebAction.GET_CHAT_DATA, chat_id=chat_id), "chat data")
        chat = body.get("chat")
        if not isinstance(chat, dict):
            raise WebClientError("chat_data не содержит chat")
        items = chat["messages"]["items"]
        current = chat["currentParticipantId"]
        messages = []
        for item in items:
            messages.append({"id": item["id"], "text": item["text"], "is_current_user": item["participantId"] == current, "created_at": item["creationTime"]})
        vacancy_ids = chat["resources"]["VACANCY"]
        return {"id": chat_id, "messages": messages, "vacancy_id": vacancy_ids[0] if vacancy_ids else None, "write_allowed": body["chatStates"]["writeMessageState"]["allowed"]}

    def send_chat_message(self, chat_id: str, text: str, idempotency_key: str | None = None) -> dict[str, Any]:
        key = idempotency_key or str(uuid.uuid4())
        body = _json_body(self._call(WebAction.SEND_CHAT_MESSAGE, chat_id=chat_id, text=text, idempotency_key=key), "chat send")
        if body["chatId"] != chat_id:
            raise WebClientError("Receipt сообщения относится к другому чату")
        return {"id": str(body["id"]), "chat_id": str(body["chatId"]), "idempotency_key": key}

    def mark_read(self, chat_id: str, message_id: str) -> None:
        self._call(WebAction.MARK_READ, chat_id=chat_id, message_id=message_id)
# endregion CLASS_HHWebClient
