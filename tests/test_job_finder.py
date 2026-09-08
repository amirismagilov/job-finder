import ctypes
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import threading
import unittest

from job_finder.config import load_config
from job_finder.keychain import MacOSKeychain, MemorySecrets
from job_finder.mcp_server import handle_message
from job_finder.storage import Storage, WriteGuardError


class ConfigAndStorageTest(unittest.TestCase):
    def test_example_config_loads_with_conservative_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            knowledge, rules = root / "k.md", root / "r.md"
            knowledge.write_text("# Опыт", encoding="utf-8")
            rules.write_text("# Правила", encoding="utf-8")
            config = root / "config.toml"
            config.write_text(f'''[profile]\nknowledge_paths=["{knowledge}"]\ncover_letter_rules_path="{rules}"\n[search]\nqueries=["AI"]\n''', encoding="utf-8")
            loaded = load_config(config)
            self.assertEqual(loaded.safety.daily_application_limit, 5)
            self.assertEqual(loaded.safety.daily_message_limit, 20)
            self.assertEqual(loaded.safety.minimum_write_interval_seconds, 30)
            self.assertEqual(loaded.bridge.host, "127.0.0.1")

    def test_storage_opt_in_dedupe_and_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "private" / "state.sqlite3"
            storage = Storage(path)
            self.assertFalse(storage.autonomy_enabled())
            storage.set_autonomy(True)
            storage.record_vacancy("vacancy-a", "applied", score=90, input_hash="hash")
            storage.mark_message_processed("message-a", "chat-a", "response-a", "hash")
            self.assertTrue(storage.autonomy_enabled())
            self.assertEqual(storage.vacancy_status("vacancy-a"), "applied")
            self.assertTrue(storage.message_processed("message-a"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_write_reservations_own_one_daily_slot_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "state.sqlite3"
            Storage(path).set_autonomy(True)
            barrier = threading.Barrier(2)
            outcomes = []

            def reserve(target):
                storage = Storage(path)
                barrier.wait()
                try:
                    outcomes.append(storage.reserve_write("application", target, daily_limit=1, minimum_interval_seconds=0).target)
                except WriteGuardError as exc:
                    outcomes.append(exc.reason)

            threads = [threading.Thread(target=reserve, args=(f"vacancy-{index}",)) for index in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(5)
            self.assertEqual(len([item for item in outcomes if item.startswith("vacancy-")]), 1)
            self.assertEqual(outcomes.count("application_daily_limit"), 1)

    def test_write_reservation_enforces_minimum_interval_with_fake_clock(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            storage = Storage(Path(name) / "state.sqlite3")
            current = [datetime(2026, 1, 1, tzinfo=UTC)]
            storage.now = lambda: current[0]
            storage.set_autonomy(True)
            first = storage.reserve_write("application", "vacancy-a", daily_limit=5, minimum_interval_seconds=30)
            storage.finish_write(first, success=True)
            current[0] += timedelta(seconds=29)
            with self.assertRaisesRegex(WriteGuardError, "minimum_write_interval"):
                storage.reserve_write("application", "vacancy-b", daily_limit=5, minimum_interval_seconds=30)
            current[0] += timedelta(seconds=1)
            self.assertEqual(storage.reserve_write("application", "vacancy-b", daily_limit=5, minimum_interval_seconds=30).target, "vacancy-b")

    def test_secret_store_delete_removes_binding_in_memory_and_native_contract(self) -> None:
        memory = MemorySecrets()
        memory.set("bridge_extension_id", "extension")
        self.assertTrue(memory.delete("bridge_extension_id"))
        self.assertFalse(memory.delete("bridge_extension_id"))
        self.assertIsNone(memory.get("bridge_extension_id"))

        calls = []

        class Security:
            def SecKeychainFindGenericPassword(self, _keychain, _service_length, _service, _account_length, _account, length, data, item):
                length._obj.value = 0
                data._obj.value = None
                item._obj.value = 42
                return 0

            def SecKeychainItemFreeContent(self, _attributes, _data): return 0

            def SecKeychainItemDelete(self, item):
                calls.append(item.value)
                return 0

        class CoreFoundation:
            def CFRelease(self, item): calls.append(("release", item.value))

        native = MacOSKeychain.__new__(MacOSKeychain)
        native._security = Security()
        native._cf = CoreFoundation()
        native._keychain = ctypes.c_void_p(1)
        native._service = b"test"
        self.assertTrue(native.delete("bridge_extension_id"))
        self.assertEqual(calls, [42, ("release", 42)])


class MCPTest(unittest.TestCase):
    class FakeService:
        def status(self): return {"ok": True}
        def run_cycle(self, *, dry_run): return {"dry_run": dry_run}
        def recent_audit(self, *, limit): return {"limit": limit}

    def test_initialize_and_dry_run_default(self) -> None:
        initialized = handle_message(self.FakeService(), {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(initialized["result"]["serverInfo"]["version"], "0.2.0")
        called = handle_message(self.FakeService(), {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "run_autonomous_cycle", "arguments": {}}})
        self.assertFalse(called["result"]["isError"])
        self.assertIn('"dry_run": true', called["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
