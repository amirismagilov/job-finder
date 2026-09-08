from __future__ import annotations

import argparse
import base64
from getpass import getpass
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

from .app import create_service
from .config import load_config
from .hh_client import HHClient
from .keychain import MacOSKeychain


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def configure_auth(store: MacOSKeychain) -> None:
    print("Создайте приложение на https://dev.hh.ru/admin и задайте локальный redirect URI.")
    client_id = input("client_id: ").strip()
    client_secret = getpass("client_secret (ввод скрыт): ").strip()
    redirect_uri = input("redirect_uri [http://127.0.0.1:8765/callback]: ").strip() or "http://127.0.0.1:8765/callback"
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        raise SystemExit("Разрешён только фиксированный loopback redirect URI вида http://127.0.0.1:8765/callback")
    if not client_id or not client_secret:
        raise SystemExit("client_id и client_secret обязательны")
    store.set("client_id", client_id)
    store.set("client_secret", client_secret)
    store.set("redirect_uri", redirect_uri)
    print("OAuth-параметры сохранены в macOS Keychain.")


def login(store: MacOSKeychain, *, no_browser: bool) -> None:
    cfg = load_config()
    client_id = store.get("client_id")
    redirect_uri = store.get("redirect_uri")
    if not client_id or not store.get("client_secret") or not redirect_uri:
        raise SystemExit("Сначала выполните: job-finder auth configure")
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        raise SystemExit("В Keychain записан небезопасный redirect_uri; повторите auth configure")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    authorize_url = "https://hh.ru/oauth/authorize?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "state": state,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "role": "applicant",
            "force_role": "true",
        }
    )
    received: dict[str, str] = {}
    expected_path = parsed.path or "/"

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            incoming = urlparse(self.path)
            values = parse_qs(incoming.query)
            if incoming.path != expected_path:
                self.send_error(404)
                return
            received["state"] = (values.get("state") or [""])[0]
            received["code"] = (values.get("code") or [""])[0]
            received["error"] = (values.get("error") or [""])[0]
            body = "Авторизация получена. Вернитесь в терминал и закройте эту вкладку.".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer((parsed.hostname, parsed.port), CallbackHandler)
    server.timeout = 180
    print("Откройте ссылку авторизации hh.ru:\n" + authorize_url)
    if not no_browser:
        webbrowser.open(authorize_url)
    print("Ожидаю callback не более 3 минут…")
    server.handle_request()
    server.server_close()
    if received.get("error"):
        raise SystemExit(f"hh.ru отказал в авторизации: {received['error']}")
    if not received.get("code"):
        raise SystemExit("Callback не получен или истёк тайм-аут")
    if not secrets.compare_digest(received.get("state", ""), state):
        raise SystemExit("Проверка OAuth state не прошла; токен не запрашивался")
    client = HHClient(cfg, store)
    client.exchange_code(received["code"], verifier)
    me = client.me()
    print(f"Авторизация успешна: {me.get('first_name', '')} {me.get('last_name', '')}".strip())


def import_token(store: MacOSKeychain) -> None:
    print("OAuth login предпочтительнее. Токен вводится скрыто и сохраняется только в macOS Keychain.")
    access_token = getpass("access_token: ").strip()
    refresh_token = getpass("refresh_token (Enter, если отсутствует): ").strip()
    if not access_token:
        raise SystemExit("access_token не может быть пустым")
    store.set("access_token", access_token)
    if refresh_token:
        store.set("refresh_token", refresh_token)
    print("Токен сохранён. Не публикуйте его и не вставляйте в чат.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-finder", description="Локальный безопасный помощник hh.ru")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Локальный статус")

    auth = sub.add_parser("auth", help="Авторизация hh.ru")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser("configure", help="Сохранить client_id/client_secret/redirect_uri в Keychain")
    login_parser = auth_sub.add_parser("login", help="OAuth2 + PKCE вход через hh.ru")
    login_parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер автоматически")
    auth_sub.add_parser("import-token", help="Импортировать готовый токен через скрытый ввод")
    auth_sub.add_parser("test", help="Проверить токен запросом /me")

    safety = sub.add_parser("safety", help="Управление safety-stop")
    safety_sub = safety.add_subparsers(dest="safety_command", required=True)
    safety_sub.add_parser("clear-stop", help="Снять блокировку после ручной проверки hh.ru")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    store = MacOSKeychain()
    if args.command == "status":
        _print_json(create_service(secrets_store=store).status())
    elif args.command == "auth" and args.auth_command == "configure":
        configure_auth(store)
    elif args.command == "auth" and args.auth_command == "login":
        login(store, no_browser=args.no_browser)
    elif args.command == "auth" and args.auth_command == "import-token":
        import_token(store)
    elif args.command == "auth" and args.auth_command == "test":
        client = HHClient(load_config(), store)
        me = client.me()
        _print_json({key: me.get(key) for key in ("id", "first_name", "last_name", "email")})
    elif args.command == "safety" and args.safety_command == "clear-stop":
        phrase = input("После ручной проверки аккаунта введите СНЯТЬ БЛОКИРОВКУ: ").strip()
        if phrase != "СНЯТЬ БЛОКИРОВКУ":
            raise SystemExit("Фраза не совпала; блокировка сохранена")
        service = create_service(secrets_store=store)
        service.storage.clear_hard_stop()
        print("Safety-stop снят.")


if __name__ == "__main__":
    main()
