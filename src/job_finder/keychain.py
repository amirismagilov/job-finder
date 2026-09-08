from __future__ import annotations

import ctypes
import ctypes.util
import platform


SERVICE = "ru.hh.amir-job-finder"


class KeychainError(RuntimeError):
    pass


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
