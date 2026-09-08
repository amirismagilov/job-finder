# region MODULE_CONTRACT [DOMAIN(10): HHWebTransport, Security; CONCEPT(10): TypedAllowlist, SafetyClassification; TECH(9): Dataclasses, Enum]
## @file web_contract.py
## @brief Closed command protocol shared by the backend and Chromium extension.
## @modulecontract
## @purpose Make arbitrary URLs, methods, headers and payload fields unrepresentable, and accept only action-specific projected response bodies.
## @scope Command validation, nested response schemas and deterministic safety classification.
## @input Typed action plus small scalar params; bounded browser response.
## @output Validated envelopes and ResponseClass.
## @invariants Credentials, raw HTML and network destinations never appear in an envelope; unknown nested schemas never count as successful writes.
## @rationale
## Q: Why validate projected response bodies twice, in JavaScript and Python?
## A: The extension minimizes data at the browser boundary; the backend independently fails closed if that projector drifts or is tampered with.
## @changes LAST_CHANGE: [v0.2.2 — Protection classification now uses explicit signals and strong bounded messages, avoiding vocabulary false positives.]
## @modulemap
## CLASS 10[Closed list of browser operations] => WebAction
## FUNC 10[Rejects unknown or unsafe command shapes] => validate_command
## FUNC 10[Validates exact action-specific projected response topology] => validate_response_body
## FUNC 10[Turns transport outcomes into hard safety states] => classify_response
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: WebAction, CommandEnvelope, ResponseEnvelope, strict params, projected response allowlist, nested schema, redirect protection, hard stop
# STRUCTURE: action enum + exact params -> browser projector -> strict nested validator -> redirect/body safety classifier

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import re
from typing import Any, Mapping
import uuid


class WebAction(StrEnum):
    SEARCH_VACANCIES = "SEARCH_VACANCIES"
    GET_VACANCY = "GET_VACANCY"
    GET_RESPONSE_POPUP = "GET_RESPONSE_POPUP"
    APPLY = "APPLY"
    LIST_CHATS = "LIST_CHATS"
    GET_CHAT_DATA = "GET_CHAT_DATA"
    SEND_CHAT_MESSAGE = "SEND_CHAT_MESSAGE"
    MARK_READ = "MARK_READ"


class ResponseClass(StrEnum):
    SUCCESS = "success"
    AUTH_REQUIRED = "auth_required"
    PROTECTION = "protection"
    RATE_LIMIT = "rate_limit"
    CONTRACT_DRIFT = "contract_drift"
    AMBIGUOUS_WRITE = "ambiguous_write"
    FAILURE = "failure"

    @property
    def hard_stop(self) -> bool:
        return self in {self.AUTH_REQUIRED, self.PROTECTION, self.RATE_LIMIT, self.CONTRACT_DRIFT, self.AMBIGUOUS_WRITE}


@dataclass(frozen=True)
class CommandEnvelope:
    command_id: str
    action: WebAction
    params: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"command_id": self.command_id, "action": self.action.value, "params": self.params, "created_at": self.created_at}


@dataclass(frozen=True)
class ResponseEnvelope:
    command_id: str
    status: int
    content_type: str
    body: Any
    redirect_path: str = ""


class ContractError(ValueError):
    pass


ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
SENSITIVE_NAMES = {"url", "path", "method", "headers", "cookie", "cookies", "authorization", "csrf", "xsrf", "token", "pairing_secret", "api_key"}
PARAMS: dict[WebAction, frozenset[str]] = {
    WebAction.SEARCH_VACANCIES: frozenset({"text", "area"}),
    WebAction.GET_VACANCY: frozenset({"vacancy_id"}),
    WebAction.GET_RESPONSE_POPUP: frozenset({"vacancy_id"}),
    WebAction.APPLY: frozenset({"vacancy_id", "resume_hash", "letter", "country_ids"}),
    WebAction.LIST_CHATS: frozenset({"unread_only"}),
    WebAction.GET_CHAT_DATA: frozenset({"chat_id"}),
    WebAction.SEND_CHAT_MESSAGE: frozenset({"chat_id", "text", "idempotency_key"}),
    WebAction.MARK_READ: frozenset({"chat_id", "message_id"}),
}
REQUIRED: dict[WebAction, frozenset[str]] = {
    WebAction.SEARCH_VACANCIES: frozenset({"text", "area"}),
    WebAction.GET_VACANCY: frozenset({"vacancy_id"}),
    WebAction.GET_RESPONSE_POPUP: frozenset({"vacancy_id"}),
    WebAction.APPLY: frozenset({"vacancy_id", "resume_hash", "letter"}),
    WebAction.LIST_CHATS: frozenset(),
    WebAction.GET_CHAT_DATA: frozenset({"chat_id"}),
    WebAction.SEND_CHAT_MESSAGE: frozenset({"chat_id", "text", "idempotency_key"}),
    WebAction.MARK_READ: frozenset({"chat_id", "message_id"}),
}
WRITE_ACTIONS = frozenset({WebAction.APPLY, WebAction.SEND_CHAT_MESSAGE, WebAction.MARK_READ})


def new_command(action: WebAction, params: Mapping[str, Any]) -> CommandEnvelope:
    envelope = CommandEnvelope(str(uuid.uuid4()), action, dict(params), datetime.now(UTC).isoformat())
    validate_command(envelope)
    return envelope


# region FUNC_validate_command [DOMAIN(10): Security; CONCEPT(10): LeastPrivilege; TECH(9): SchemaValidation]
## @purpose Admit only known operations with exact fields and conservative scalar limits.
## @io CommandEnvelope -> CommandEnvelope or ContractError
## @complexity 7
def validate_command(command: CommandEnvelope) -> CommandEnvelope:
    if not UUID_RE.fullmatch(command.command_id):
        raise ContractError("Некорректный command_id")
    if not isinstance(command.action, WebAction):
        raise ContractError("Неизвестное действие")
    keys = set(command.params)
    if keys != keys - SENSITIVE_NAMES or not REQUIRED[command.action] <= keys or not keys <= PARAMS[command.action]:
        raise ContractError("Набор параметров не разрешён контрактом")
    for key in ("vacancy_id", "chat_id", "message_id", "resume_hash"):
        if key in command.params and not ID_RE.fullmatch(str(command.params[key])):
            raise ContractError(f"Некорректный идентификатор {key}")
    for key, maximum in (("text", 5000), ("letter", 10000)):
        if key in command.params:
            value = command.params[key]
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ContractError(f"Некорректное поле {key}")
    if command.action is WebAction.SEARCH_VACANCIES:
        text = command.params.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ContractError("Некорректный поисковый текст")
    if command.action is WebAction.LIST_CHATS and "unread_only" in command.params and not isinstance(command.params["unread_only"], bool):
        raise ContractError("unread_only должен быть bool")
    if command.action is WebAction.APPLY and "country_ids" in command.params:
        country_ids = command.params["country_ids"]
        if not isinstance(country_ids, list) or len(country_ids) > 20 or any(not isinstance(item, str) or not ID_RE.fullmatch(item) for item in country_ids):
            raise ContractError("country_ids должен быть ограниченным списком идентификаторов")
    if command.action is WebAction.SEND_CHAT_MESSAGE and not UUID_RE.fullmatch(str(command.params["idempotency_key"])):
        raise ContractError("idempotency_key должен быть UUIDv4")
    return command
# endregion FUNC_validate_command


def response_from_dict(value: Mapping[str, Any], *, max_body_bytes: int = 1_000_000) -> ResponseEnvelope:
    allowed = {"command_id", "status", "content_type", "body", "redirect_path"}
    if set(value) - allowed:
        raise ContractError("Ответ расширения содержит неизвестные поля")
    command_id = str(value.get("command_id", ""))
    if not UUID_RE.fullmatch(command_id):
        raise ContractError("Ответ содержит некорректный command_id")
    status = value.get("status")
    if not isinstance(status, int) or not 0 <= status <= 599:
        raise ContractError("Некорректный HTTP status")
    content_type_value = value.get("content_type", "")
    redirect_path_value = value.get("redirect_path", "")
    if not isinstance(content_type_value, str) or not isinstance(redirect_path_value, str):
        raise ContractError("content_type и redirect_path обязаны быть строками")
    content_type = content_type_value[:200]
    redirect_path = redirect_path_value[:500]
    if redirect_path and (not redirect_path.startswith("/") or "://" in redirect_path):
        raise ContractError("redirect_path обязан быть относительным")
    body = value.get("body")
    if len(repr(body).encode("utf-8", errors="replace")) > max_body_bytes:
        raise ContractError("Ответ расширения превышает лимит")
    if _contains_sensitive(body):
        raise ContractError("Ответ расширения содержит session credential")
    return ResponseEnvelope(command_id, status, content_type, body, redirect_path)


def _contains_sensitive(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower().replace("-", "_")
            if any(marker in lowered for marker in ("cookie", "authorization", "csrf", "xsrf", "pairing_secret", "api_key")):
                return True
            if _contains_sensitive(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive(item) for item in value[:1000])
    if isinstance(value, str):
        lowered = value.lower()
        return "set-cookie:" in lowered or bool(re.search(r"(?:csrf|xsrf)[\"']?\s*[:=]\s*[\"'][^\[]", lowered))
    return False


def _explicit_protection(body: Any) -> bool:
    """Recognize bounded semantic protection responses, never incidental vacancy/chat vocabulary."""
    if not isinstance(body, dict):
        return False

    def active_flag(value: Any) -> bool:
        if value is None or value is False or value == 0:
            return False
        if isinstance(value, str):
            return value.strip().lower() not in {"", "false", "none", "no", "0"}
        return True

    stack: list[tuple[Any, int]] = [(body, 0)]
    visited = 0
    while stack and visited < 256:
        value, depth = stack.pop()
        visited += 1
        if depth > 6 or not isinstance(value, (dict, list)):
            continue
        entries = list(value.items())[:50] if isinstance(value, dict) else [(str(index), item) for index, item in enumerate(value[:50])]
        for key_value, item in entries:
            key = str(key_value).lower().replace("_", "").replace("-", "")
            if key in {"captcha", "challenge"} and active_flag(item):
                return True
            if key == "protection" and isinstance(item, str) and re.search(r"captcha|challenge|cf-chl-", item[:100], re.I):
                return True
            if key in {"message", "error", "errormessage", "detail"} and isinstance(item, str) and len(item) <= 4096:
                normalized = " ".join(item.lower().split())
                if re.search(r"(?:подтвердите|докажите|пройдите проверку)[^.!?]{0,100}(?:вы )?(?:не )?робот|(?:verify|prove)[^.!?]{0,100}(?:you are|you're)?[^.!?]{0,40}human|complete (?:the )?captcha|captcha verification|security challenge required", normalized):
                    return True
            if isinstance(item, (dict, list)):
                stack.append((item, depth + 1))
    return False


def _plain_string(value: Any, maximum: int, *, allow_empty: bool = True) -> bool:
    return isinstance(value, str) and len(value) <= maximum and (allow_empty or bool(value.strip()))


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_RE.fullmatch(value))


def _exact_object(value: Any, allowed: set[str], required: set[str] | None = None) -> bool:
    return isinstance(value, dict) and set(value) <= allowed and (required or set()) <= set(value)


def _public_vacancy(value: Any, *, details: bool) -> bool:
    allowed = {"id", "name", "description", "employer", "area", "published_at"}
    required = {"id", "name", "description"} if details else {"id", "name"}
    if not _exact_object(value, allowed, required) or not _identifier(value["id"]) or not _plain_string(value["name"], 500):
        return False
    if "description" in value and not _plain_string(value["description"], 200_000):
        return False
    for key in ("employer", "area"):
        if key in value and (not _exact_object(value[key], {"name"}) or not _plain_string(value[key].get("name"), 500)):
            return False
    return "published_at" not in value or value["published_at"] is None or _plain_string(value["published_at"], 100)


# region FUNC_validate_response_body [DOMAIN(10): Security; CONCEPT(10): FailClosedProjection; TECH(9): NestedSchemaValidation]
## @purpose Confirm that the browser returned exactly the bounded public/action fields the backend understands, never raw HTML or credential-bearing extras.
## @io WebAction, body -> bool
## @complexity 10
def validate_response_body(action: WebAction, body: Any) -> bool:
    """action -> exact object/list topology -> scalar and identifier bounds -> boolean verdict.

    This validator deliberately avoids coercion. A malformed unread counter, participant identifier,
    message payload or popup field is contract drift rather than a partially usable response.
    """
    if action is WebAction.SEARCH_VACANCIES:
        return (
            _exact_object(body, {"items", "found"}, {"items", "found"})
            and isinstance(body["items"], list)
            and len(body["items"]) <= 100
            and all(_public_vacancy(item, details=False) for item in body["items"])
            and isinstance(body["found"], int)
            and not isinstance(body["found"], bool)
            and body["found"] >= 0
        )
    if action is WebAction.GET_VACANCY:
        return _public_vacancy(body, details=True)
    if action is WebAction.GET_RESPONSE_POPUP:
        if not _exact_object(body, {"responseStatus", "responsePopup", "countryIds"}, {"responseStatus", "responsePopup"}):
            return False
        status, popup = body["responseStatus"], body["responsePopup"]
        status_keys = {"alreadyApplied", "responseImpossible", "test", "shortVacancy", "unusedResumeIds", "resumes", "letterMaxLength"}
        if not _exact_object(status, status_keys, status_keys):
            return False
        if type(status["alreadyApplied"]) is not bool or type(status["responseImpossible"]) is not bool:
            return False
        if not _exact_object(status["test"], {"hasTests"}, {"hasTests"}) or type(status["test"]["hasTests"]) is not bool:
            return False
        if not _exact_object(status["shortVacancy"], {"userTestPresent"}, {"userTestPresent"}) or type(status["shortVacancy"]["userTestPresent"]) is not bool:
            return False
        if not isinstance(status["unusedResumeIds"], list) or len(status["unusedResumeIds"]) > 20 or any(not _identifier(item) for item in status["unusedResumeIds"]):
            return False
        resumes = status["resumes"]
        if not isinstance(resumes, dict) or len(resumes) > 20:
            return False
        for resume_id, resume in resumes.items():
            if not _identifier(resume_id) or not _exact_object(resume, {"hash", "isIncomplete", "forbidden"}, {"hash", "isIncomplete", "forbidden"}):
                return False
            if not _identifier(resume["hash"]) or type(resume["isIncomplete"]) is not bool or (resume["forbidden"] is not None and type(resume["forbidden"]) is not bool):
                return False
        maximum = status["letterMaxLength"]
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 10_000:
            return False
        if not _exact_object(popup, {"startedWithQuestion"}, {"startedWithQuestion"}) or type(popup["startedWithQuestion"]) is not bool:
            return False
        countries = body.get("countryIds", [])
        return isinstance(countries, list) and len(countries) <= 20 and all(_identifier(item) for item in countries)
    if action is WebAction.APPLY:
        return (
            _exact_object(body, {"success", "topic_id", "chat_id"}, {"success", "topic_id", "chat_id"})
            and body["success"] is True
            and (body["topic_id"] == "" or _identifier(body["topic_id"]))
            and (body["chat_id"] == "" or _identifier(body["chat_id"]))
            and bool(body["topic_id"] or body["chat_id"])
        )
    if action is WebAction.LIST_CHATS:
        if not _exact_object(body, {"items"}, {"items"}) or not isinstance(body["items"], list) or len(body["items"]) > 200:
            return False
        return all(
            _exact_object(item, {"id", "unreadCount"}, {"id", "unreadCount"})
            and _identifier(item["id"])
            and isinstance(item["unreadCount"], int)
            and not isinstance(item["unreadCount"], bool)
            and item["unreadCount"] >= 0
            for item in body["items"]
        )
    if action is WebAction.GET_CHAT_DATA:
        if not _exact_object(body, {"chat", "chatStates"}, {"chat", "chatStates"}):
            return False
        chat, states = body["chat"], body["chatStates"]
        if not _exact_object(chat, {"messages", "currentParticipantId", "resources"}, {"messages", "currentParticipantId", "resources"}):
            return False
        if not _identifier(chat["currentParticipantId"]):
            return False
        messages = chat["messages"]
        if not _exact_object(messages, {"items"}, {"items"}) or not isinstance(messages["items"], list) or len(messages["items"]) > 200:
            return False
        for item in messages["items"]:
            if not _exact_object(item, {"id", "participantId", "text", "creationTime"}, {"id", "participantId", "text", "creationTime"}):
                return False
            if not _identifier(item["id"]) or not _identifier(item["participantId"]) or not _plain_string(item["text"], 5000):
                return False
            if item["creationTime"] is not None and not _plain_string(item["creationTime"], 100):
                return False
        resources = chat["resources"]
        if not _exact_object(resources, {"VACANCY"}, {"VACANCY"}) or not isinstance(resources["VACANCY"], list) or any(not _identifier(item) for item in resources["VACANCY"]):
            return False
        return _exact_object(states, {"writeMessageState"}, {"writeMessageState"}) and _exact_object(states["writeMessageState"], {"allowed"}, {"allowed"}) and type(states["writeMessageState"]["allowed"]) is bool
    if action is WebAction.SEND_CHAT_MESSAGE:
        return _exact_object(body, {"id", "chatId"}, {"id", "chatId"}) and _identifier(body["id"]) and _identifier(body["chatId"])
    if action is WebAction.MARK_READ:
        return _exact_object(body, {"success"}, {"success"}) and body["success"] is True
    return False
# endregion FUNC_validate_response_body


# region FUNC_classify_response [DOMAIN(10): Safety; CONCEPT(10): FailClosed; TECH(8): SemanticValidation]
## @purpose Fail closed on authentication loss, anti-bot protection, schema drift and ambiguous writes.
## @io WebAction, ResponseEnvelope -> ResponseClass
## @complexity 8
def classify_response(action: WebAction, response: ResponseEnvelope) -> ResponseClass:
    if response.status == 0 and action in WRITE_ACTIONS:
        return ResponseClass.AMBIGUOUS_WRITE
    if response.status == 429:
        return ResponseClass.RATE_LIMIT
    if response.status in {401, 403}:
        return ResponseClass.AUTH_REQUIRED if response.status == 401 else ResponseClass.PROTECTION
    redirect_marker = response.redirect_path.lower()
    if any(token in redirect_marker for token in ("/account/login", "/oauth/", "/login")):
        return ResponseClass.AUTH_REQUIRED
    # BUG_FIX_CONTEXT: Scanning every projected string for the words captcha/challenge caused
    # ordinary vacancy/chat text to hard-stop the robot. Semantic bodies and redirect paths retain
    # fail-closed protection detection without treating incidental vocabulary as a challenge.
    if _explicit_protection(response.body) or any(token in redirect_marker for token in ("/captcha", "/challenge", "cf-chl-")):
        return ResponseClass.PROTECTION
    if response.status < 200 or response.status >= 300:
        return ResponseClass.FAILURE
    # BUG_FIX_CONTEXT: Shallow root checks allowed malformed nested chat counters/messages to reach
    # coercing parsers and crash outside the WebClientError hard-stop path; every action is exact now.
    if not validate_response_body(action, response.body):
        return ResponseClass.AMBIGUOUS_WRITE if action in WRITE_ACTIONS else ResponseClass.CONTRACT_DRIFT
    return ResponseClass.SUCCESS
# endregion FUNC_classify_response
