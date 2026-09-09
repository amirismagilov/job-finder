# region MODULE_CONTRACT [DOMAIN(10): LocalTransport, Security; CONCEPT(10): Pairing, BoundedQueue; TECH(9): HTTPServer, HMAC]
## @file bridge_server.py
## @brief Authenticated allowlisted loopback queue for the Chromium extension.
## @modulecontract
## @purpose Connect deterministic backend commands to the browser-held session without receiving cookies or arbitrary HTTP instructions.
## @scope 127.0.0.1 command polling and bounded result submission.
## @input Pairing secret in Authorization and typed envelopes.
## @output Validated ResponseEnvelope.
## @invariants Loopback only; extension identity is bound on first successful poll; request bodies are bounded and never logged.
## @changes LAST_CHANGE: [v0.2.5 — Supported Chrome's omitted extension Origin while retaining secret and ID binding.]
## @modulemap
## CLASS 10[Thread-safe one-shot command rendezvous] => BridgeQueue
## CLASS 10[Loopback transport and pairing boundary] => BrowserBridgeServer
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: browser bridge, 127.0.0.1, pairing, bearer, extension identity, command queue
# STRUCTURE: backend enqueue -> authenticated extension poll -> fixed action -> bounded result -> backend classify

from collections import deque
from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import re
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlparse

from .config import BridgeConfig
from .web_contract import WRITE_ACTIONS, CommandEnvelope, ResponseClass, ResponseEnvelope, classify_response, response_from_dict, validate_command


logger = logging.getLogger(__name__)
EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")


class SecretStore(Protocol):
    def get(self, account: str) -> str | None: ...
    def set(self, account: str, value: str) -> None: ...


class BridgeError(RuntimeError):
    def __init__(self, message: str, *, outcome: ResponseClass = ResponseClass.FAILURE) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass
class _Waiting:
    command: CommandEnvelope
    result: ResponseEnvelope | None = None


# region CLASS_BridgeQueue [DOMAIN(9): Concurrency; CONCEPT(10): OneShotDelivery; TECH(8): Condition]
## @purpose Coordinate one sequential command with an extension without automatic write retries.
class BridgeQueue:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: deque[_Waiting] = deque()
        self._by_id: dict[str, _Waiting] = {}

    def enqueue(self, command: CommandEnvelope, timeout: float) -> ResponseEnvelope:
        validate_command(command)
        waiting = _Waiting(command)
        with self._condition:
            self._pending.append(waiting)
            self._by_id[command.command_id] = waiting
            self._condition.notify_all()
            deadline = time.monotonic() + timeout
            while waiting.result is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._by_id.pop(command.command_id, None)
                    try:
                        self._pending.remove(waiting)
                    except ValueError:
                        pass
                    raise BridgeError("Расширение не вернуло результат в срок")
                self._condition.wait(remaining)
            self._by_id.pop(command.command_id, None)
            return waiting.result

    def next(self) -> CommandEnvelope | None:
        with self._condition:
            if not self._pending:
                return None
            return self._pending.popleft().command

    def resolve(self, response: ResponseEnvelope) -> bool:
        with self._condition:
            waiting = self._by_id.get(response.command_id)
            if waiting is None or waiting.result is not None:
                return False
            waiting.result = response
            self._condition.notify_all()
            return True
# endregion CLASS_BridgeQueue


class _BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# region CLASS_BrowserBridgeServer [DOMAIN(10): LocalTransport; CONCEPT(10): AuthenticatedAllowlist; TECH(9): HTTPServer]
## @purpose Expose only poll/result endpoints to one paired Chromium extension on a fixed loopback address.
class BrowserBridgeServer:
    def __init__(self, config: BridgeConfig, secrets_store: SecretStore, *, storage: Any | None = None) -> None:
        if config.host != "127.0.0.1" or config.port != 8766:
            raise BridgeError("Небезопасный адрес bridge")
        self.config = config
        self.secrets = secrets_store
        self.storage = storage
        self.queue = BridgeQueue()
        self._server: _BridgeHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._last_seen = 0.0
        self._last_auth_failure: tuple[str, float] | None = None

    @property
    def paired(self) -> bool:
        return bool(self.secrets.get("bridge_pairing_secret"))

    @property
    def connected(self) -> bool:
        return self.paired and time.monotonic() - self._last_seen < 20

    def _auth_rejected(self, reason: str) -> bool:
        """fixed reason code -> rate-limited local log/audit -> false authorization verdict."""
        now = time.monotonic()
        previous_reason, previous_at = self._last_auth_failure or ("", 0.0)
        if reason != previous_reason or now - previous_at >= 60:
            # BUG_FIX_CONTEXT: A single 401 hid secret, Origin, extension-ID and stale-binding
            # failures behind the same UI message. Fixed reason codes expose no credential value.
            logger.warning("[IMP:8][BrowserBridgeServer][AUTH_REJECTED] %s", reason)
            if self.storage:
                self.storage.audit(8, "bridge_auth_rejected", detail={"reason": reason})
            self._last_auth_failure = (reason, now)
        return False

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        configured = self.secrets.get("bridge_pairing_secret") or ""
        supplied = handler.headers.get("Authorization", "")
        candidate = supplied[7:] if supplied.startswith("Bearer ") else ""
        extension_id = handler.headers.get("X-Job-Finder-Extension", "")
        origin = handler.headers.get("Origin", "")
        if not configured:
            return self._auth_rejected("configured_secret_missing")
        if not candidate:
            return self._auth_rejected("authorization_missing")
        if not hmac.compare_digest(configured, candidate):
            return self._auth_rejected("secret_mismatch")
        if not EXTENSION_ID_RE.fullmatch(extension_id):
            return self._auth_rejected("extension_id_invalid")
        if origin:
            if origin == "null":
                return self._auth_rejected("origin_null")
            parsed_origin = urlparse(origin)
            try:
                origin_port = parsed_origin.port
            except ValueError:
                return self._auth_rejected("origin_shape_mismatch")
            # BUG_FIX_CONTEXT: Chrome 150 omits Origin on privileged service-worker fetches.
            # When present it remains strictly constrained; when absent the 256-bit bearer,
            # extension-ID binding and browser-enforced CORS preflight remain mandatory.
            if (
                parsed_origin.scheme != "chrome-extension"
                or parsed_origin.hostname != extension_id
                or origin_port is not None
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.params
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                if parsed_origin.scheme != "chrome-extension":
                    return self._auth_rejected("origin_scheme_mismatch")
                if parsed_origin.hostname != extension_id:
                    return self._auth_rejected("origin_host_mismatch")
                return self._auth_rejected("origin_shape_mismatch")
        bound = self.secrets.get("bridge_extension_id")
        if bound and not hmac.compare_digest(bound, extension_id):
            return self._auth_rejected("extension_binding_mismatch")
        if not bound:
            self.secrets.set("bridge_extension_id", extension_id)
        first_connection = self._last_seen == 0.0
        self._last_seen = time.monotonic()
        if first_connection:
            logger.info("[IMP:8][BrowserBridgeServer][CONNECTED] Paired extension authenticated")
            if self.storage:
                self.storage.audit(8, "bridge_connected")
        return True

    def start(self) -> None:
        if self._server:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "JobFinderBridge/0.2"

            def _cors(self) -> None:
                origin = self.headers.get("Origin", "")
                if origin.startswith("chrome-extension://"):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")

            def _json(self, status: int, value: object) -> None:
                body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self._cors()
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Job-Finder-Extension")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/commands/next":
                    self.send_error(404)
                    return
                if not owner._authorized(self):
                    self.send_error(401)
                    return
                command = owner.queue.next()
                self._json(200, {"command": command.to_dict() if command else None})

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                parts = parsed.path.split("/")
                if len(parts) != 5 or parts[:3] != ["", "v1", "commands"] or parts[4] != "result":
                    self.send_error(404)
                    return
                if not owner._authorized(self):
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if length <= 0 or length > owner.config.max_request_bytes:
                    self.send_error(413)
                    return
                try:
                    raw = self.rfile.read(length)
                    value = json.loads(raw)
                    if not isinstance(value, dict) or value.get("command_id") != parts[3]:
                        raise ValueError
                    response = response_from_dict(value, max_body_bytes=owner.config.max_response_bytes)
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                self._json(202 if owner.queue.resolve(response) else 409, {"accepted": True})

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = _BridgeHTTPServer((self.config.host, self.config.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="job-finder-bridge", daemon=True)
        self._thread.start()
        logger.info("[IMP:8][BrowserBridgeServer][START] Loopback bridge started")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def enqueue(self, command: CommandEnvelope) -> ResponseEnvelope:
        if not self._server:
            raise BridgeError("Bridge не запущен")
        if not self.paired:
            raise BridgeError("Bridge не paired; выполните локальную pairing-команду")
        if self.storage:
            self.storage.record_bridge_command(command.command_id, command.action.value, "queued")
        logger.info("[IMP:8][BrowserBridgeServer][COMMAND] Allowed command queued: %s", command.action.value)
        try:
            response = self.queue.enqueue(command, self.config.command_timeout_seconds)
        except BridgeError as exc:
            outcome = ResponseClass.AMBIGUOUS_WRITE if command.action in WRITE_ACTIONS else exc.outcome
            if self.storage:
                self.storage.record_bridge_command(command.command_id, command.action.value, outcome.value)
            raise BridgeError("Расширение не подтвердило результат команды", outcome=outcome) from exc
        outcome = classify_response(command.action, response)
        if self.storage:
            self.storage.record_bridge_command(command.command_id, command.action.value, outcome.value)
        if outcome is not ResponseClass.SUCCESS:
            logger.error("[IMP:10][BrowserBridgeServer][SAFETY] Response classified as %s", outcome.value)
            raise BridgeError(f"Ответ hh.ru классифицирован как {outcome.value}", outcome=outcome)
        return response
# endregion CLASS_BrowserBridgeServer
