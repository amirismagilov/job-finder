from types import SimpleNamespace
import logging
import threading
import unittest

from job_finder.bridge_server import BridgeError, BridgeQueue, BrowserBridgeServer
from job_finder.config import BridgeConfig
from job_finder.keychain import MemorySecrets
from job_finder.web_contract import ResponseClass, ResponseEnvelope, WebAction, new_command


class BridgeServerTest(unittest.TestCase):
    def setUp(self):
        self.secrets = MemorySecrets()
        self.secrets.set("bridge_pairing_secret", "a" * 64)
        self.server = BrowserBridgeServer(BridgeConfig(command_timeout_seconds=2), self.secrets)

    @staticmethod
    def handler(secret="a" * 64, extension="abcdefghijklmnopabcdefghijklmnop"):
        return SimpleNamespace(headers={"Authorization": "Bearer " + secret, "X-Job-Finder-Extension": extension, "Origin": "chrome-extension://" + extension})

    def test_rejects_wrong_pairing_secret_and_binds_extension(self):
        with self.assertLogs("job_finder.bridge_server", level=logging.WARNING) as captured:
            self.assertFalse(self.server._authorized(self.handler(secret="b" * 64)))
        self.assertTrue(any("secret_mismatch" in line for line in captured.output))
        self.assertTrue(self.server._authorized(self.handler()))
        self.assertEqual(self.secrets.get("bridge_extension_id"), "abcdefghijklmnopabcdefghijklmnop")
        with self.assertLogs("job_finder.bridge_server", level=logging.WARNING) as captured:
            self.assertFalse(self.server._authorized(self.handler(extension="ponmlkjihgfedcbaponmlkjihgfedcba")))
        self.assertTrue(any("extension_binding_mismatch" in line for line in captured.output))

    def test_accepts_chrome_origin_with_terminal_slash_but_rejects_extra_path(self):
        extension = "abcdefghijklmnopabcdefghijklmnop"
        slash = self.handler(extension=extension)
        slash.headers["Origin"] = "chrome-extension://" + extension + "/"
        self.assertTrue(self.server._authorized(slash))
        extra = self.handler(extension=extension)
        extra.headers["Origin"] = "chrome-extension://" + extension + "/unexpected"
        with self.assertLogs("job_finder.bridge_server", level=logging.WARNING) as captured:
            self.assertFalse(self.server._authorized(extra))
        self.assertTrue(any("origin_shape_mismatch" in line for line in captured.output))
        malformed = self.handler(extension=extension)
        malformed.headers["Origin"] = "chrome-extension://" + extension + ":invalid"
        self.assertFalse(self.server._authorized(malformed))

    def test_accepts_omitted_chrome_origin_but_rejects_null_origin(self):
        missing = self.handler()
        missing.headers.pop("Origin")
        self.assertTrue(self.server._authorized(missing))
        null = self.handler()
        null.headers["Origin"] = "null"
        with self.assertLogs("job_finder.bridge_server", level=logging.WARNING) as captured:
            self.assertFalse(self.server._authorized(null))
        self.assertTrue(any("origin_null" in line for line in captured.output))

    def test_auth_rejection_log_is_rate_limited_and_never_contains_secret(self):
        self.server.storage = SimpleNamespace(events=[], audit=lambda *args, **kwargs: self.server.storage.events.append((args, kwargs)))
        with self.assertLogs("job_finder.bridge_server", level=logging.WARNING) as captured:
            self.assertFalse(self.server._authorized(self.handler(secret="b" * 64)))
            self.assertFalse(self.server._authorized(self.handler(secret="c" * 64)))
        self.assertEqual(sum("secret_mismatch" in line for line in captured.output), 1)
        self.assertNotIn("b" * 64, "\n".join(captured.output))
        self.assertNotIn("c" * 64, "\n".join(captured.output))
        self.assertEqual(len(self.server.storage.events), 1)

    def test_command_queue_round_trip_is_one_shot(self):
        queue = BridgeQueue()
        command = new_command(WebAction.GET_RESPONSE_POPUP, {"vacancy_id": "vacancy-a"})
        output = {}
        thread = threading.Thread(target=lambda: output.setdefault("response", queue.enqueue(command, 2)))
        thread.start()
        polled = queue.next()
        self.assertEqual(polled.command_id, command.command_id)
        response = ResponseEnvelope(command.command_id, 200, "application/json", {"responseStatus": {}})
        self.assertTrue(queue.resolve(response))
        self.assertFalse(queue.resolve(response))
        thread.join(2)
        self.assertEqual(output["response"].command_id, command.command_id)

    def test_unconfirmed_write_timeout_is_ambiguous(self):
        class TimeoutQueue:
            def enqueue(self, command, timeout):
                raise BridgeError("timeout")
        self.server._server = object()
        self.server.queue = TimeoutQueue()
        command = new_command(WebAction.SEND_CHAT_MESSAGE, {"chat_id": "chat-a", "text": "Здравствуйте", "idempotency_key": "00000000-0000-4000-8000-000000000000"})
        with self.assertRaises(BridgeError) as error:
            self.server.enqueue(command)
        self.assertEqual(error.exception.outcome, ResponseClass.AMBIGUOUS_WRITE)
        self.server._server = None


if __name__ == "__main__":
    unittest.main()
