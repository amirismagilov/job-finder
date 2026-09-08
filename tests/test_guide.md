# Offline test guide

Все тесты используют синтетические объекты, `MemorySecrets`, временную SQLite и fake client/LLM. Они не открывают браузер, сокеты к hh.ru и не выполняют live write.

## Команды

```bash
.venv/bin/python -m compileall -q src/job_finder
.venv/bin/python -m pytest -s -v
node --check browser_extension/projectors.js
node --check browser_extension/page_executor.js
node --check browser_extension/background.js
```

Чтобы увидеть LDD-события уровня IMP:7–10:

```bash
.venv/bin/python -c 'import logging,unittest; logging.basicConfig(level=logging.INFO,format="%(message)s"); unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover("tests"))'
```

## Матрица

| Область | Проверяется |
|---|---|
| Typed contract | Запрет URL/headers/raw HTML/path traversal, UUID, exact action params и строгие nested response schemas |
| Safety classifier | 401, 403, 429, CAPTCHA/challenge в body и redirect_path, malformed unread/chat schema, ambiguous write |
| Browser extension | MV3, точные host permissions/action enum, readiness handshake, HAR-fidelity benign apply fields, отсутствие UI automation/remote code |
| Browser projectors | >1 MiB synthetic HTML проходит под 4 MiB raw cap и становится <1 MiB typed projection; CSRF/meta/form/script/JSON поля не выходят из браузера |
| Bridge | Pairing, extension ID binding, одноразовая очередь и bounded response contract без сетевого порта |
| Preflight | Тест вакансии, недоступное резюме и выбор допустимого resume hash |
| AI boundary | Удаление URL/headers, exact/snake_case/camelCase IDs на всех уровнях, unsupported claims, confidence и правило Сбера |
| Autonomy | Dry-run, adversarial disable внутри цикла, hard stop, атомарный daily/interval reservation и guarded MARK_READ |
| Crash recovery | Chat outbox сохраняет UUID/текст до send; crash-after-send retry использует тот же UUID, receipt/processed/action коммитятся одной транзакцией |
| Cadence | Общий read throttler проверяется fake clock/sleep без jitter/stealth |
| Storage/Keychain/MCP | Права SQLite, concurrent reservation, native/MemorySecrets delete, consent state, MCP version и dry-run по умолчанию |

`tests/fixtures/hh_contract_sanitized.json` — ручная обезличенная contract fixture. Добавлять в неё реальные IDs, тексты сообщений, headers, cookies, CSRF или response bodies запрещено.
