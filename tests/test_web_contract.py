import json
from pathlib import Path
import unittest

from job_finder.web_client import HHWebClient, ReadThrottler, WebClientError, parse_search_body, parse_vacancy_body
from job_finder.web_contract import ContractError, ResponseClass, ResponseEnvelope, WebAction, classify_response, new_command, response_from_dict


class ContractTest(unittest.TestCase):
    def test_allows_only_typed_exact_params(self) -> None:
        new_command(WebAction.GET_VACANCY, {"vacancy_id": "vacancy_1"})
        for bad in ({"vacancy_id": "1", "url": "https://evil.test"}, {"vacancy_id": "../1"}, {"vacancy_id": "1", "headers": {"Cookie": "x"}}):
            with self.assertRaises(ContractError):
                new_command(WebAction.GET_VACANCY, bad)

    def test_safety_response_classes(self) -> None:
        def response(status=200, body=None, path=""):
            return ResponseEnvelope("00000000-0000-4000-8000-000000000000", status, "application/json", body, path)
        self.assertEqual(classify_response(WebAction.LIST_CHATS, response(401, {})), ResponseClass.AUTH_REQUIRED)
        self.assertEqual(classify_response(WebAction.LIST_CHATS, response(403, {})), ResponseClass.PROTECTION)
        self.assertEqual(classify_response(WebAction.LIST_CHATS, response(429, {})), ResponseClass.RATE_LIMIT)
        self.assertEqual(classify_response(WebAction.LIST_CHATS, response(200, {"captcha": True})), ResponseClass.PROTECTION)
        self.assertEqual(classify_response(WebAction.GET_CHAT_DATA, response(200, "html")), ResponseClass.CONTRACT_DRIFT)
        self.assertEqual(classify_response(WebAction.APPLY, response(200, {"success": "ok"})), ResponseClass.AMBIGUOUS_WRITE)
        self.assertEqual(classify_response(WebAction.SEND_CHAT_MESSAGE, response(0, {"executor_error": True})), ResponseClass.AMBIGUOUS_WRITE)
        self.assertEqual(classify_response(WebAction.LIST_CHATS, response(200, {}, "/account/login")), ResponseClass.AUTH_REQUIRED)
        self.assertEqual(classify_response(WebAction.LIST_CHATS, response(200, {"items": []}, "/captcha/check")), ResponseClass.PROTECTION)
        self.assertEqual(classify_response(WebAction.LIST_CHATS, response(200, {"items": [{"id": "chat-a", "unreadCount": "1"}]})), ResponseClass.CONTRACT_DRIFT)
        malformed_chat = {"chat": {"messages": {"items": "bad"}, "currentParticipantId": "person-a", "resources": {"VACANCY": []}}, "chatStates": {"writeMessageState": {"allowed": True}}}
        self.assertEqual(classify_response(WebAction.GET_CHAT_DATA, response(200, malformed_chat)), ResponseClass.CONTRACT_DRIFT)

    def test_projected_protection_and_redirect_hard_stop_without_vocabulary_false_positive(self) -> None:
        command_id = "00000000-0000-4000-8000-000000000000"
        protected = ResponseEnvelope(command_id, 200, "application/json", {"protection": "challenge"})
        redirected = ResponseEnvelope(command_id, 200, "application/json", {"items": [], "found": 0}, "/captcha/check")
        ordinary = ResponseEnvelope(command_id, 200, "application/json", {"items": [{"id": "123", "name": "Captcha Challenge Engineer"}], "found": 1})
        self.assertEqual(classify_response(WebAction.SEARCH_VACANCIES, protected), ResponseClass.PROTECTION)
        self.assertEqual(classify_response(WebAction.SEARCH_VACANCIES, redirected), ResponseClass.PROTECTION)
        self.assertEqual(classify_response(WebAction.SEARCH_VACANCIES, ordinary), ResponseClass.SUCCESS)

    def test_sanitized_fixture_contains_metadata_only(self) -> None:
        path = Path(__file__).parent / "fixtures/hh_contract_sanitized.json"
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
        self.assertEqual(set(value["actions"]), {action.value for action in WebAction})
        self.assertNotIn("Authorization", raw)
        self.assertNotRegex(raw, r"\b\d{7,}\b")

    def test_backend_rejects_credentials_in_extension_result(self) -> None:
        with self.assertRaises(ContractError):
            response_from_dict({"command_id": "00000000-0000-4000-8000-000000000000", "status": 200, "content_type": "application/json", "body": {"csrf": "secret"}})

    def test_backend_rejects_raw_html_even_when_it_has_no_credential_marker(self) -> None:
        response = ResponseEnvelope("00000000-0000-4000-8000-000000000000", 200, "text/html", "<h1>AI Lead</h1>")
        self.assertEqual(classify_response(WebAction.GET_VACANCY, response), ResponseClass.CONTRACT_DRIFT)


class WebClientParsingTest(unittest.TestCase):
    def test_accepts_only_typed_browser_projections(self) -> None:
        search = {"items": [{"id": "123", "name": "AI Lead"}], "found": 1}
        vacancy = {"id": "123", "name": "AI Lead", "description": "LLM", "employer": {"name": "Company"}}
        self.assertEqual(parse_search_body(search)[0]["id"], "123")
        self.assertEqual(parse_vacancy_body(vacancy, "123")["name"], "AI Lead")
        with self.assertRaises(WebClientError):
            parse_search_body('<a href="/vacancy/123">AI Lead</a>')

    def test_preflight_rejects_test_and_selects_safe_resume(self) -> None:
        class Bridge:
            def __init__(self, test=False): self.test = test
            def enqueue(self, command):
                body = {
                    "responseStatus": {"alreadyApplied": False, "responseImpossible": False, "test": {"hasTests": self.test}, "shortVacancy": {"userTestPresent": False}, "unusedResumeIds": ["resume-a"], "resumes": {"resume-a": {"hash": "hash-a", "isIncomplete": False, "forbidden": None}}, "letterMaxLength": 1000},
                    "responsePopup": {"startedWithQuestion": False},
                    "countryIds": ["country-a"],
                }
                return ResponseEnvelope(command.command_id, 200, "application/json", body)
        self.assertFalse(HHWebClient(Bridge(True)).application_preflight("vacancy-a").allowed)
        result = HHWebClient(Bridge(False)).application_preflight("vacancy-a")
        self.assertTrue(result.allowed)
        self.assertEqual(result.resume_hash, "hash-a")
        self.assertEqual(result.country_ids, ("country-a",))

    def test_malformed_nested_web_data_becomes_web_client_error(self) -> None:
        class Bridge:
            def enqueue(self, command):
                return ResponseEnvelope(command.command_id, 200, "application/json", {"items": [{"id": "chat-a", "unreadCount": {"bad": True}}]})

        with self.assertRaises(WebClientError):
            HHWebClient(Bridge()).list_chats()

    def test_common_read_throttler_uses_exact_fake_clock_delay(self) -> None:
        class FakeTime:
            now = 100.0
            sleeps = []

            def clock(self): return self.now
            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.now += seconds

        class Bridge:
            def enqueue(self, command):
                body = {"id": command.params["vacancy_id"], "name": "AI Lead", "description": "LLM"}
                return ResponseEnvelope(command.command_id, 200, "application/json", body)

        fake = FakeTime()
        client = HHWebClient(Bridge(), read_throttler=ReadThrottler(2.5, clock=fake.clock, sleep=fake.sleep))
        client.get_vacancy("vacancy-a")
        fake.now += 1.0
        client.get_vacancy("vacancy-b")
        self.assertEqual(fake.sleeps, [1.5])


if __name__ == "__main__":
    unittest.main()
