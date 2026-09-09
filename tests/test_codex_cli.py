from __future__ import annotations

# region MODULE_CONTRACT [DOMAIN(10): AITransportTests; CONCEPT(10): SubprocessIsolation; TECH(9): unittest, FakeRunner]
## @file test_codex_cli.py
## @brief Offline security-contract tests for the ephemeral Codex CLI transport.
## @modulecontract
## @purpose Prove fixed argv, stdin-only prompts, private temporary cwd, bounded output and non-sensitive failure behavior without starting Codex.
## @scope CodexCLITransport process construction, file lifecycle, schema validation and failure handling.
## @input Synthetic executable, prompt, schema, runner results and errors.
## @output Deterministic assertions over the intercepted subprocess boundary.
## @invariants The runner is always fake; no Codex, browser, OpenAI or hh.ru process/request is started; all credentials and outputs are synthetic.
## @changes LAST_CHANGE: [v0.3.0 — Added complete offline coverage for isolated Codex CLI execution.]
## @modulemap
## CLASS 10[Captured subprocess contract and synthetic result writer] => FakeRunner
## CLASS 10[Codex CLI isolation and failure tests] => CodexCLITransportTest
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: fake Codex CLI runner, fixed argv, stdin prompt, private cwd, cleanup, timeout, bounded result, safe error
# STRUCTURE: synthetic executable + fake runner -> CodexCLITransport -> argv/files/result/error assertions

import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from job_finder.codex_cli import CodexCLIError, CodexCLITransport, DISABLED_TOOL_FEATURES, MAX_RESULT_BYTES
from job_finder.config import LLMConfig


RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value"],
    "properties": {"value": {"type": "string", "minLength": 1, "maxLength": 20}},
}


def make_executable(root: Path) -> Path:
    executable = root / "codex"
    executable.write_text("synthetic executable; never run", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def cli_config(executable: Path) -> LLMConfig:
    return LLMConfig(
        model="gpt-5.6-sol",
        timeout_seconds=300,
        reasoning_effort="xhigh",
        provider="codex_cli",
        codex_executable=executable,
    )


class FakeRunner:
    def __init__(self, *, raw_result: bytes | None = b'{"value":"ok"}', returncode: int = 0, error: Exception | None = None) -> None:
        self.raw_result = raw_result
        self.returncode = returncode
        self.error = error
        self.argv: list[str] = []
        self.kwargs: dict[str, object] = {}
        self.cwd: Path | None = None
        self.cwd_mode: int | None = None
        self.schema_mode: int | None = None
        self.entries_before_result: tuple[str, ...] = ()
        self.schema: dict[str, object] | None = None

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.cwd = Path(str(kwargs["cwd"]))
        self.cwd_mode = stat.S_IMODE(self.cwd.stat().st_mode)
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        self.entries_before_result = tuple(sorted(path.name for path in self.cwd.iterdir()))
        self.schema_mode = stat.S_IMODE(schema_path.stat().st_mode)
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if self.error is not None:
            raise self.error
        if self.raw_result is not None:
            result_path.write_bytes(self.raw_result)
        return subprocess.CompletedProcess(argv, self.returncode)


class CodexCLITransportTest(unittest.TestCase):
    def test_fixed_argv_stdin_private_cwd_schema_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            executable = make_executable(Path(name))
            runner = FakeRunner()
            transport = CodexCLITransport(cli_config(executable), runner)
            prompt = "SENTINEL_UNTRUSTED_VACANCY_TEXT"

            result = transport.complete(prompt, RESULT_SCHEMA)

        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(runner.argv[:2], [str(executable), "exec"])
        self.assertEqual(runner.argv[-1], "-")
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--strict-config", "--skip-git-repo-check", "--json"):
            self.assertIn(flag, runner.argv)
        for feature in DISABLED_TOOL_FEATURES:
            self.assertIn(feature, runner.argv)
        self.assertIn('model_reasoning_effort="xhigh"', runner.argv)
        self.assertIn('approval_policy="never"', runner.argv)
        self.assertIn('web_search="disabled"', runner.argv)
        self.assertNotIn(prompt, " ".join(runner.argv))
        self.assertEqual(runner.kwargs["input"], prompt)
        self.assertEqual(runner.kwargs["timeout"], 300)
        self.assertIs(runner.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(runner.kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(runner.kwargs["shell"], False)
        self.assertIs(runner.kwargs["check"], False)
        self.assertEqual(runner.cwd_mode, 0o700)
        self.assertEqual(runner.schema_mode, 0o600)
        self.assertEqual(runner.entries_before_result, ("schema.json",))
        self.assertEqual(runner.schema, RESULT_SCHEMA)
        self.assertIsNotNone(runner.cwd)
        self.assertFalse(runner.cwd.exists())

    def test_timeout_nonzero_and_runner_exception_are_redacted_and_cleanup(self) -> None:
        private_marker = "RAW_PRIVATE_PROCESS_OUTPUT"
        scenarios = (
            FakeRunner(error=subprocess.TimeoutExpired([private_marker], 300, output=private_marker, stderr=private_marker)),
            FakeRunner(returncode=9),
            FakeRunner(error=OSError(private_marker)),
        )
        with tempfile.TemporaryDirectory() as name:
            executable = make_executable(Path(name))
            for runner in scenarios:
                with self.subTest(runner=runner):
                    with self.assertRaises(CodexCLIError) as raised:
                        CodexCLITransport(cli_config(executable), runner).complete("safe prompt", RESULT_SCHEMA)
                    self.assertNotIn(private_marker, str(raised.exception))
                    self.assertIsNotNone(runner.cwd)
                    self.assertFalse(runner.cwd.exists())

    def test_missing_oversized_malformed_and_schema_invalid_results_are_rejected(self) -> None:
        scenarios = (
            ("missing", None),
            ("oversized", b"x" * (MAX_RESULT_BYTES + 1)),
            ("malformed", b"not-json"),
            ("not-object", b"[]"),
            ("extra-property", b'{"value":"ok","unexpected":true}'),
            ("wrong-type", b'{"value":7}'),
        )
        with tempfile.TemporaryDirectory() as name:
            executable = make_executable(Path(name))
            for label, raw_result in scenarios:
                runner = FakeRunner(raw_result=raw_result)
                with self.subTest(label=label):
                    with self.assertRaises(CodexCLIError):
                        CodexCLITransport(cli_config(executable), runner).complete("safe prompt", RESULT_SCHEMA)
                    self.assertIsNotNone(runner.cwd)
                    self.assertFalse(runner.cwd.exists())

    def test_open_schema_and_oversized_prompt_are_rejected_before_runner(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            executable = make_executable(Path(name))
            runner = FakeRunner()
            open_schema = {"type": "object", "additionalProperties": True, "properties": {}}
            with self.assertRaisesRegex(CodexCLIError, "неизвестную схему"):
                CodexCLITransport(cli_config(executable), runner).complete("safe", open_schema)
            with self.assertRaisesRegex(CodexCLIError, "prompt превышает"):
                CodexCLITransport(cli_config(executable), runner).complete("я" * 200_001, RESULT_SCHEMA)
            self.assertEqual(runner.argv, [])


if __name__ == "__main__":
    unittest.main()
