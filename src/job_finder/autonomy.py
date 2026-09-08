# region MODULE_CONTRACT [DOMAIN(10): AutonomousJobSearch; CONCEPT(10): DeterministicPolicy, HardStop; TECH(9): Orchestration]
## @file autonomy.py
## @brief Deterministic autonomous cycle around a restricted AI and web client.
## @modulecontract
## @purpose Search, rank, apply and answer chats after one-time consent while enforcing limits and fail-closed safety in code.
## @scope One cycle; scheduling is owned by CLI daemon.
## @input Config, web adapter, LLM, local knowledge and Storage.
## @output RunReport with counts and non-secret reasons.
## @invariants AI never selects commands or policy; each write starts with a transactional SQLite reservation that rechecks opt-in, hard-stop, quota and cadence.
## @rationale
## Q: Why does the worker reserve immediately before calling the web client?
## A: A cycle may run for minutes; only a fresh serialized policy decision can honor disable/hard-stop changes made after the cycle began.
## @changes LAST_CHANGE: [v0.2.1 — Added transactional per-write policy gates and crash-recoverable chat outbox sends.]
## @modulemap
## CLASS 10[Non-secret cycle observability] => RunReport
## CLASS 10[Deterministic job and chat orchestrator] => AutonomousWorker
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: autonomous worker, opt-in, dry-run, application, recruiter chat, limits, dedupe, hard stop
# STRUCTURE: consent/safety -> search -> AI score -> preflight -> grounded text -> guarded write -> chats -> report

from dataclasses import asdict, dataclass, field
import json
import logging
import threading
from typing import Any

from .bridge_server import BridgeError
from .config import Config
from .knowledge import KnowledgeBase, render_chunks
from .llm import GeneratedText, LLMError, LLMProvider, validate_grounded_text
from .storage import Storage, WriteGuardError
from .web_client import HHWebClient, WebClientError


logger = logging.getLogger(__name__)


@dataclass
class RunReport:
    dry_run: bool
    autonomy_enabled: bool
    hard_stop: str | None = None
    vacancies_found: int = 0
    assessed: int = 0
    applications_sent: int = 0
    messages_sent: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# region CLASS_AutonomousWorker [DOMAIN(10): AutonomousJobSearch; CONCEPT(10): PolicyOwner; TECH(9): SequentialOrchestration]
## @purpose Execute the user-approved workflow without per-action confirmation while retaining deterministic safety ownership.
class AutonomousWorker:
    def __init__(self, config: Config, client: HHWebClient, llm: LLMProvider, storage: Storage, knowledge: KnowledgeBase) -> None:
        self.config = config
        self.client = client
        self.llm = llm
        self.storage = storage
        self.knowledge = knowledge
        self._write_lock = threading.Lock()

    def _knowledge_for(self, query: str, limit: int = 10) -> str:
        return render_chunks(self.knowledge.retrieve(query, limit=limit, max_chars=36_000))

    @staticmethod
    def _vacancy_query(vacancy: dict[str, Any]) -> str:
        employer = vacancy.get("employer") or {}
        employer_name = employer.get("name", "") if isinstance(employer, dict) else employer
        return "\n".join(str(value or "") for value in (vacancy.get("name"), vacancy.get("description"), employer_name))

    @staticmethod
    def _employer(vacancy: dict[str, Any]) -> str:
        employer = vacancy.get("employer") or ""
        return str(employer.get("name", "")) if isinstance(employer, dict) else str(employer)

    def _guard_denied(self, report: RunReport, exc: WriteGuardError) -> None:
        """policy rejection -> observable skip without weakening an existing hard stop."""
        report.skip(exc.reason)
        if exc.reason == "hard_stop":
            report.hard_stop = self.storage.hard_stop() or "hard_stop"
        logger.warning("[IMP:9][AutonomousWorker][WRITE_GUARD] Remote write denied: %s", exc.reason)

    def _hard_stop(self, report: RunReport, reason: str) -> None:
        self.storage.set_hard_stop(reason)
        report.hard_stop = reason
        report.warnings.append(reason)
        logger.error("[IMP:10][AutonomousWorker][HARD_STOP] %s", reason)

    def _transport_failure(self, report: RunReport, exc: Exception) -> bool:
        if isinstance(exc, BridgeError) and exc.outcome.hard_stop:
            self._hard_stop(report, f"browser_transport:{exc.outcome.value}")
            return True
        if isinstance(exc, WebClientError):
            self._hard_stop(report, "browser_transport:contract_drift")
            return True
        report.warnings.append(exc.__class__.__name__)
        return False

    def _validate_text(self, generated: GeneratedText, sources: str, vacancy: dict[str, Any], maximum: int) -> None:
        validate_grounded_text(generated, sources, max_length=maximum, minimum_confidence=self.config.llm.minimum_confidence, employer=self._employer(vacancy))

    def _mark_read_guarded(self, report: RunReport, chat_id: str, message_id: str) -> bool:
        """transactional write reservation -> mark-read request -> durable completion."""
        try:
            # BUG_FIX_CONTEXT: MARK_READ is also a remote write; calling it under a Python lock only
            # bypassed late disable/hard-stop and cross-process quota/interval ownership.
            reservation = self.storage.reserve_write(
                "mark_read",
                message_id,
                daily_limit=self.config.safety.daily_message_limit,
                minimum_interval_seconds=self.config.safety.minimum_write_interval_seconds,
            )
        except WriteGuardError as exc:
            self._guard_denied(report, exc)
            return False
        self.client.mark_read(chat_id, message_id)
        self.storage.finish_write(reservation, success=True)
        return True

    # region METHOD_run_cycle [DOMAIN(10): AutonomousJobSearch; CONCEPT(10): FailClosedCycle; TECH(9): PolicyPipeline]
    ## @purpose Complete one search/application/chat pass, reserving policy and idempotency state immediately before every remote write.
    ## @io dry_run bool|None -> RunReport
    ## @complexity 10
    def run_cycle(self, *, dry_run: bool | None = None) -> RunReport:
        enabled = self.storage.autonomy_enabled()
        effective_dry_run = (not enabled) if dry_run is None else (dry_run or not enabled)
        report = RunReport(effective_dry_run, enabled, self.storage.hard_stop())
        logger.info("[IMP:9][AutonomousWorker][POLICY] cycle dry_run=%s consent=%s", effective_dry_run, enabled)
        if report.hard_stop:
            report.skip("hard_stop")
            return report

        vacancies: dict[str, dict[str, Any]] = {}
        try:
            for query in self.config.search.queries:
                result = self.client.search_vacancies(text=query, area=self.config.search.area, period=self.config.search.days, per_page=self.config.search.per_query)
                for item in result.get("items", []):
                    vacancy_id = str(item.get("id") or "")
                    if vacancy_id:
                        vacancies.setdefault(vacancy_id, item)
        except (BridgeError, WebClientError) as exc:
            self._transport_failure(report, exc)
            return report
        report.vacancies_found = len(vacancies)

        for vacancy_id in list(vacancies)[: self.config.autonomy.max_vacancies_per_cycle]:
            if self.storage.vacancy_status(vacancy_id) in {"applied", "rejected", "ineligible"}:
                report.skip("vacancy_deduplicated")
                continue
            try:
                vacancy = self.client.get_vacancy(vacancy_id)
                query = self._vacancy_query(vacancy)
                knowledge = self._knowledge_for(query)
                input_hash = self.storage.digest(json.dumps(vacancy, ensure_ascii=False, sort_keys=True) + knowledge)
                decision = self.llm.assess_relevance(vacancy, knowledge)
                report.assessed += 1
                logger.info("[IMP:9][AutonomousWorker][RELEVANCE] structured decision accepted score=%d", decision.score)
                if decision.decision != "apply" or decision.score < self.config.autonomy.relevance_threshold or decision.confidence < self.config.llm.minimum_confidence:
                    self.storage.record_vacancy(vacancy_id, "rejected", score=decision.score, input_hash=input_hash, reason="policy_threshold")
                    report.skip("relevance_policy")
                    continue
                preflight = self.client.application_preflight(vacancy_id)
                if not preflight.allowed or not preflight.resume_hash:
                    self.storage.record_vacancy(vacancy_id, "ineligible", score=decision.score, input_hash=input_hash, reason=preflight.reason or "preflight")
                    report.skip(preflight.reason or "preflight")
                    continue
                rules = self.knowledge.cover_letter_rules()
                generated = self.llm.generate_cover_letter(vacancy, knowledge, rules)
                sources = json.dumps(vacancy, ensure_ascii=False) + "\n" + knowledge + "\n" + rules
                self._validate_text(generated, sources, vacancy, preflight.letter_max_length)
                if effective_dry_run:
                    self.storage.record_vacancy(vacancy_id, "dry_run_ready", score=decision.score, input_hash=input_hash, reason="dry_run", generated_text=generated.text)
                    self.storage.audit(9, "application_dry_run", target=vacancy_id, detail={"score": decision.score, "needs_attention": generated.needs_attention})
                    report.skip("dry_run_application")
                    continue
                with self._write_lock:
                    try:
                        # BUG_FIX_CONTEXT: Cycle-start consent and separate counter reads left a race where
                        # disable/hard-stop or a competing worker could arrive before this remote call.
                        reservation = self.storage.reserve_write(
                            "application",
                            vacancy_id,
                            daily_limit=self.config.safety.daily_application_limit,
                            minimum_interval_seconds=self.config.safety.minimum_write_interval_seconds,
                        )
                    except WriteGuardError as exc:
                        self._guard_denied(report, exc)
                        continue
                    receipt = self.client.apply(
                        vacancy_id,
                        preflight.resume_hash,
                        generated.text,
                        country_ids=preflight.country_ids,
                    )
                    self.storage.finish_write(reservation, success=True, detail={"receipt": receipt, "sent_text": generated.text})
                    self.storage.record_vacancy(vacancy_id, "applied", score=decision.score, input_hash=input_hash, generated_text=generated.text)
                    report.applications_sent += 1
                    logger.info("[IMP:10][AutonomousWorker][APPLICATION_SENT] transactional reservation and semantic receipt committed")
            except LLMError:
                report.skip("llm_validation")
            except (BridgeError, WebClientError) as exc:
                if self._transport_failure(report, exc):
                    return report

        try:
            chats = self.client.list_chats(unread_only=True)
        except (BridgeError, WebClientError) as exc:
            self._transport_failure(report, exc)
            return report
        for chat in chats:
            if int(chat.get("unread_count") or 0) <= 0:
                continue
            chat_id = str(chat["id"])
            try:
                data = self.client.chat_messages(chat_id)
                recruiter = next((item for item in reversed(data["messages"]) if not item.get("is_current_user") and item.get("text")), None)
                if not recruiter:
                    report.skip("no_recruiter_message")
                    continue
                message_id = str(recruiter["id"])
                if self.storage.message_processed(message_id):
                    with self._write_lock:
                        self._mark_read_guarded(report, chat_id, message_id)
                    report.skip("message_deduplicated")
                    continue
                existing_outbox = self.storage.chat_outbox(message_id)
                vacancy = self.client.get_vacancy(data["vacancy_id"]) if data.get("vacancy_id") else {}
                query = recruiter["text"] + "\n" + self._vacancy_query(vacancy)
                knowledge = self._knowledge_for(query, limit=8)
                sources = json.dumps(vacancy, ensure_ascii=False) + json.dumps(data["messages"], ensure_ascii=False) + knowledge
                input_hash = self.storage.digest(sources)
                if existing_outbox:
                    generated = GeneratedText(existing_outbox.response_text, 1.0, (), (), (), True)
                    input_hash = existing_outbox.input_hash
                else:
                    generated = self.llm.generate_chat_reply(vacancy, data["messages"][-self.config.autonomy.chat_history_limit :], knowledge)
                    self._validate_text(generated, sources, vacancy, 5000)
                if effective_dry_run:
                    self.storage.audit(9, "chat_reply_dry_run", target=message_id, detail={"needs_attention": generated.needs_attention, "unknowns": list(generated.unknowns)})
                    report.skip("dry_run_chat_reply")
                    continue
                if not data.get("write_allowed"):
                    report.skip("chat_write_forbidden")
                    continue
                with self._write_lock:
                    try:
                        outbox = self.storage.reserve_chat_send(
                            message_id,
                            chat_id,
                            generated.text,
                            input_hash,
                            daily_limit=self.config.safety.daily_message_limit,
                            minimum_interval_seconds=self.config.safety.minimum_write_interval_seconds,
                        )
                    except WriteGuardError as exc:
                        self._guard_denied(report, exc)
                        continue
                    receipt = self.client.send_chat_message(
                        chat_id,
                        outbox.response_text,
                        idempotency_key=outbox.idempotency_key,
                    )
                    self.storage.complete_chat_send(message_id, receipt["id"])
                    report.messages_sent += 1
                    logger.info("[IMP:10][AutonomousWorker][CHAT_SENT] stable idempotency key and semantic receipt committed atomically")
                    self._mark_read_guarded(report, chat_id, message_id)
            except LLMError:
                report.skip("llm_validation")
            except (BridgeError, WebClientError) as exc:
                if self._transport_failure(report, exc):
                    return report
        return report
    # endregion METHOD_run_cycle
# endregion CLASS_AutonomousWorker
