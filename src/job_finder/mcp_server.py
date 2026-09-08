from __future__ import annotations

import json
import sys
from typing import Any, Callable

from .app import create_service


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


TOOLS: list[dict[str, Any]] = [
    {
        "name": "status",
        "description": "Проверить локальную конфигурацию, авторизацию hh.ru, файлы знаний, лимиты и safety-stop. Не делает сетевых запросов.",
        "inputSchema": _object_schema({}),
    },
    {
        "name": "discover_vacancies",
        "description": "Найти и локально ранжировать вакансии через официальный API hh.ru. Если queries не заданы, используются безопасные пресеты из config.toml.",
        "inputSchema": _object_schema(
            {
                "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "area": {"type": "string", "description": "ID региона hh.ru; Москва — 1"},
                "days": {"type": "integer", "minimum": 1, "maximum": 30},
                "per_query": {"type": "integer", "minimum": 1, "maximum": 20},
                "only_with_salary": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            }
        ),
    },
    {
        "name": "get_vacancy",
        "description": "Получить полное нормализованное описание вакансии hh.ru и предварительную оценку соответствия.",
        "inputSchema": _object_schema({"vacancy_id": {"type": "string"}}, ["vacancy_id"]),
    },
    {
        "name": "list_my_resumes",
        "description": "Получить краткий список резюме текущего соискателя. Требует OAuth hh.ru.",
        "inputSchema": _object_schema({}),
    },
    {
        "name": "prepare_cover_letter",
        "description": "Подготовить контекст из вакансии, подтверждённых фактов и правил. После вызова модель должна сгенерировать пользователю готовое письмо; этот инструмент ничего не отправляет.",
        "inputSchema": _object_schema({"vacancy_id": {"type": "string"}}, ["vacancy_id"]),
    },
    {
        "name": "list_chats",
        "description": "Получить чаты по вакансиям через актуальный официальный API /common/chats. Требует OAuth hh.ru.",
        "inputSchema": _object_schema(
            {
                "unread_only": {"type": "boolean", "default": True},
                "page": {"type": "integer", "minimum": 0, "maximum": 50, "default": 0},
                "per_page": {"type": "integer", "minimum": 1, "maximum": 20, "default": 20},
            }
        ),
    },
    {
        "name": "get_chat_messages",
        "description": "Получить последние сообщения и доступность ответа в чате hh.ru.",
        "inputSchema": _object_schema(
            {"chat_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
            ["chat_id"],
        ),
    },
    {
        "name": "prepare_chat_answer",
        "description": "Подготовить подтверждённый контекст для ответа рекрутеру. Передайте chat_id или текст question. После вызова модель должна написать готовый ответ; инструмент ничего не отправляет.",
        "inputSchema": _object_schema({"question": {"type": "string"}, "chat_id": {"type": "string"}}),
    },
    {
        "name": "prepare_application",
        "description": "Шаг 1/2: создать предпросмотр отклика. Ничего не отправляет. Вызывать только с уже согласованным текстом письма.",
        "inputSchema": _object_schema(
            {"vacancy_id": {"type": "string"}, "resume_id": {"type": "string"}, "cover_letter": {"type": "string", "maxLength": 10000}},
            ["vacancy_id", "resume_id", "cover_letter"],
        ),
    },
    {
        "name": "submit_application",
        "description": "Шаг 2/2: отправить ранее показанный отклик. Разрешено вызывать только после отдельного явного согласия пользователя на конкретный предпросмотр.",
        "inputSchema": _object_schema(
            {"confirmation_id": {"type": "string"}, "confirmation": {"type": "string", "enum": ["ОТПРАВИТЬ"]}},
            ["confirmation_id", "confirmation"],
        ),
    },
    {
        "name": "prepare_chat_message",
        "description": "Шаг 1/2: проверить чат и создать предпросмотр сообщения. Ничего не отправляет.",
        "inputSchema": _object_schema(
            {"chat_id": {"type": "string"}, "message": {"type": "string", "maxLength": 20000}},
            ["chat_id", "message"],
        ),
    },
    {
        "name": "submit_chat_message",
        "description": "Шаг 2/2: отправить ранее показанное сообщение с is_automated=true. Только после отдельного явного согласия пользователя.",
        "inputSchema": _object_schema(
            {"confirmation_id": {"type": "string"}, "confirmation": {"type": "string", "enum": ["ОТПРАВИТЬ"]}},
            ["confirmation_id", "confirmation"],
        ),
    },
]


def _dispatch(service: Any, name: str, args: dict[str, Any]) -> Any:
    calls: dict[str, Callable[[], Any]] = {
        "status": lambda: service.status(),
        "discover_vacancies": lambda: service.discover_vacancies(**args),
        "get_vacancy": lambda: service.get_vacancy(**args),
        "list_my_resumes": lambda: service.list_my_resumes(),
        "prepare_cover_letter": lambda: service.prepare_cover_letter(**args),
        "list_chats": lambda: service.list_chats(**args),
        "get_chat_messages": lambda: service.get_chat_messages(**args),
        "prepare_chat_answer": lambda: service.prepare_chat_answer(**args),
        "prepare_application": lambda: service.prepare_application(**args),
        "submit_application": lambda: service.submit_application(**args),
        "prepare_chat_message": lambda: service.prepare_chat_message(**args),
        "submit_chat_message": lambda: service.submit_chat_message(**args),
    }
    if name not in calls:
        raise ValueError(f"Неизвестный инструмент: {name}")
    return calls[name]()


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_message(service: Any, message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "amir-job-finder", "version": "0.1.0"},
                "instructions": (
                    "Используйте только официальный API hh.ru. Поиск и генерация черновиков разрешены автоматически. "
                    "Любая отправка двухшаговая: сначала prepare с полным предпросмотром, затем отдельное явное согласие "
                    "пользователя и submit. Не интерпретируйте общий запрос на поиск как согласие на отправку."
                ),
            },
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        try:
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise ValueError("arguments должен быть объектом")
            value = _dispatch(service, name, args)
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
            return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            print(f"job-finder tool error: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
            text = json.dumps({"error": str(exc), "type": exc.__class__.__name__}, ensure_ascii=False)
            return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": True})
    if request_id is None:
        return None
    return _error(request_id, -32601, f"Метод не поддерживается: {method}")


def main() -> None:
    try:
        service = create_service()
    except Exception as exc:
        print(f"job-finder startup error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line)
            response = handle_message(service, message)
        except Exception as exc:
            print(f"job-finder protocol error: {exc}", file=sys.stderr, flush=True)
            response = _error(None, -32700, "Некорректный JSON-RPC запрос")
        if response is not None:
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
