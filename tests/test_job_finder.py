from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from job_finder.config import Config, HHConfig, ProfileConfig, SafetyConfig, SearchConfig
from job_finder.hh_client import APIResponse, HHClient, HHError
from job_finder.keychain import MemorySecrets
from job_finder.knowledge import KnowledgeBase
from job_finder.matching import normalize_vacancy
from job_finder.mcp_server import handle_message
from job_finder.service import JobFinderService
from job_finder.storage import Storage


def make_config(root: Path, knowledge: Path, rules: Path) -> Config:
    return Config(
        root=root,
        profile=ProfileConfig((knowledge,), rules),
        search=SearchConfig("1", 14, 10, ("AI Product Manager",)),
        safety=SafetyConfig(10, 30, 0.5, 5.0, 15),
        hh=HHConfig("https://api.hh.ru", "job-finder-tests/0.1"),
    )


def vacancy(vacancy_id: str = "123") -> dict:
    return {
        "id": vacancy_id,
        "name": "Руководитель AI-проектов",
        "description": "Управление LLM-платформой, B2B, интеграции REST и Kafka",
        "employer": {"name": "Тест"},
        "area": {"name": "Москва"},
        "relations": [],
        "key_skills": [{"name": "Project Management"}],
        "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
    }


class FakeClient:
    def __init__(self) -> None:
        self.applications: list[tuple[str, str, str]] = []
        self.messages: list[tuple[str, str, str]] = []
        self.apply_error: HHError | None = None

    def auth_summary(self) -> dict:
        return {"access_token_present": True}

    def get_vacancy(self, vacancy_id: str) -> dict:
        return vacancy(vacancy_id)

    def apply(self, vacancy_id: str, resume_id: str, message: str) -> APIResponse:
        if self.apply_error:
            raise self.apply_error
        self.applications.append((vacancy_id, resume_id, message))
        return APIResponse(201, "", {"Location": "/negotiations/1"})

    def chat_messages(self, chat_id: str, *, limit: int = 20) -> dict:
        return {
            "id": chat_id,
            "vacancy_id": "123",
            "display": {"title": "Тест"},
            "chat_states": {"write_message_state": {"allowed": True}},
            "items": [],
        }

    def send_chat_message(self, chat_id: str, message: str, idempotency_key: str) -> APIResponse:
        self.messages.append((chat_id, message, idempotency_key))
        return APIResponse(201, {"id": "message-1"}, {})


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        knowledge = root / "knowledge.md"
        rules = root / "rules.md"
        knowledge.write_text(
            "# База\n## Общие данные\n10+ лет управляю ИТ-проектами.\n"
            "## AI\nВнедрил LLM и RAG, высвободил 2 FTE.\n"
            "## Границы\nНе обучал ML-модели.\n",
            encoding="utf-8",
        )
        rules.write_text("# Правила\nПисать 3-5 пунктов, нумерация 1/.", encoding="utf-8")
        self.config = make_config(root, knowledge, rules)
        self.client = FakeClient()
        self.storage = Storage(root / "state.sqlite3")
        self.service = JobFinderService(self.config, self.client, self.storage, KnowledgeBase(self.config.profile))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cover_context_uses_confirmed_knowledge(self) -> None:
        result = self.service.prepare_cover_letter("123")
        self.assertIn("высвободил 2 FTE", result["approved_candidate_context"])
        self.assertIn("ничего не отправляй", result["task"].lower())

    def test_application_is_two_step_and_confirmation_is_single_use(self) -> None:
        preview = self.service.prepare_application(vacancy_id="123", resume_id="resume", cover_letter="Письмо")
        with self.assertRaises(ValueError):
            self.service.submit_application(confirmation_id=preview["confirmation_id"], confirmation="да")
        self.assertEqual(self.client.applications, [])

        result = self.service.submit_application(confirmation_id=preview["confirmation_id"], confirmation="ОТПРАВИТЬ")
        self.assertEqual(result["status"], 201)
        self.assertEqual(len(self.client.applications), 1)
        with self.assertRaises(RuntimeError):
            self.service.submit_application(confirmation_id=preview["confirmation_id"], confirmation="ОТПРАВИТЬ")

    def test_hh_limit_activates_hard_stop(self) -> None:
        self.client.apply_error = HHError(
            "limit", status=403, codes=("negotiations", "limit_exceeded")
        )
        preview = self.service.prepare_application(vacancy_id="123", resume_id="resume", cover_letter="Письмо")
        with self.assertRaises(HHError):
            self.service.submit_application(confirmation_id=preview["confirmation_id"], confirmation="ОТПРАВИТЬ")
        self.assertIn("limit", self.storage.hard_stop() or "")

    def test_ambiguous_network_failure_activates_hard_stop(self) -> None:
        self.client.apply_error = HHError("network timeout")
        preview = self.service.prepare_application(vacancy_id="123", resume_id="resume", cover_letter="Письмо")
        with self.assertRaises(HHError):
            self.service.submit_application(confirmation_id=preview["confirmation_id"], confirmation="ОТПРАВИТЬ")
        self.assertIn("Неопределённый результат", self.storage.hard_stop() or "")

    def test_chat_message_is_two_step(self) -> None:
        preview = self.service.prepare_chat_message(chat_id="chat", message="Здравствуйте")
        self.assertTrue(preview["preview"]["marked_as_automated"])
        self.service.submit_chat_message(confirmation_id=preview["confirmation_id"], confirmation="ОТПРАВИТЬ")
        self.assertEqual(self.client.messages[0][0:2], ("chat", "Здравствуйте"))


class MatchingTest(unittest.TestCase):
    def test_relevance_is_explainable(self) -> None:
        result = normalize_vacancy(vacancy(), include_description=True)
        self.assertGreaterEqual(result["match"]["score"], 60)
        self.assertIn("AI/GenAI", result["match"]["reasons"])
        self.assertNotIn("<", result["description"])


class CaptureClient(HHClient):
    def __init__(self, config: Config) -> None:
        super().__init__(config, MemorySecrets())
        self.captured: dict = {}

    def request(self, method: str, path: str, **kwargs):  # type: ignore[override]
        self.captured = {"method": method, "path": path, **kwargs}
        return APIResponse(201, {"id": "1"}, {})


class ClientTest(unittest.TestCase):
    def test_chat_marks_ai_and_uses_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            knowledge = root / "k.md"
            rules = root / "r.md"
            knowledge.write_text("# k", encoding="utf-8")
            rules.write_text("# r", encoding="utf-8")
            client = CaptureClient(make_config(root, knowledge, rules))
            client.send_chat_message("chat", "Текст", "00000000-0000-4000-8000-000000000000")
            self.assertEqual(client.captured["path"], "/common/chats/chat/messages")
            self.assertTrue(client.captured["json_body"]["is_automated"])
            self.assertEqual(client.captured["json_body"]["idempotency_key"], "00000000-0000-4000-8000-000000000000")


class MCPTest(unittest.TestCase):
    class FakeService:
        def status(self):
            return {"ok": True}

    def test_initialize_and_status_tool(self) -> None:
        initialized = handle_message(
            self.FakeService(),
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        )
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "amir-job-finder")
        called = handle_message(
            self.FakeService(),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
        )
        self.assertFalse(called["result"]["isError"])
        self.assertIn('"ok": true', called["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
