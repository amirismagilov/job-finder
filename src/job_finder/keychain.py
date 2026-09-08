from __future__ import annotations

# region MODULE_CONTRACT [DOMAIN(10): LocalSecrets; CONCEPT(10): NonArgvKeychain, Deletion; TECH(9): macOS Security.framework]
## @file keychain.py
## @brief Native macOS Keychain and in-memory test secret stores.
## @modulecontract
## @purpose Keep pairing and provider secrets outside configuration, process arguments and logs, including reliable removal of stale bindings.
## @scope Generic-password get, set, delete and presence checks.
## @input Account names and secret values supplied through hidden local input.
## @output SecretStore-compatible values and deletion status.
## @invariants Secret values never enter argv or logs; deletion removes the item rather than storing an ambiguous empty value.
## @changes LAST_CHANGE: [v0.2.1 — Added native Keychain and MemorySecrets deletion for clean extension re-pairing.]
## @modulemap
## CLASS 10[Native Keychain secret lifecycle] => MacOSKeychain
## CLASS 8[Deterministic test secret lifecycle] => MemorySecrets
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: macOS Keychain, Security.framework, secret delete, pairing reset, MemorySecrets
# STRUCTURE: hidden input -> native generic password item -> get/set/delete without argv -> paired runtime

import ctypes
import ctypes.util
import platform


SERVICE = "ru.hh.amir-job-finder"


class KeychainError(RuntimeError):
    pass


# region CLASS_MacOSKeychain [DOMAIN(10): LocalSecrets; CONCEPT(10): NativeSecretLifecycle; TECH(9): Security.framework]
## @purpose Manage private generic-password entries without shelling out or exposing their values in process arguments.
class MacOSKeychain:
    """Минимальная обёртка над Security.framework без передачи секретов в argv."""

    ERR_SEC_ITEM_NOT_FOUND = -25300

    def __init__(self, service: str = SERVICE) -> None:
        if platform.system() != "Darwin":
            raise KeychainError("Хранилище поддерживает только macOS Keychain")
        security_path = ctypes.util.find_library("Security")
        core_foundation_path = ctypes.util.find_library("CoreFoundation")
        if not security_path or not core_foundation_path:
            raise KeychainError("Не найден macOS Security.framework")
        self._security = ctypes.CDLL(security_path)
        self._cf = ctypes.CDLL(core_foundation_path)
        self._service = service.encode("utf-8")
        self._configure_signatures()
        self._keychain = ctypes.c_void_p()
        status = self._security.SecKeychainCopyDefault(ctypes.byref(self._keychain))
        if status != 0 or not self._keychain:
            raise KeychainError(f"Не удалось открыть default Keychain: OSStatus {status}")

    def _configure_signatures(self) -> None:
        void_p = ctypes.c_void_p
        uint32 = ctypes.c_uint32
        self._security.SecKeychainCopyDefault.argtypes = [ctypes.POINTER(void_p)]
        self._security.SecKeychainCopyDefault.restype = ctypes.c_int32
        self._security.SecKeychainFindGenericPassword.argtypes = [
            void_p,
            uint32,
            ctypes.c_char_p,
            uint32,
            ctypes.c_char_p,
            ctypes.POINTER(uint32),
            ctypes.POINTER(void_p),
            ctypes.POINTER(void_p),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [
            void_p,
            uint32,
            ctypes.c_char_p,
            uint32,
            ctypes.c_char_p,
            uint32,
            void_p,
            ctypes.POINTER(void_p),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyAttributesAndData.argtypes = [
            void_p,
            void_p,
            uint32,
            void_p,
        ]
        self._security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [void_p]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [void_p, void_p]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._cf.CFRelease.argtypes = [void_p]
        self._cf.CFRelease.restype = None

    def get(self, account: str) -> str | None:
        account_b = account.encode("utf-8")
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            self._keychain,
            len(self._service),
            self._service,
            len(account_b),
            account_b,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        if status == self.ERR_SEC_ITEM_NOT_FOUND:
            return None
        if status != 0:
            raise KeychainError(f"Keychain не вернул секрет {account!r}: OSStatus {status}")
        try:
            raw = ctypes.string_at(data, length.value)
            return raw.decode("utf-8")
        finally:
            self._security.SecKeychainItemFreeContent(None, data)
            if item:
                self._cf.CFRelease(item)

    def set(self, account: str, value: str) -> None:
        account_b = account.encode("utf-8")
        value_b = value.encode("utf-8")
        item = ctypes.c_void_p()
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            self._keychain,
            len(self._service),
            self._service,
            len(account_b),
            account_b,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        if status == 0:
            self._security.SecKeychainItemFreeContent(None, data)
            try:
                buffer = ctypes.create_string_buffer(value_b)
                status = self._security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(value_b), ctypes.cast(buffer, ctypes.c_void_p)
                )
            finally:
                if item:
                    self._cf.CFRelease(item)
        elif status == self.ERR_SEC_ITEM_NOT_FOUND:
            buffer = ctypes.create_string_buffer(value_b)
            status = self._security.SecKeychainAddGenericPassword(
                self._keychain,
                len(self._service),
                self._service,
                len(account_b),
                account_b,
                len(value_b),
                ctypes.cast(buffer, ctypes.c_void_p),
                None,
            )
        if status != 0:
            raise KeychainError(f"Не удалось сохранить секрет {account!r}: OSStatus {status}")

    def has(self, account: str) -> bool:
        return self.get(account) is not None

    # region METHOD_delete [DOMAIN(10): LocalSecrets; CONCEPT(10): ExplicitRemoval; TECH(9): SecKeychainItemDelete]
    ## @purpose Remove a stale account binding completely so an empty string cannot be mistaken for a valid stored secret.
    ## @io account str -> bool existed
    ## @complexity 6
    def delete(self, account: str) -> bool:
        """find native item -> free copied password bytes -> delete item -> release handle."""
        account_b = account.encode("utf-8")
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            self._keychain,
            len(self._service),
            self._service,
            len(account_b),
            account_b,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        if status == self.ERR_SEC_ITEM_NOT_FOUND:
            return False
        if status != 0:
            raise KeychainError(f"Keychain не нашёл секрет {account!r} для удаления: OSStatus {status}")
        self._security.SecKeychainItemFreeContent(None, data)
        try:
            # BUG_FIX_CONTEXT: Writing an empty bridge_extension_id left a present-but-empty Keychain
            # record; native deletion restores the unbound state without overloading secret values.
            status = self._security.SecKeychainItemDelete(item)
        finally:
            if item:
                self._cf.CFRelease(item)
        if status != 0:
            raise KeychainError(f"Не удалось удалить секрет {account!r}: OSStatus {status}")
        return True
    # endregion METHOD_delete
# endregion CLASS_MacOSKeychain


# region CLASS_MemorySecrets [DOMAIN(8): Tests; CONCEPT(9): SecretStoreDouble; TECH(7): Dict]
## @purpose Mirror the production secret lifecycle for offline tests without touching the operating-system Keychain.
class MemorySecrets:
    """Тестовое хранилище с тем же интерфейсом."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self.values.get(account)

    def set(self, account: str, value: str) -> None:
        self.values[account] = value

    def has(self, account: str) -> bool:
        return account in self.values

    def delete(self, account: str) -> bool:
        return self.values.pop(account, None) is not None
# endregion CLASS_MemorySecrets
