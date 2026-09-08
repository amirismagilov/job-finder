from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import secrets
import ssl
import threading
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .config import Config


class SecretStore(Protocol):
    def get(self, account: str) -> str | None: ...
    def set(self, account: str, value: str) -> None: ...


class HHError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, codes: tuple[str, ...] = (), payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.codes = codes
        self.payload = payload

    @property
    def requires_hard_stop(self) -> bool:
        hard_codes = {"captcha_required", "limit_exceeded", "overall_limit"}
        return self.status == 429 or bool(hard_codes.intersection(self.codes))


@dataclass(frozen=True)
class APIResponse:
    status: int
    data: Any
    headers: dict[str, str]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class HHClient:
    def __init__(self, config: Config, secrets_store: SecretStore) -> None:
        self.config = config
        self.secrets = secrets_store
        self._opener = build_opener(_NoRedirect, HTTPSHandler(context=self._tls_context()))
        self._lock = threading.Lock()
        self._last_request = 0.0

    @staticmethod
    def _tls_context() -> ssl.SSLContext:
        """Использовать системный CA bundle, не отключая проверку TLS.

        Homebrew Python иногда ссылается на отсутствующий Homebrew CA bundle,
        хотя macOS предоставляет актуальный /etc/ssl/cert.pem.
        """
        paths = ssl.get_default_verify_paths()
        candidates = [paths.cafile, paths.openssl_cafile, "/etc/ssl/cert.pem"]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return ssl.create_default_context(cafile=candidate)
        return ssl.create_default_context()

    def _throttle(self, write: bool) -> None:
        minimum = (
            self.config.safety.minimum_write_interval_seconds
            if write
            else self.config.safety.minimum_read_interval_seconds
        )
        with self._lock:
            remaining = minimum - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()

    @staticmethod
    def _error_codes(payload: Any) -> tuple[str, ...]:
        result: list[str] = []
        if isinstance(payload, dict):
            for item in payload.get("errors") or []:
                if isinstance(item, dict):
                    for key in ("type", "value"):
                        value = item.get(key)
                        if value:
                            result.append(str(value))
            for key in ("error", "error_description"):
                value = payload.get(key)
                if value:
                    result.append(str(value))
        return tuple(dict.fromkeys(result))

    @staticmethod
    def _parse_body(raw: bytes, content_type: str) -> Any:
        text = raw.decode("utf-8", errors="replace")
        if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        return text

    def _token_expired(self) -> bool:
        expires_at = self.secrets.get("expires_at")
        if not expires_at:
            return False
        try:
            return float(expires_at) <= time.time() + 30
        except ValueError:
            return True

    def _access_token(self, required: bool) -> str | None:
        token = self.secrets.get("access_token")
        if token and self._token_expired() and self.secrets.get("refresh_token"):
            self.refresh_tokens()
            token = self.secrets.get("access_token")
        if required and not token:
            raise HHError("Не настроена авторизация hh.ru. Выполните: job-finder auth configure, затем job-finder auth login")
        return token

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        multipart: dict[str, str] | None = None,
        auth_required: bool = False,
        write: bool = False,
        allowed_statuses: tuple[int, ...] = (200,),
        retry_auth: bool = True,
    ) -> APIResponse:
        if not path.startswith("/") or path.startswith("//"):
            raise HHError("Некорректный путь API")
        url = self.config.hh.base_url + path
        if query:
            clean_query = {key: value for key, value in query.items() if value is not None}
            url += "?" + urlencode(clean_query, doseq=True)
        headers = {
            "Accept": "application/json",
            "User-Agent": self.config.hh.user_agent,
            "HH-User-Agent": self.config.hh.user_agent,
        }
        # На /token нельзя добавлять просроченный Bearer и нельзя запускать refresh рекурсивно.
        token = None if path == "/token" else self._access_token(auth_required)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        elif form is not None:
            body = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif multipart is not None:
            boundary = "job-finder-" + secrets.token_hex(16)
            pieces: list[bytes] = []
            for name, value in multipart.items():
                pieces.extend(
                    [
                        f"--{boundary}\r\n".encode(),
                        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                        str(value).encode("utf-8"),
                        b"\r\n",
                    ]
                )
            pieces.append(f"--{boundary}--\r\n".encode())
            body = b"".join(pieces)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        self._throttle(write)
        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            response = self._opener.open(request, timeout=30)
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise HHError("Ответ hh.ru превышает безопасный лимит 2 МБ")
            status = response.status
            response_headers = dict(response.headers.items())
            data = self._parse_body(raw, response_headers.get("Content-Type", ""))
        except HTTPError as exc:
            raw = exc.read(2_000_001)
            status = exc.code
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            data = self._parse_body(raw, response_headers.get("Content-Type", ""))
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            detail = str(reason or exc)
            raise HHError(f"Сетевая ошибка при обращении к hh.ru: {detail[:300]}") from exc

        if status not in allowed_statuses:
            codes = self._error_codes(data)
            if retry_auth and "token_expired" in codes and self.secrets.get("refresh_token"):
                self.refresh_tokens()
                return self.request(
                    method,
                    path,
                    query=query,
                    json_body=json_body,
                    form=form,
                    multipart=multipart,
                    auth_required=auth_required,
                    write=write,
                    allowed_statuses=allowed_statuses,
                    retry_auth=False,
                )
            readable = ", ".join(codes) if codes else "без кода ошибки"
            raise HHError(f"hh.ru вернул HTTP {status}: {readable}", status=status, codes=codes, payload=data)
        return APIResponse(status=status, data=data, headers=response_headers)

    def refresh_tokens(self) -> None:
        refresh_token = self.secrets.get("refresh_token")
        if not refresh_token:
            raise HHError("Нет refresh_token; требуется повторный вход")
        response = self.request(
            "POST",
            "/token",
            form={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth_required=False,
            write=False,
            allowed_statuses=(200,),
            retry_auth=False,
        )
        self.save_token_response(response.data)

    def save_token_response(self, data: Any) -> None:
        if not isinstance(data, dict) or not data.get("access_token"):
            raise HHError("hh.ru вернул некорректный ответ с токеном")
        self.secrets.set("access_token", str(data["access_token"]))
        if data.get("refresh_token"):
            self.secrets.set("refresh_token", str(data["refresh_token"]))
        expires_in = int(data.get("expires_in") or 0)
        if expires_in:
            self.secrets.set("expires_at", str(time.time() + expires_in))

    def exchange_code(self, code: str, code_verifier: str) -> None:
        required = {name: self.secrets.get(name) for name in ("client_id", "client_secret", "redirect_uri")}
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise HHError("Не настроены OAuth-параметры: " + ", ".join(missing))
        response = self.request(
            "POST",
            "/token",
            form={
                "grant_type": "authorization_code",
                "client_id": required["client_id"],
                "client_secret": required["client_secret"],
                "redirect_uri": required["redirect_uri"],
                "code": code,
                "code_verifier": code_verifier,
            },
            auth_required=False,
            allowed_statuses=(200,),
            retry_auth=False,
        )
        self.save_token_response(response.data)

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/me", auth_required=True).data

    def search_vacancies(self, *, text: str, area: str, period: int, page: int, per_page: int, only_with_salary: bool) -> dict[str, Any]:
        return self.request(
            "GET",
            "/vacancies",
            query={
                "text": text,
                "area": area,
                "period": period,
                "page": page,
                "per_page": per_page,
                "order_by": "publication_time",
                "only_with_salary": str(only_with_salary).lower(),
            },
        ).data

    def get_vacancy(self, vacancy_id: str) -> dict[str, Any]:
        return self.request("GET", f"/vacancies/{quote(str(vacancy_id), safe='')}").data

    def my_resumes(self) -> dict[str, Any]:
        return self.request("GET", "/resumes/mine", auth_required=True).data

    def suitable_resumes(self, vacancy_id: str) -> dict[str, Any]:
        return self.request("GET", f"/vacancies/{quote(str(vacancy_id), safe='')}/suitable_resumes", auth_required=True).data

    def list_chats(self, *, unread_only: bool, page: int, per_page: int) -> dict[str, Any]:
        return self.request(
            "GET",
            "/common/chats",
            query={"filter_unread": str(unread_only).lower(), "page": page, "per_page": per_page, "vacancy_status": "with"},
            auth_required=True,
        ).data

    def chat_messages(self, chat_id: str, *, limit: int = 20) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/common/chats/{quote(str(chat_id), safe='')}/messages",
            query={"limit": limit, "order": "prev"},
            auth_required=True,
        ).data

    def apply(self, vacancy_id: str, resume_id: str, message: str) -> APIResponse:
        return self.request(
            "POST",
            "/negotiations",
            multipart={"vacancy_id": vacancy_id, "resume_id": resume_id, "message": message},
            auth_required=True,
            write=True,
            allowed_statuses=(201, 303),
        )

    def send_chat_message(self, chat_id: str, message: str, idempotency_key: str) -> APIResponse:
        return self.request(
            "POST",
            f"/common/chats/{quote(str(chat_id), safe='')}/messages",
            json_body={"text": message, "idempotency_key": idempotency_key, "is_automated": True},
            auth_required=True,
            write=True,
            allowed_statuses=(201,),
        )

    def auth_summary(self) -> dict[str, Any]:
        expires_at = self.secrets.get("expires_at")
        return {
            "client_configured": all(self.secrets.get(key) for key in ("client_id", "client_secret", "redirect_uri")),
            "access_token_present": bool(self.secrets.get("access_token")),
            "refresh_token_present": bool(self.secrets.get("refresh_token")),
            "expires_at": datetime.fromtimestamp(float(expires_at), UTC).isoformat() if expires_at else None,
        }
