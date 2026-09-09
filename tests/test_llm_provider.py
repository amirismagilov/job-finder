from __future__ import annotations

# region MODULE_CONTRACT [DOMAIN(9): GroundedGenerationTests; CONCEPT(10): OfflineHTTPContract; TECH(9): unittest, Mock]
## @file test_llm_provider.py
## @brief Offline contract tests for provider selection, Codex CLI schema routing and OpenAI-compatible Chat Completions.
## @modulecontract
## @purpose Verify provider validation, CLI schema routing, request isolation, bearer authorization and structured response parsing without live traffic.
## @scope LLMConfig TOML loading and LLMProvider CLI/HTTP request-response boundaries.
## @input Synthetic configuration, context, credentials, CLI transport and Chat Completions responses.
## @output Deterministic assertions over parsed values and intercepted HTTP requests.
## @invariants urlopen is always mocked; credentials are synthetic; no browser, OpenAI or hh.ru request is performed.
## @changes LAST_CHANGE: [v0.3.0 — Added Codex CLI provider selection/schema routing while retaining HTTP regression coverage.]
## @modulemap
## CLASS 10[Validated optional reasoning configuration] => LLMConfigTest
## CLASS 10[Codex CLI task-schema routing] => LLMProviderCLITransportTest
## CLASS 10[Intercepted Chat Completions contract] => LLMProviderHTTPTest
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: offline LLM, provider selection, Codex CLI schema routing, reasoning effort, HTTP temperature compatibility, bearer
# STRUCTURE: synthetic TOML/CLI/HTTP response -> validated config/routed request -> contract assertions

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from job_finder.codex_cli import CodexCLIError
from job_finder.config import LLMConfig, load_config
from job_finder.keychain import MemorySecrets
from job_finder.llm import GENERATED_TEXT_SCHEMA, LLMError, LLMProvider, RELEVANCE_SCHEMA


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.read_limit: int | None = None

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.raw


def chat_response(content: dict[str, object]) -> FakeHTTPResponse:
    return FakeHTTPResponse({"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})


def write_config(root: Path, llm_lines: str) -> Path:
    config_path = root / "config.toml"
    config_path.write_text(
        '[profile]\nknowledge_paths=[]\ncover_letter_rules_path=""\n'
        '[search]\nqueries=["AI"]\n'
        f"[llm]\n{llm_lines}",
        encoding="utf-8",
    )
    return config_path


class LLMConfigTest(unittest.TestCase):
    def test_openai_reasoning_configuration_is_loaded_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = write_config(
                Path(name),
                'endpoint="https://api.openai.com/v1/chat/completions"\n'
                'model="gpt-5.6-sol"\nreasoning_effort="xhigh"\ntimeout_seconds=999\n',
            )

            config = load_config(path).llm

        self.assertEqual(config.endpoint, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(config.model, "gpt-5.6-sol")
        self.assertEqual(config.reasoning_effort, "xhigh")
        self.assertEqual(config.timeout_seconds, 180.0)
        self.assertEqual(config.api_key_account, "llm_api_key")

    def test_unknown_reasoning_effort_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = write_config(Path(name), 'reasoning_effort="unbounded"\n')
            with self.assertRaisesRegex(RuntimeError, "Некорректный reasoning_effort"):
                load_config(path)

    def test_remote_plaintext_endpoint_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = write_config(Path(name), 'endpoint="http://api.openai.com/v1/chat/completions"\n')
            with self.assertRaisesRegex(RuntimeError, "обязан использовать HTTPS"):
                load_config(path)

    def test_omitted_reasoning_preserves_provider_neutral_default(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = load_config(write_config(Path(name), "")).llm

        self.assertIsNone(config.reasoning_effort)
        self.assertEqual(config.endpoint, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(config.model, "local-model")
        self.assertEqual(config.provider, "http")
        self.assertIsNone(config.codex_executable)

    def test_codex_cli_configuration_requires_validated_executable_model_and_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            executable = Path(name) / "codex"
            executable.write_text("synthetic executable; never run", encoding="utf-8")
            executable.chmod(0o700)
            path = write_config(
                Path(name),
                'provider="codex_cli"\n'
                f'codex_executable="{executable}"\n'
                'model="gpt-5.6-sol"\nreasoning_effort="xhigh"\ntimeout_seconds=300\n',
            )

            config = load_config(path).llm

        self.assertEqual(config.provider, "codex_cli")
        self.assertEqual(config.codex_executable, executable)
        self.assertEqual(config.model, "gpt-5.6-sol")
        self.assertEqual(config.reasoning_effort, "xhigh")
        self.assertEqual(config.timeout_seconds, 300.0)
        self.assertEqual(config.endpoint, "")

    def test_codex_cli_rejects_unknown_provider_relative_executable_and_missing_reasoning(self) -> None:
        scenarios = (
            ('provider="dynamic"\n', "provider должен быть"),
            ('provider="codex_cli"\ncodex_executable="codex"\nreasoning_effort="xhigh"\n', "абсолютным путём"),
            ('provider="codex_cli"\ncodex_executable="/missing/codex"\nreasoning_effort="xhigh"\n', "абсолютным путём"),
            ('provider="codex_cli"\ncodex_executable="/missing/codex"\n', "требуется reasoning_effort"),
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for index, (llm_lines, message) in enumerate(scenarios):
                case_root = root / str(index)
                case_root.mkdir()
                with self.subTest(index=index), self.assertRaisesRegex(RuntimeError, message):
                    load_config(write_config(case_root, llm_lines))


class RejectingSecrets:
    def get(self, _account: str) -> str | None:
        raise AssertionError("codex_cli must not read an HTTP API key")


class FakeCodexTransport:
    def __init__(self, responses: list[dict[str, object]] | None = None, error: CodexCLIError | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def complete(self, prompt: str, schema: object) -> dict[str, object]:
        self.calls.append((prompt, schema))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class LLMProviderCLITransportTest(unittest.TestCase):
    def test_cli_provider_routes_sanitized_prompt_through_task_specific_closed_schemas(self) -> None:
        transport = FakeCodexTransport([
            {
                "score": 88,
                "reasons": ["опыт подтверждён"],
                "gaps": [],
                "decision": "apply",
                "confidence": 0.9,
                "unknowns": [],
            },
            {
                "text": "Управляю AI-проектами.",
                "confidence": 0.91,
                "used_facts": ["управление AI-проектами"],
                "unknowns": [],
                "unsupported_claims": [],
                "grounded": True,
                "needs_attention": False,
            },
        ])
        config = LLMConfig(
            model="gpt-5.6-sol",
            timeout_seconds=300,
            reasoning_effort="xhigh",
            provider="codex_cli",
            codex_executable=Path("/synthetic/not-executed/codex"),
        )
        provider = LLMProvider(config, RejectingSecrets(), codex_transport=transport)

        decision = provider.assess_relevance(
            {"id": "PRIVATE_IDENTIFIER", "url": "https://private.example/vacancy", "description": "Опыт AI"},
            "Управление AI-проектами",
        )
        generated = provider.generate_cover_letter({"name": "AI Lead"}, "Управление AI-проектами", "Кратко")

        self.assertEqual(decision.score, 88)
        self.assertEqual(generated.text, "Управляю AI-проектами.")
        relevance_prompt, relevance_schema = transport.calls[0]
        generated_prompt, generated_schema = transport.calls[1]
        self.assertNotIn("PRIVATE_IDENTIFIER", relevance_prompt)
        self.assertNotIn("https://private.example", relevance_prompt)
        self.assertIn("недоверенными данными", relevance_prompt)
        self.assertEqual(relevance_schema, RELEVANCE_SCHEMA)
        self.assertEqual(generated_schema, GENERATED_TEXT_SCHEMA)
        self.assertIs(relevance_schema["additionalProperties"], False)
        self.assertIs(generated_schema["additionalProperties"], False)
        self.assertNotEqual(relevance_schema, generated_schema)
        self.assertIn("сопроводительное", generated_prompt)

    def test_cli_transport_error_becomes_safe_llm_error(self) -> None:
        transport = FakeCodexTransport(error=CodexCLIError("Codex CLI завершился с ошибкой"))
        provider = LLMProvider(
            LLMConfig(provider="codex_cli", model="gpt-5.6-sol", reasoning_effort="xhigh"),
            RejectingSecrets(),
            codex_transport=transport,
        )
        with self.assertRaisesRegex(LLMError, "Codex CLI завершился с ошибкой"):
            provider.assess_relevance({"name": "AI"}, "AI")


class LLMProviderHTTPTest(unittest.TestCase):
    def test_reasoning_request_uses_bearer_without_temperature_and_parses_json(self) -> None:
        secrets = MemorySecrets()
        secrets.set("llm_api_key", "synthetic-test-key")
        provider = LLMProvider(
            LLMConfig(
                endpoint="https://api.openai.com/v1/chat/completions",
                model="gpt-5.6-sol",
                timeout_seconds=180,
                reasoning_effort="xhigh",
            ),
            secrets,
        )
        response = chat_response({
            "score": 91,
            "reasons": ["подтверждённый опыт"],
            "gaps": ["нет данных о бюджете"],
            "decision": "apply",
            "confidence": 0.92,
            "unknowns": ["размер команды"],
        })
        mocked_urlopen = Mock(return_value=response)

        with patch("job_finder.llm.urlopen", mocked_urlopen):
            decision = provider.assess_relevance(
                {"id": "synthetic-id", "name": "AI Lead", "description": "Управление AI-проектами"},
                "Подтверждённый опыт управления AI-проектами",
            )

        request = mocked_urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        user_context = json.loads(body["messages"][1]["content"])
        self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-test-key")
        self.assertEqual(mocked_urlopen.call_args.kwargs["timeout"], 180)
        self.assertEqual(body["model"], "gpt-5.6-sol")
        self.assertEqual(body["reasoning_effort"], "xhigh")
        self.assertNotIn("temperature", body)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("id", user_context["vacancy"])
        self.assertEqual(decision.score, 91)
        self.assertEqual(decision.decision, "apply")
        self.assertEqual(decision.reasons, ("подтверждённый опыт",))
        self.assertEqual(decision.unknowns, ("размер команды",))
        self.assertEqual(response.read_limit, 1_000_001)

    def test_local_provider_without_reasoning_keeps_temperature_and_omits_bearer(self) -> None:
        provider = LLMProvider(LLMConfig(), MemorySecrets())
        response = chat_response({
            "score": 40,
            "reasons": ["частичное совпадение"],
            "gaps": [],
            "decision": "skip",
            "confidence": 0.8,
            "unknowns": [],
        })
        mocked_urlopen = Mock(return_value=response)

        with patch("job_finder.llm.urlopen", mocked_urlopen):
            provider.assess_relevance({"name": "Локальная проверка"}, "Синтетические данные")

        request = mocked_urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["temperature"], 0.1)
        self.assertNotIn("reasoning_effort", body)
        self.assertIsNone(request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
