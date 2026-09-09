# job-finder 0.2.0

Локальный автономный помощник для hh.ru. ИИ выполняет только три задачи: оценивает релевантность вакансии, пишет сопроводительное и формирует ответ рекрутеру. Поиск, preflight, отправка, лимиты, дедупликация и остановка принадлежат детерминированному Python-коду.

Публичный соискательский API больше не является основой проекта. Действующую web-сессию использует локально загруженное Chromium Manifest V3 расширение. Оно не нажимает элементы страницы, не ищет DOM-элементы и не эмулирует пользователя: расширение выполняет только восемь заранее заданных HTTP-операций. Cookies не покидают браузер.

Это интеграция с неподдерживаемым web-контрактом hh.ru. Он может измениться, а риск ограничения аккаунта нельзя исключить. Проект не обходит CAPTCHA, challenge, rate limit, fingerprint и антибот-защиту: при любой такой проверке включается hard stop.

## Архитектура

```text
планировщик → deterministic policy → AI relevance/text → typed command
                                                       ↓
SQLite audit ← semantic result ← loopback bridge ← Chromium extension
                                                       ↓
                                      fixed hh.ru/chatik.hh.ru request
```

- Backend принимает только `WebAction` и точные схемы параметров — передать URL, метод или заголовки невозможно.
- Pairing secret и ключ LLM хранятся в macOS Keychain; session Cookie и CSRF остаются в браузере.
- SQLite имеет права `0600`, хранит opt-in, hard stop, атомарные write-reservations, идемпотентный chat outbox, дедупликацию и аудит.
- По умолчанию разрешено не более 5 откликов и 20 сообщений в сутки, между любыми записями — минимум 30 секунд.
- Непосредственно перед каждой записью SQLite-транзакция заново проверяет opt-in, hard stop, суточный лимит и интервал, поэтому `autonomy disable` останавливает ещё не начатые write-действия текущего цикла.
- До однократного opt-in любой цикл является dry-run. После opt-in подтверждение каждого действия не требуется.

## Установка

Нужны macOS, Python 3.11+ и Chromium/Chrome.

```bash
cd /Users/macos/job-finder
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-build-isolation -e .
.venv/bin/job-finder status
```

Локальный `config.toml` указывает на три базы знаний в `/Users/macos/Downloads`. Файлы читаются динамически и не копируются в проект. Проверьте секции `[search]`, `[llm]` и `[autonomy]` по [config.example.toml](config.example.toml).

Для локального OpenAI-compatible сервера оставьте loopback endpoint и не задавайте `reasoning_effort`: запрос сохранит совместимый режим `temperature=0.1`. Для официального OpenAI API используйте Chat Completions endpoint и выбранные модель/глубину reasoning:

```toml
[llm]
endpoint = "https://api.openai.com/v1/chat/completions"
model = "gpt-5.6-sol"
reasoning_effort = "xhigh"
timeout_seconds = 180
minimum_confidence = 0.75
```

Удалённому провайдеру разрешён только HTTPS. Используется отдельный Platform API key; авторизация Codex не переиспользуется. Ключ вводится скрыто и сохраняется только в macOS Keychain:

```bash
.venv/bin/job-finder llm set-key
```

## Расширение и pairing

1. Откройте `chrome://extensions`, включите режим разработчика и выберите «Загрузить распакованное расширение».
2. Укажите каталог `/Users/macos/job-finder/browser_extension`.
3. На странице настроек расширения нажмите «Создать новый», «Скопировать секрет», затем «Сохранить и включить». Если системный буфер обмена недоступен, кнопка «Показать» позволяет скопировать значение вручную.
4. Выполните `.venv/bin/job-finder bridge pair` и вставьте тот же секрет в скрытый ввод.
5. Во время запущенного bridge кнопка «Проверить соединение» покажет безопасный статус без вывода секрета. Расширение само проверит готовность content script через ограниченный handshake; если старая вкладка была открыта до установки, она будет один раз перезагружена без кликов по сайту.

Расширение имеет доступ только к `https://hh.ru/*`, `https://chatik.hh.ru/*` и `http://127.0.0.1:8766/*`. Оно не содержит удалённого кода, произвольных запросов, Playwright, Selenium, CDP, селекторов или кликов.

## Безопасный ввод в эксплуатацию

Сначала войдите в hh.ru обычным способом и выполните dry-run:

```bash
.venv/bin/job-finder run once
.venv/bin/job-finder audit --limit 30
```

Проверьте найденные вакансии и локальный аудит. Затем один раз включите автономные записи:

```bash
.venv/bin/job-finder autonomy enable
```

CLI потребует точную фразу `ВКЛЮЧИТЬ АВТОНОМНЫЙ РЕЖИМ`. После этого:

```bash
.venv/bin/job-finder run once --live
.venv/bin/job-finder daemon run
```

Отключение мгновенное:

```bash
.venv/bin/job-finder autonomy disable
```

Для фонового запуска команда `.venv/bin/job-finder daemon install-launch-agent` создаёт приватный plist, но не загружает его автоматически. Сначала обязательно проверьте несколько dry-run циклов.

## Поведение safety

Запись блокируется при 401/403/429, login redirect, CAPTCHA/challenge, неизвестной схеме или неоднозначном результате write-запроса. Неидемпотентный отклик не повторяется после неоднозначного исхода. Для чата UUID и точный текст сохраняются до отправки: после аварийного перезапуска повтор использует тот же UUID, чтобы hh.ru вернул тот же логический receipt. После ручной проверки аккаунта:

```bash
.venv/bin/job-finder safety clear-stop
```

Preflight пропускает уже обработанные вакансии, тесты, дополнительные/внешние вопросы, `responseImpossible`, незавершённое или недоступное резюме. Если popup явно возвращает `countryIds`, они передаются как несекретное preflight-поле; код не хардкодит IDs и опускает поле, если его нет. Живая обязательность этого поля остаётся неподтверждённой до осторожного незаписывающего preflight на текущей схеме hh.ru. Ответ чата получает UUID один раз на исходное recruiter message и связывается с ним в persistent outbox.

Сырой HTML поиска и вакансии может быть до 4 MiB, но обрабатывается только внутри расширения. В backend уходит только строго типизированная проекция публичных полей менее 1 MiB; popup/chat JSON так же пропускаются через action-specific allowlist.

Модель получает только описание вакансии/историю конкретного чата и релевантные фрагменты трёх локальных файлов. URL и transport-поля удаляются. Структурированный результат содержит `used_facts`, `unknowns`, `unsupported_claims`, `grounded` и `confidence`. Неподтверждённый результат блокируется. Для работодателей Сбера запрещено называть «Истру» и Сбер Университет.

## MCP

```bash
codex mcp add job-finder -- /Users/macos/job-finder/.venv/bin/job-finder-mcp
codex mcp get job-finder
```

Инструменты: `status`, `run_autonomous_cycle` и `recent_audit`. MCP запускает cycle в dry-run по умолчанию. Включение автономии намеренно доступно только через локальную CLI.

## Проверка

```bash
.venv/bin/python -m compileall -q src/job_finder
.venv/bin/python -m unittest discover -s tests -v
```

Тесты полностью офлайн: реальные HAR, cookies, аккаунт, LLM и hh.ru не используются. Подробная матрица — в [tests/test_guide.md](tests/test_guide.md).
