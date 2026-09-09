from pathlib import Path
import json
import tempfile
import unittest

from job_finder.autonomy import AutonomousWorker, _vacancy_exclusion_reason
from job_finder.bridge_server import BridgeError
from job_finder.config import AutonomyConfig, BridgeConfig, Config, HHConfig, LLMConfig, ProfileConfig, SafetyConfig, SearchConfig
from job_finder.knowledge import KnowledgeBase
from job_finder.llm import GeneratedText, LLMError, RelevanceDecision, sanitize_ai_context, validate_grounded_text
from job_finder.storage import Storage
from job_finder.web_client import ApplicationPreflight, WebClientError
from job_finder.web_contract import ResponseClass


class FakeClient:
    def __init__(self, *, with_chat=True, search_error=None, before_apply=None):
        self.with_chat, self.search_error, self.before_apply = with_chat, search_error, before_apply
        self.applications, self.sent_messages, self.marked = [], [], []
        self.remote_messages = {}

    def search_vacancies(self, **_kwargs):
        if self.search_error: raise self.search_error
        return {"items": [{"id": "vacancy-a", "name": "AI Lead"}]}

    def get_vacancy(self, vacancy_id):
        return {"id": vacancy_id, "name": "AI Lead", "description": "Управление LLM и RAG", "employer": {"name": "Компания"}}

    def application_preflight(self, vacancy_id):
        return ApplicationPreflight(vacancy_id, True, None, "resume-hash", 2000)

    def apply(self, vacancy_id, resume_hash, letter, *, country_ids=()):
        if self.before_apply: self.before_apply()
        self.applications.append((vacancy_id, resume_hash, letter))
        return {"success": True, "topic_id": "topic-a", "chat_id": "chat-a"}

    def list_chats(self, **_kwargs):
        return [{"id": "chat-a", "unread_count": 1}] if self.with_chat else []

    def chat_messages(self, chat_id):
        return {"id": chat_id, "vacancy_id": "vacancy-a", "write_allowed": True, "messages": [{"id": "message-a", "text": "Расскажите об опыте LLM", "is_current_user": False}]}

    def send_chat_message(self, chat_id, text, *, idempotency_key):
        self.sent_messages.append((chat_id, text, idempotency_key))
        response_id = self.remote_messages.setdefault(idempotency_key, "response-a")
        return {"id": response_id, "chat_id": chat_id, "idempotency_key": idempotency_key}

    def mark_read(self, chat_id, message_id):
        self.marked.append((chat_id, message_id))


class FakeLLM:
    unsupported = False

    def assess_relevance(self, vacancy, knowledge):
        return RelevanceDecision(92, ("LLM",), (), "apply", 0.95)

    def generate_cover_letter(self, vacancy, knowledge, rules):
        return GeneratedText("Управляю ИТ-проектами более 10 лет, внедрял LLM и RAG.", 0.95, ("10 лет управляю ИТ-проектами", "внедрял LLM и RAG"), (), ("выдуманный факт",) if self.unsupported else (), True)

    def generate_chat_reply(self, vacancy, messages, knowledge):
        return GeneratedText("Более 10 лет управляю ИТ-проектами; внедрял LLM и RAG.", 0.95, ("10 лет управляю ИТ-проектами", "внедрял LLM и RAG"), (), (), True)


class ExclusionClient(FakeClient):
    def __init__(self, vacancy):
        super().__init__(with_chat=False)
        self.vacancy = vacancy
        self.preflight_calls = 0

    def search_vacancies(self, **_kwargs):
        return {"items": [{"id": self.vacancy["id"], "name": self.vacancy["name"]}]}

    def get_vacancy(self, vacancy_id):
        return dict(self.vacancy, id=vacancy_id)

    def application_preflight(self, vacancy_id):
        self.preflight_calls += 1
        return super().application_preflight(vacancy_id)


class CountingLLM(FakeLLM):
    def __init__(self):
        self.relevance_calls = 0
        self.cover_calls = 0

    def assess_relevance(self, vacancy, knowledge):
        self.relevance_calls += 1
        return super().assess_relevance(vacancy, knowledge)

    def generate_cover_letter(self, vacancy, knowledge, rules):
        self.cover_calls += 1
        return super().generate_cover_letter(vacancy, knowledge, rules)


class CountingKnowledge:
    def __init__(self):
        self.retrieve_calls = 0
        self.rules_calls = 0

    def retrieve(self, *_args, **_kwargs):
        self.retrieve_calls += 1
        return []

    def cover_letter_rules(self):
        self.rules_calls += 1
        return ""


def make_config(root, knowledge, rules, *, app_limit=5):
    return Config(
        root, ProfileConfig((knowledge,), rules), SearchConfig("1", 14, 10, ("AI Lead",)),
        SafetyConfig(app_limit, 20, 0, 0), HHConfig(), BridgeConfig(), LLMConfig(minimum_confidence=0.75),
        AutonomyConfig(relevance_threshold=75, max_vacancies_per_cycle=5, chat_history_limit=20, cycle_interval_seconds=60),
    )


class AutonomyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.knowledge = root / "knowledge.md"
        self.rules = root / "rules.md"
        self.knowledge.write_text("# Опыт\nБолее 10 лет управляю ИТ-проектами, внедрял LLM и RAG.", encoding="utf-8")
        self.rules.write_text("# Правила\nТолько подтверждённые факты.", encoding="utf-8")
        self.config = make_config(root, self.knowledge, self.rules)
        self.storage = Storage(root / "state.sqlite3")

    def tearDown(self): self.temp.cleanup()

    def worker(self, client=None, llm=None, config=None):
        cfg = config or self.config
        return AutonomousWorker(cfg, client or FakeClient(), llm or FakeLLM(), self.storage, KnowledgeBase(cfg.profile))

    def run_excluded(self, vacancy, reason):
        client = ExclusionClient(vacancy)
        llm = CountingLLM()
        knowledge = CountingKnowledge()
        self.storage.set_autonomy(True)
        report = AutonomousWorker(self.config, client, llm, self.storage, knowledge).run_cycle(dry_run=False)
        self.assertFalse(report.dry_run)
        self.assertEqual(report.skipped.get(reason), 1)
        self.assertEqual(report.assessed, 0)
        self.assertEqual(self.storage.vacancy_status(vacancy["id"]), "ineligible")
        self.assertEqual((knowledge.retrieve_calls, knowledge.rules_calls), (0, 0))
        self.assertEqual((llm.relevance_calls, llm.cover_calls), (0, 0))
        self.assertEqual(client.preflight_calls, 0)
        self.assertEqual(client.applications, [])
        return report

    def test_full_live_path_and_second_cycle_deduplicates(self):
        client = FakeClient()
        self.storage.set_autonomy(True)
        first = self.worker(client).run_cycle(dry_run=False)
        self.assertEqual((first.applications_sent, first.messages_sent), (1, 1))
        second = self.worker(client).run_cycle(dry_run=False)
        self.assertEqual((second.applications_sent, second.messages_sent), (0, 0))
        self.assertEqual(len(client.applications), 1)
        self.assertEqual(len(client.sent_messages), 1)

    def test_no_opt_in_forces_dry_run_even_when_live_requested(self):
        client = FakeClient()
        report = self.worker(client).run_cycle(dry_run=False)
        self.assertTrue(report.dry_run)
        self.assertEqual(client.applications, [])
        self.assertEqual(client.sent_messages, [])

    def test_employer_ecosystems_are_terminal_before_knowledge_and_ai(self):
        employers = ("СБЕРБАНК", "СБЁР-Тех", "SberTech", "ЯНДЕКС.Маркет", "Yandex Cloud")
        for index, employer in enumerate(employers):
            with self.subTest(employer=employer):
                self.run_excluded(
                    {"id": f"vacancy-employer-{index}", "name": "AI Lead", "description": "Управление LLM", "employer": {"name": employer}},
                    "excluded_employer",
                )

    def test_betting_markers_in_employer_title_or_description_are_terminal(self):
        vacancies = (
            {"id": "vacancy-betting-0", "name": "AI Lead", "description": "Управление продуктом", "employer": {"name": "FONBET"}},
            {"id": "vacancy-betting-1", "name": "Менеджер букмекерской платформы", "description": "Управление продуктом", "employer": {"name": "Компания"}},
            {"id": "vacancy-betting-2", "name": "AI Lead", "description": "Развитие betting-платформы", "employer": {"name": "Компания"}},
            {"id": "vacancy-betting-3", "name": "Product Lead", "description": "Развитие gambling и онлайн-казино", "employer": {"name": "Компания"}},
            {"id": "vacancy-betting-4", "name": "AI Lead", "description": "Управление продуктом", "employer": {"name": "MOSTBET"}},
        )
        for vacancy in vacancies:
            with self.subTest(vacancy_id=vacancy["id"]):
                self.run_excluded(vacancy, "excluded_betting")

    def test_exclusion_overwrites_stale_dry_run_letter_with_public_only_hash(self):
        vacancy = {"id": "vacancy-stale", "name": "AI Lead", "description": "Управление LLM", "employer": {"name": "Sber Tech"}}
        self.storage.record_vacancy(vacancy["id"], "dry_run_ready", score=99, input_hash="old-hash", reason="dry_run", generated_text="старое письмо")
        self.run_excluded(vacancy, "excluded_employer")
        with self.storage._connect() as conn:
            row = conn.execute(
                "SELECT status,score,input_hash,reason,generated_text FROM vacancy_decisions WHERE vacancy_id=?",
                (vacancy["id"],),
            ).fetchone()
        self.assertEqual(row["status"], "ineligible")
        self.assertIsNone(row["score"])
        self.assertEqual(row["reason"], "excluded_employer")
        self.assertIsNone(row["generated_text"])
        self.assertEqual(row["input_hash"], self.storage.digest(json.dumps(vacancy, ensure_ascii=False, sort_keys=True)))

    def test_general_bet_substring_and_financial_rates_are_not_excluded(self):
        ordinary = {
            "id": "vacancy-ordinary",
            "name": "Product Manager",
            "description": "A/B testing и процентные ставки по депозитам",
            "employer": {"name": "BetterMe"},
        }
        self.assertIsNone(_vacancy_exclusion_reason(ordinary, self.config.search))

    def test_transport_protection_activates_hard_stop(self):
        error = BridgeError("forbidden", outcome=ResponseClass.PROTECTION)
        report = self.worker(FakeClient(search_error=error)).run_cycle(dry_run=True)
        self.assertIsNotNone(report.hard_stop)
        self.assertIn("protection", self.storage.hard_stop())

    def test_malformed_web_contract_activates_hard_stop(self):
        report = self.worker(FakeClient(search_error=WebClientError("malformed unreadCount"))).run_cycle(dry_run=True)
        self.assertEqual(report.hard_stop, "browser_transport:contract_drift")
        self.assertEqual(self.storage.hard_stop(), "browser_transport:contract_drift")

    def test_hallucination_is_blocked_before_write(self):
        client, llm = FakeClient(with_chat=False), FakeLLM()
        llm.unsupported = True
        self.storage.set_autonomy(True)
        report = self.worker(client, llm).run_cycle(dry_run=False)
        self.assertEqual(client.applications, [])
        self.assertEqual(report.skipped.get("llm_validation"), 1)

    def test_daily_limit_is_owned_by_policy_not_ai(self):
        cfg = make_config(Path(self.temp.name), self.knowledge, self.rules, app_limit=1)
        self.storage.set_autonomy(True)
        self.storage.record_action("application", "old", success=True)
        client = FakeClient(with_chat=False)
        report = self.worker(client, config=cfg).run_cycle(dry_run=False)
        self.assertEqual(client.applications, [])
        self.assertEqual(report.skipped.get("application_daily_limit"), 1)

    def test_disable_during_running_cycle_blocks_not_started_write(self):
        class DisableAfterGeneration(FakeLLM):
            def generate_cover_letter(inner_self, vacancy, knowledge, rules):
                result = super().generate_cover_letter(vacancy, knowledge, rules)
                self.storage.set_autonomy(False)
                return result

        client = FakeClient(with_chat=False)
        self.storage.set_autonomy(True)
        report = self.worker(client, DisableAfterGeneration()).run_cycle(dry_run=False)
        self.assertEqual(client.applications, [])
        self.assertEqual(report.skipped.get("autonomy_disabled"), 1)

    def test_hard_stop_during_running_cycle_blocks_not_started_write(self):
        class StopAfterGeneration(FakeLLM):
            def generate_cover_letter(inner_self, vacancy, knowledge, rules):
                result = super().generate_cover_letter(vacancy, knowledge, rules)
                self.storage.set_hard_stop("manual_test_stop")
                return result

        client = FakeClient(with_chat=False)
        self.storage.set_autonomy(True)
        report = self.worker(client, StopAfterGeneration()).run_cycle(dry_run=False)
        self.assertEqual(client.applications, [])
        self.assertEqual(report.skipped.get("hard_stop"), 1)

    def test_chat_crash_after_send_reuses_persistent_idempotency_key(self):
        class CrashAfterSend(BaseException):
            pass

        class CrashOnceClient(FakeClient):
            crashed = False

            def send_chat_message(inner_self, chat_id, text, *, idempotency_key):
                receipt = super().send_chat_message(chat_id, text, idempotency_key=idempotency_key)
                if not inner_self.crashed:
                    inner_self.crashed = True
                    raise CrashAfterSend()
                return receipt

        client = CrashOnceClient()
        self.storage.set_autonomy(True)
        with self.assertRaises(CrashAfterSend):
            self.worker(client).run_cycle(dry_run=False)
        outbox = self.storage.chat_outbox("message-a")
        self.assertIsNotNone(outbox)
        self.assertEqual(outbox.state, "sending")

        retry = self.worker(client).run_cycle(dry_run=False)
        self.assertEqual(retry.messages_sent, 1)
        self.assertEqual(client.sent_messages[0][2], client.sent_messages[1][2])
        self.assertEqual(len(client.remote_messages), 1)
        self.assertTrue(self.storage.message_processed("message-a"))
        self.assertEqual(self.storage.successful_today("chat_message"), 1)

    def test_disable_after_chat_receipt_blocks_following_mark_read_write(self):
        class DisableAfterChatClient(FakeClient):
            def send_chat_message(inner_self, chat_id, text, *, idempotency_key):
                receipt = super().send_chat_message(chat_id, text, idempotency_key=idempotency_key)
                self.storage.set_autonomy(False)
                return receipt

        client = DisableAfterChatClient()
        self.storage.set_autonomy(True)
        report = self.worker(client).run_cycle(dry_run=False)
        self.assertEqual(report.messages_sent, 1)
        self.assertEqual(client.marked, [])
        self.assertGreaterEqual(report.skipped.get("autonomy_disabled", 0), 1)

    def test_sber_confidentiality_and_ai_context_boundary(self):
        generated = GeneratedText("Кейс в Сбер Университете", 0.9, (), (), (), True)
        with self.assertRaises(LLMError):
            validate_grounded_text(generated, "Сбер Университет", max_length=1000, minimum_confidence=0.7, employer="Сбер")
        safe = sanitize_ai_context({
            "id": "root-id",
            "chatId": "chat-a",
            "nested": {"vacancyId": "v", "messageId": "m", "participantId": "p", "resumeId": "r", "topicId": "t", "participantsIds": ["p"], "idempotencyKey": "k", "identity": "обычное слово"},
            "url": "https://hh.ru/vacancy/1",
            "headers": {"Cookie": "secret"},
            "description": "См. https://example.test",
        })
        self.assertNotIn("url", safe)
        self.assertNotIn("headers", safe)
        self.assertNotIn("https://", safe["description"])
        self.assertNotIn("id", safe)
        self.assertEqual(safe["nested"], {"identity": "обычное слово"})


if __name__ == "__main__":
    unittest.main()
