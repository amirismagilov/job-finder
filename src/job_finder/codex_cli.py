from __future__ import annotations

# region MODULE_CONTRACT [DOMAIN(10): AITransport; CONCEPT(10): EphemeralIsolation, StructuredOutput; TECH(9): CodexCLI, Subprocess]
## @file codex_cli.py
## @brief Isolated one-shot transport for structured Codex CLI completions.
## @modulecontract
## @purpose Use existing Codex ChatGPT authorization without exposing untrusted vacancy or chat content to tools, argv, repository rules or persistent sessions.
## @scope Fixed subprocess invocation, private temporary schema/result files, bounded execution and strict JSON result validation.
## @input Validated LLMConfig, sanitized prompt and a closed task-specific JSON Schema.
## @output One schema-valid JSON object or a non-sensitive CodexCLIError.
## @invariants Prompt travels only through stdin; shell/web/apps/user config/rules are disabled; cwd is private and ephemeral; raw process output is never logged or returned.
## @changes LAST_CHANGE: [v0.3.0 — Added isolated Codex CLI structured-output transport.]
## @modulemap
## CLASS 10[Safe fixed-argv Codex subprocess boundary] => CodexCLITransport
## CLASS 9[Non-sensitive transport failure] => CodexCLIError
## FUNC 9[Validates output against the closed task schema] => _matches_schema
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: Codex CLI, ephemeral, stdin prompt, strict config, disabled tools, private temporary directory, bounded JSON
# STRUCTURE: validated config + prompt/schema -> fixed argv in private cwd -> bounded result file -> schema-valid object

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Mapping

from .config import CODEX_MODEL_CHARS, LLMConfig, REASONING_EFFORTS


MAX_PROMPT_BYTES = 400_000
MAX_RESULT_BYTES = 256_000
DISABLED_TOOL_FEATURES = (
    "apps",
    "browser_use",
    "computer_use",
    "goals",
    "image_generation",
    "multi_agent",
    "shell_tool",
    "skill_search",
    "sleep_tool",
    "tool_suggest",
    "unified_exec",
    "view_image",
)
Runner = Callable[..., subprocess.CompletedProcess[str]]


class CodexCLIError(RuntimeError):
    pass


def _matches_schema(value: Any, schema: Mapping[str, Any]) -> bool:
    expected = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        return False
    if expected == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(value, dict) or schema.get("additionalProperties") is not False:
            return False
        if not isinstance(properties, dict) or not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            return False
        if not set(required).issubset(value) or not set(value).issubset(properties):
            return False
        return all(isinstance(properties[name], Mapping) and _matches_schema(item, properties[name]) for name, item in value.items())
    if expected == "array":
        item_schema = schema.get("items")
        if not isinstance(value, list) or not isinstance(item_schema, Mapping):
            return False
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            return False
        return all(_matches_schema(item, item_schema) for item in value)
    if expected == "string":
        if not isinstance(value, str):
            return False
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            return False
        return "maxLength" not in schema or len(value) <= int(schema["maxLength"])
    if expected == "integer":
        valid_type = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "number":
        valid_type = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "boolean":
        return isinstance(value, bool)
    else:
        return False
    if not valid_type:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    if "minimum" in schema and value < schema["minimum"]:
        return False
    return "maximum" not in schema or value <= schema["maximum"]


# region CLASS_CodexCLITransport [DOMAIN(10): AITransport; CONCEPT(10): ToolIsolation; TECH(9): subprocess.run]
## @purpose Execute one schema-bound semantic task without granting model-originated text access to local or remote tools.
class CodexCLITransport:
    def __init__(self, config: LLMConfig, runner: Runner | None = None) -> None:
        self.config = config
        self.runner = runner or subprocess.run

    # region METHOD_complete [DOMAIN(10): AITransport; CONCEPT(10): BoundedEphemeralCall; TECH(9): FixedArgv]
    ## @purpose Send only stdin prompt to a fixed, tool-disabled CLI process and accept only a bounded schema-valid result file.
    ## @io sanitized prompt + closed JSON Schema -> JSON object or CodexCLIError
    ## @complexity 8
    def complete(self, prompt: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        executable = self.config.codex_executable
        effort = self.config.reasoning_effort
        model = self.config.model
        if (
            self.config.provider != "codex_cli"
            or executable is None
            or not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
            or effort not in REASONING_EFFORTS
            or not model
            or len(model) > 128
            or any(char not in CODEX_MODEL_CHARS for char in model)
        ):
            raise CodexCLIError("Codex CLI provider некорректно настроен")
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise CodexCLIError("Codex CLI prompt превышает допустимый размер")
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise CodexCLIError("Codex CLI получил неизвестную схему результата")

        with tempfile.TemporaryDirectory(prefix="job-finder-codex-cli-") as name:
            workdir = Path(name)
            workdir.chmod(0o700)
            schema_path = workdir / "schema.json"
            result_path = workdir / "result.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            schema_path.chmod(0o600)
            argv = [
                str(executable),
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--strict-config",
                "--model",
                model,
                "--config",
                f'model_reasoning_effort="{effort}"',
                "--config",
                'approval_policy="never"',
                "--config",
                'web_search="disabled"',
            ]
            for feature in DISABLED_TOOL_FEATURES:
                argv.extend(("--disable", feature))
            argv.extend([
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                "-",
            ])
            try:
                completed = self.runner(
                    argv,
                    input=prompt,
                    text=True,
                    cwd=str(workdir),
                    stdin=None,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self.config.timeout_seconds,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexCLIError("Codex CLI превысил допустимое время выполнения") from exc
            except Exception as exc:
                raise CodexCLIError("Не удалось безопасно запустить Codex CLI") from exc
            if completed.returncode != 0:
                raise CodexCLIError("Codex CLI завершился с ошибкой")
            if result_path.is_symlink() or not result_path.is_file():
                raise CodexCLIError("Codex CLI не создал результат")
            try:
                result_path.chmod(0o600)
                if result_path.stat().st_size > MAX_RESULT_BYTES:
                    raise CodexCLIError("Результат Codex CLI превышает допустимый размер")
                with result_path.open("rb") as result_file:
                    raw = result_file.read(MAX_RESULT_BYTES + 1)
            except CodexCLIError:
                raise
            except OSError as exc:
                raise CodexCLIError("Не удалось безопасно прочитать результат Codex CLI") from exc
            if len(raw) > MAX_RESULT_BYTES:
                raise CodexCLIError("Результат Codex CLI превышает допустимый размер")
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CodexCLIError("Codex CLI вернул некорректный JSON") from exc
            if not isinstance(result, dict) or not _matches_schema(result, schema):
                raise CodexCLIError("Codex CLI вернул результат вне разрешённой схемы")
            return result
    # endregion METHOD_complete
# endregion CLASS_CodexCLITransport
