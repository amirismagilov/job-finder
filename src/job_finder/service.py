from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
import uuid

from .config import Config
from .hh_client import HHClient, HHError
from .knowledge import KnowledgeBase, render_chunks
from .matching import normalize_vacancy
from .storage import Storage
from .text import compact, plain_text


CONFIRM_PHRASE = "ОТПРАВИТЬ"


class JobFinderService:
    def __init__(self, config: Config, client: HHClient, storage: Storage, knowledge: KnowledgeBase) -> None:
        self.config = config
        self.client = client
        self.storage = storage
        self.knowledge = knowledge

    def status(self) -> dict[str, Any]:
        return {
            "mode": "local-stdio-mcp",
            "official_api_only": True,
            "browser_automation": False,
            "writes_require_two_steps": True,
            "auth": self.client.auth_summary(),
            "knowledge": self.knowledge.health(),
            "safety": {
                "hard_stop": self.storage.hard_stop(),
                "applications_sent_today": self.storage.successful_today("application"),
                "application_daily_limit": self.config.safety.daily_application_limit,
                "messages_sent_today": self.storage.successful_today("chat_message"),
                "message_daily_limit": self.config.safety.daily_message_limit,
                "minimum_write_interval_seconds": self.config.safety.minimum_write_interval_seconds,
            },
        }

    def discover_vacancies(
        self,
        *,
        queries: list[str] | None = None,
        area: str | None = None,
        days: int | None = None,
        per_query: int | None = None,
        only_with_salary: bool = False,
        limit: int = 30,
    ) -> dict[str, Any]:
        selected_queries = [q.strip() for q in (queries or list(self.config.search.queries)) if q.strip()][:8]
        if not selected_queries:
            raise ValueError("Нужен хотя бы один поисковый запрос")
        effective_area = area or self.config.search.area
        effective_days = max(1, min(days or self.config.search.days, 30))
        effective_per_query = max(1, min(per_query or self.config.search.per_query, 20))
        limit = max(1, min(limit, 100))
        vacancies: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []
        totals: dict[str, int] = {}
        for query in selected_queries:
            try:
                response = self.client.search_vacancies(
                    text=query,
                    area=effective_area,
                    period=effective_days,
                    page=0,
                    per_page=effective_per_query,
                    only_with_salary=only_with_salary,
                )
            except HHError as exc:
                errors.append({"query": query, "error": str(exc)})
                if exc.requires_hard_stop:
                    self.storage.set_hard_stop(str(exc))
                    break
                continue
            totals[query] = int(response.get("found") or 0)
            for raw in response.get("items") or []:
                item = normalize_vacancy(raw)
                vacancy_id = item["id"]
                previous = vacancies.get(vacancy_id)
                if previous is None or item["match"]["score"] > previous["match"]["score"]:
                    item["matched_search_queries"] = [query]
                    vacancies[vacancy_id] = item
                elif query not in previous["matched_search_queries"]:
                    previous["matched_search_queries"].append(query)
        ranked = sorted(
            vacancies.values(),
            key=lambda item: (item["match"]["score"], item.get("published_at") or ""),
            reverse=True,
        )[:limit]
        return {
            "search": {"queries": selected_queries, "area": effective_area, "days": effective_days, "totals": totals},
            "found_unique": len(vacancies),
            "returned": len(ranked),
            "vacancies": ranked,
            "warnings": errors,
            "ranking_note": "Скоринг эвристический: перед откликом всегда прочитать полное описание и проверить пробелы.",
        }

    def get_vacancy(self, vacancy_id: str) -> dict[str, Any]:
        return normalize_vacancy(self.client.get_vacancy(vacancy_id), include_description=True)

    def list_my_resumes(self) -> dict[str, Any]:
        response = self.client.my_resumes()
        items = []
        for resume in response.get("items") or []:
            items.append(
                {
                    "id": resume.get("id"),
                    "title": resume.get("title"),
                    "status": (resume.get("status") or {}).get("name"),
                    "updated_at": resume.get("updated_at"),
                    "alternate_url": resume.get("alternate_url"),
                }
            )
        return {"items": items, "found": response.get("found", len(items))}

    def prepare_cover_letter(self, vacancy_id: str) -> dict[str, Any]:
        vacancy = self.get_vacancy(vacancy_id)
        query = "\n".join(
            str(vacancy.get(key) or "")
            for key in ("name", "employer", "description", "experience", "key_skills")
        )
        chunks = self.knowledge.retrieve(query, limit=10, max_chars=36000)
        return {
            "task": (
                "Сгенерируй готовое сопроводительное письмо на русском языке. Строго следуй памятке и только "
                "подтверждённым фактам ниже. Выбери 3–5 главных требований вакансии, не выдумывай опыт и метрики. "
                "Проверь специальные правила конфиденциальности для работодателя. Верни пользователю сначала письмо, "
                "затем одной строкой перечисли честные пробелы, если они существенны. Ничего не отправляй автоматически."
            ),
            "vacancy": vacancy,
            "approved_candidate_context": render_chunks(chunks),
            "cover_letter_rules": self.knowledge.cover_letter_rules(),
        }

    def list_chats(self, *, unread_only: bool = True, page: int = 0, per_page: int = 20) -> dict[str, Any]:
        response = self.client.list_chats(
            unread_only=unread_only,
            page=max(0, min(page, 50)),
            per_page=max(1, min(per_page, 20)),
        )
        items = []
        for chat in response.get("items") or []:
            last = chat.get("last_message") or {}
            payload = last.get("payload") or {}
            items.append(
                {
                    "id": chat.get("id"),
                    "title": (chat.get("display") or {}).get("title"),
                    "type": chat.get("type"),
                    "vacancy_id": chat.get("vacancy_id"),
                    "unread_message_count": chat.get("unread_message_count"),
                    "blocked": chat.get("block_reason"),
                    "last_message": {
                        "id": last.get("id"),
                        "created_at": last.get("creation_time"),
                        "sender": (last.get("sender_display_info") or {}).get("name"),
                        "sender_role": (last.get("sender_display_info") or {}).get("role"),
                        "text": compact(payload.get("text"), 1000),
                    },
                }
            )
        return {"items": items, "page": response.get("page"), "pages": response.get("pages"), "found": response.get("found")}

    def get_chat_messages(self, chat_id: str, *, limit: int = 20) -> dict[str, Any]:
        response = self.client.chat_messages(chat_id, limit=max(1, min(limit, 50)))
        messages = []
        for message in response.get("items") or []:
            sender = message.get("sender_display_info") or {}
            payload = message.get("payload") or {}
            messages.append(
                {
                    "id": message.get("id"),
                    "created_at": message.get("creation_time"),
                    "sender": sender.get("name"),
                    "sender_role": sender.get("role"),
                    "is_current_participant": sender.get("is_current_participant"),
                    "text": plain_text(payload.get("text")),
                }
            )
        state = response.get("chat_states") or {}
        return {
            "chat_id": response.get("id") or chat_id,
            "title": (response.get("display") or {}).get("title"),
            "vacancy_id": response.get("vacancy_id"),
            "write_state": state.get("write_message_state"),
            "messages": messages,
        }

    def prepare_chat_answer(self, *, question: str | None = None, chat_id: str | None = None) -> dict[str, Any]:
        if not question and not chat_id:
            raise ValueError("Передайте question или chat_id")
        chat: dict[str, Any] | None = None
        vacancy: dict[str, Any] | None = None
        effective_question = plain_text(question)
        if chat_id:
            chat = self.get_chat_messages(chat_id, limit=30)
            if not effective_question:
                for message in reversed(chat["messages"]):
                    if not message.get("is_current_participant") and message.get("text"):
                        effective_question = message["text"]
                        break
            if chat.get("vacancy_id"):
                vacancy = self.get_vacancy(str(chat["vacancy_id"]))
        context_query = "\n".join(
            [effective_question, str((vacancy or {}).get("name") or ""), str((vacancy or {}).get("description") or "")]
        )
        chunks = self.knowledge.retrieve(context_query, limit=8, max_chars=32000)
        return {
            "task": (
                "Подготовь готовый ответ рекрутеру на русском языке. Структура: факт → механика → результат → вывод. "
                "Используй только подтверждённые факты, честно обозначай границы, не приписывай технические решения. "
                "Соблюдай «ёлочки» и специальные правила конфиденциальности. Дай сначала готовый текст; не отправляй его."
            ),
            "question": effective_question,
            "chat": chat,
            "vacancy": vacancy,
            "approved_candidate_context": render_chunks(chunks),
        }

    def _assert_writes_allowed(self, kind: str) -> None:
        hard_stop = self.storage.hard_stop()
        if hard_stop:
            raise RuntimeError(f"Отправка заблокирована safety-stop: {hard_stop}. Снимите блокировку вручную через CLI после проверки hh.ru.")
        if kind == "application":
            count = self.storage.successful_today(kind)
            limit = self.config.safety.daily_application_limit
        else:
            count = self.storage.successful_today(kind)
            limit = self.config.safety.daily_message_limit
        if count >= limit:
            raise RuntimeError(f"Достигнут локальный дневной лимит {limit} для действия {kind}")
        last = self.storage.last_action_time(("application", "chat_message"))
        if last:
            elapsed = (datetime.now(UTC) - last).total_seconds()
            remaining = self.config.safety.minimum_write_interval_seconds - elapsed
            if remaining > 0:
                raise RuntimeError(f"До следующей отправки нужно подождать ещё {remaining:.1f} с")

    def prepare_application(self, *, vacancy_id: str, resume_id: str, cover_letter: str) -> dict[str, Any]:
        cover_letter = cover_letter.strip()
        if not cover_letter:
            raise ValueError("Сопроводительное письмо не может быть пустым")
        if len(cover_letter) > 10000:
            raise ValueError("Сопроводительное письмо превышает безопасный лимит 10 000 символов")
        vacancy = self.get_vacancy(vacancy_id)
        target = f"{vacancy_id}:{resume_id}"
        if "got_response" in (vacancy.get("relations") or []) or self.storage.was_sent("application", target):
            raise RuntimeError("На эту вакансию этим резюме уже был отправлен отклик")
        action_id = self.storage.create_pending(
            "application",
            target,
            {"vacancy_id": vacancy_id, "resume_id": resume_id, "message": cover_letter},
            self.config.safety.confirmation_ttl_minutes,
        )
        return {
            "confirmation_id": action_id,
            "expires_in_minutes": self.config.safety.confirmation_ttl_minutes,
            "preview": {"vacancy": vacancy, "resume_id": resume_id, "cover_letter": cover_letter},
            "next_step": f"Только после явного согласия пользователя вызовите submit_application с confirmation='{CONFIRM_PHRASE}'.",
        }

    def submit_application(self, *, confirmation_id: str, confirmation: str) -> dict[str, Any]:
        if confirmation != CONFIRM_PHRASE:
            raise ValueError(f"Для отправки требуется точная фраза {CONFIRM_PHRASE!r}")
        self._assert_writes_allowed("application")
        pending = self.storage.claim_pending(confirmation_id, "application")
        payload = pending["payload"]
        try:
            response = self.client.apply(payload["vacancy_id"], payload["resume_id"], payload["message"])
            result = {
                "status": response.status,
                "location": response.headers.get("Location"),
                "external_application_required": response.status == 303,
            }
            self.storage.finish_pending(confirmation_id, success=True, result=result)
            return result
        except HHError as exc:
            result = {"error": str(exc), "codes": list(exc.codes), "status": exc.status}
            self.storage.finish_pending(confirmation_id, success=False, result=result)
            if exc.requires_hard_stop or exc.status is None:
                reason = str(exc)
                if exc.status is None:
                    reason = "Неопределённый результат write-запроса; проверьте отклики на hh.ru перед продолжением: " + reason
                self.storage.set_hard_stop(reason)
            raise
        except Exception as exc:
            self.storage.finish_pending(confirmation_id, success=False, result={"error": exc.__class__.__name__})
            raise

    def prepare_chat_message(self, *, chat_id: str, message: str) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise ValueError("Сообщение не может быть пустым")
        if len(message) > 20000:
            raise ValueError("Сообщение превышает лимит API 20 000 символов")
        chat = self.get_chat_messages(chat_id, limit=10)
        state = chat.get("write_state") or {}
        if state.get("allowed") is False:
            raise RuntimeError(f"hh.ru запретил отправку в этот чат: {state.get('reason') or 'причина не указана'}")
        action_id = self.storage.create_pending(
            "chat_message",
            chat_id,
            {"chat_id": chat_id, "message": message, "idempotency_key": str(uuid.uuid4())},
            self.config.safety.confirmation_ttl_minutes,
        )
        return {
            "confirmation_id": action_id,
            "expires_in_minutes": self.config.safety.confirmation_ttl_minutes,
            "preview": {"chat": chat, "message": message, "marked_as_automated": True},
            "next_step": f"Только после явного согласия пользователя вызовите submit_chat_message с confirmation='{CONFIRM_PHRASE}'.",
        }

    def submit_chat_message(self, *, confirmation_id: str, confirmation: str) -> dict[str, Any]:
        if confirmation != CONFIRM_PHRASE:
            raise ValueError(f"Для отправки требуется точная фраза {CONFIRM_PHRASE!r}")
        self._assert_writes_allowed("chat_message")
        pending = self.storage.claim_pending(confirmation_id, "chat_message")
        payload = pending["payload"]
        try:
            response = self.client.send_chat_message(payload["chat_id"], payload["message"], payload["idempotency_key"])
            result = {"status": response.status, "message_id": response.data.get("id") if isinstance(response.data, dict) else None}
            self.storage.finish_pending(confirmation_id, success=True, result=result)
            return result
        except HHError as exc:
            result = {"error": str(exc), "codes": list(exc.codes), "status": exc.status}
            self.storage.finish_pending(confirmation_id, success=False, result=result)
            if exc.requires_hard_stop or exc.status is None:
                reason = str(exc)
                if exc.status is None:
                    reason = "Неопределённый результат write-запроса; проверьте чат на hh.ru перед продолжением: " + reason
                self.storage.set_hard_stop(reason)
            raise
        except Exception as exc:
            self.storage.finish_pending(confirmation_id, success=False, result={"error": exc.__class__.__name__})
            raise
