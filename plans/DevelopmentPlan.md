$START_JOB_FINDER_AUTONOMOUS_HTTP_ROBOT_PLAN

**PURPOSE:** Перестроить существующий локальный MCP-проект в автономного робота для hh.ru без Playwright и без управления интерфейсом: браузерное расширение сохраняет сессию внутри браузера и выполняет только разрешённые HTTP-действия, локальный backend принимает решения, вызывает ИИ для оценки и текстов, применяет safety-политику и ведёт аудит.
**SCOPE:** Расширение Chromium Manifest V3, защищённый loopback-мост, web-контракт hh.ru из обезличенных HAR-схем, автономный цикл, адаптер ИИ, хранение состояния, CLI/MCP, тесты и документация.
**KEYWORDS:** hh.ru, Chromium extension, loopback bridge, private web contract, autonomous worker, LLM grounding, Keychain, SQLite, safety stop, HAR redaction

$START_DOCUMENT_PLAN
### Document Plan

**SECTION_GOALS:**
- GOAL [Заменить недоступный соискательский API на ограниченный браузерный HTTP-мост без Playwright] => GOAL_WEB_TRANSPORT
- GOAL [Разрешить автономные отклики и ответы после однократного локального включения] => GOAL_AUTONOMY
- GOAL [Ограничить ИИ поиском релевантности и формированием текстов на основе подтверждённых данных] => GOAL_AI_BOUNDARY
- GOAL [Не допустить утечки сессии, повторной отправки и обхода защит hh.ru] => GOAL_SECURITY

**SECTION_USE_CASES:**
- USE_CASE [Пользователь -> один раз подключает расширение и включает автономный режим -> робот выполняет допустимые действия без подтверждения каждого отклика] => SCENARIO_ENABLE
- USE_CASE [Планировщик -> получает вакансии, оценивает их и отправляет подходящий отклик -> действие фиксируется в аудите] => SCENARIO_APPLY
- USE_CASE [Планировщик -> читает новые сообщения, формирует grounded-ответ и отправляет его -> сообщение фиксируется идемпотентно] => SCENARIO_CHAT
- USE_CASE [hh.ru -> возвращает защитную проверку или неизвестную схему -> робот прекращает записи и уведомляет пользователя] => SCENARIO_HARD_STOP

$END_DOCUMENT_PLAN

$START_DEV_PLAN

**PURPOSE:** Дать реализации и проверке однозначную схему перехода от официального API и двухшаговых подтверждений к локальному автономному роботу с браузерным session bridge.

---

### 1. Draft Code Graph (Pre-Code Design Artifact)

```xml
<DraftCodeGraph>
  <AmirJobFinder_0_2_0_Info TYPE="PROJECT_INFO">
    <keywords>hh.ru, autonomous applications, recruiter chat, Chromium bridge, local AI orchestration</keywords>
    <terms>BrowserSessionBridge, AllowedAction, AutonomousCycle, GroundedGeneration, SafetyStop</terms>
    <annotation>Локальный робот ищет вакансии, формирует сопроводительные и ответы, а разрешённые HTTP-запросы выполняет внутри действующей браузерной сессии без управления интерфейсом.</annotation>
    <BusinessScenarios>
      <Scenario NAME="PairBrowser">Пользователь -> вводит одноразовый pairing secret в расширении -> браузер и локальный backend устанавливают доверенный канал</Scenario>
      <Scenario NAME="ApplyAutonomously">Планировщик -> ранжирует вакансию -> ИИ создаёт письмо -> валидатор разрешает отправку -> расширение выполняет отклик</Scenario>
      <Scenario NAME="AnswerRecruiter">Планировщик -> получает непрочитанный вопрос -> ИИ создаёт grounded-ответ -> валидатор разрешает отправку -> расширение отправляет сообщение</Scenario>
      <Scenario NAME="StopOnProtection">Транспорт -> обнаруживает CAPTCHA, 401, 403, 429 или неизвестную схему -> включает hard stop -> записи прекращаются</Scenario>
    </BusinessScenarios>
  </AmirJobFinder_0_2_0_Info>

  <web_contract_py FILE="src/job_finder/web_contract.py" TYPE="WEB_CONTRACT_MODULE">
    <keywords>allowlist, typed command, schema validation, HAR-derived fields</keywords>
    <terms>WebAction, CommandEnvelope, ResponseEnvelope</terms>
    <annotation>Описывает только разрешённые операции и поля; произвольные URL, методы и заголовки отсутствуют в публичном интерфейсе.</annotation>
    <web_contract_WebAction_CLASS NAME="WebAction" TYPE="IS_CLASS_OF_MODULE">
      <annotation>Перечисляет SEARCH_VACANCIES, GET_VACANCY, GET_RESPONSE_POPUP, APPLY, LIST_CHATS, GET_CHAT_DATA, SEND_CHAT_MESSAGE и MARK_READ.</annotation>
    </web_contract_WebAction_CLASS>
    <web_contract_validate_command_FUNC NAME="validate_command" TYPE="VALIDATION_FUNCTION">
      <annotation>Проверяет action, идентификаторы, размеры текста и точный набор полей до постановки команды в очередь.</annotation>
    </web_contract_validate_command_FUNC>
    <web_contract_classify_response_FUNC NAME="classify_response" TYPE="SAFETY_CLASSIFIER_FUNCTION">
      <annotation>Распознаёт успех, истёкшую сессию, защитную проверку, неоднозначный результат записи и дрейф контракта.</annotation>
    </web_contract_classify_response_FUNC>
  </web_contract_py>

  <bridge_server_py FILE="src/job_finder/bridge_server.py" TYPE="LOCAL_TRANSPORT_MODULE">
    <keywords>127.0.0.1, pairing, command queue, bearer secret, redaction</keywords>
    <terms>BrowserBridgeServer, BridgeQueue</terms>
    <annotation>Предоставляет расширению аутентифицированный loopback-канал и никогда не принимает произвольные сетевые назначения.</annotation>
    <bridge_server_BrowserBridgeServer_CLASS NAME="BrowserBridgeServer" TYPE="IS_CLASS_OF_MODULE">
      <annotation>Выдаёт расширению ожидающую разрешённую команду и принимает ограниченный ответ с лимитом размера.</annotation>
      <bridge_server_BrowserBridgeServer_enqueue_METHOD NAME="enqueue" TYPE="IS_METHOD_OF_CLASS">
        <annotation>Ставит валидированную команду и ожидает ограниченное время.</annotation>
        <CrossLinks>
          <Link TARGET="web_contract_validate_command_FUNC" TYPE="CALLS_FUNCTION" />
          <Link TARGET="web_contract_classify_response_FUNC" TYPE="CALLS_FUNCTION" />
        </CrossLinks>
      </bridge_server_BrowserBridgeServer_enqueue_METHOD>
    </bridge_server_BrowserBridgeServer_CLASS>
  </bridge_server_py>

  <web_client_py FILE="src/job_finder/web_client.py" TYPE="HH_WEB_ADAPTER_MODULE">
    <keywords>search HTML, response popup, resume hash, chatik, idempotency</keywords>
    <terms>HHWebClient, VacancyParser, ChatParser</terms>
    <annotation>Преобразует бизнес-вызовы в разрешённые команды расширения и нормализует HAR-подтверждённые ответы hh.ru.</annotation>
    <web_client_HHWebClient_CLASS NAME="HHWebClient" TYPE="IS_CLASS_OF_MODULE">
      <annotation>Предоставляет search_vacancies, get_vacancy, suitable_resumes, apply, list_chats, chat_messages и send_chat_message.</annotation>
      <web_client_HHWebClient_apply_METHOD NAME="apply" TYPE="IS_METHOD_OF_CLASS">
        <annotation>Сначала получает popup-state, выбирает допустимый resume_hash и затем отправляет multipart-отклик.</annotation>
        <CrossLinks>
          <Link TARGET="bridge_server_BrowserBridgeServer_enqueue_METHOD" TYPE="CALLS_METHOD" />
        </CrossLinks>
      </web_client_HHWebClient_apply_METHOD>
      <web_client_HHWebClient_send_chat_message_METHOD NAME="send_chat_message" TYPE="IS_METHOD_OF_CLASS">
        <annotation>Отправляет chatId, UUID idempotencyKey и проверенный текст через chatik.</annotation>
        <CrossLinks>
          <Link TARGET="bridge_server_BrowserBridgeServer_enqueue_METHOD" TYPE="CALLS_METHOD" />
        </CrossLinks>
      </web_client_HHWebClient_send_chat_message_METHOD>
    </web_client_HHWebClient_CLASS>
  </web_client_py>

  <llm_py FILE="src/job_finder/llm.py" TYPE="AI_PROVIDER_MODULE">
    <keywords>OpenAI-compatible, structured JSON, grounded context, confidence, redaction</keywords>
    <terms>LLMProvider, LLMDecision, GroundingValidator</terms>
    <annotation>Инкапсулирует внешний или локальный OpenAI-compatible endpoint; передаёт только релевантные фрагменты знаний и требует структурированный результат.</annotation>
    <llm_LLMProvider_CLASS NAME="LLMProvider" TYPE="IS_CLASS_OF_MODULE">
      <annotation>Формирует оценку вакансии, сопроводительное письмо и ответ чата по отдельным строгим схемам.</annotation>
    </llm_LLMProvider_CLASS>
    <llm_validate_grounded_text_FUNC NAME="validate_grounded_text" TYPE="AI_OUTPUT_VALIDATOR">
      <annotation>Запрещает пустой, чрезмерный, неуверенный или содержащий заявленный моделью неподтверждённый факт текст.</annotation>
    </llm_validate_grounded_text_FUNC>
  </llm_py>

  <autonomy_py FILE="src/job_finder/autonomy.py" TYPE="ORCHESTRATION_MODULE">
    <keywords>scheduler, deterministic policy, discovery, applications, unread chats, hard stop</keywords>
    <terms>AutonomousWorker, RunReport, PolicyDecision</terms>
    <annotation>Координирует цикл, но не разрешает ИИ выбирать URL, заголовки, лимиты или обход защит.</annotation>
    <autonomy_AutonomousWorker_CLASS NAME="AutonomousWorker" TYPE="IS_CLASS_OF_MODULE">
      <annotation>Выполняет один детерминированный цикл поиска, откликов и обработки новых сообщений.</annotation>
      <autonomy_AutonomousWorker_run_cycle_METHOD NAME="run_cycle" TYPE="IS_METHOD_OF_CLASS">
        <annotation>Проверяет opt-in и hard stop, выполняет чтения, вызывает ИИ, валидирует и записывает результаты.</annotation>
        <CrossLinks>
          <Link TARGET="web_client_HHWebClient_CLASS" TYPE="USES_API" />
          <Link TARGET="llm_LLMProvider_CLASS" TYPE="USES_API" />
          <Link TARGET="storage_py" TYPE="READS_AND_WRITES_DATA" />
        </CrossLinks>
      </autonomy_AutonomousWorker_run_cycle_METHOD>
    </autonomy_AutonomousWorker_CLASS>
  </autonomy_py>

  <browser_extension_manifest_json FILE="browser_extension/manifest.json" TYPE="CHROMIUM_EXTENSION_MANIFEST">
    <keywords>Manifest V3, least privilege, hh.ru, chatik.hh.ru, localhost</keywords>
    <terms>host_permissions, background service worker, content script</terms>
    <annotation>Разрешает только hh.ru, chatik.hh.ru и фиксированный loopback endpoint; не содержит удалённого кода.</annotation>
  </browser_extension_manifest_json>

  <browser_extension_background_js FILE="browser_extension/background.js" TYPE="SESSION_BRIDGE_FRONTEND">
    <keywords>polling, pairing, action allowlist, extension storage</keywords>
    <terms>pollCommand, dispatchAllowedAction, submitResult</terms>
    <annotation>Получает типизированную команду с localhost, выполняет только заранее описанное действие и возвращает ограниченный результат.</annotation>
    <browser_extension_dispatchAllowedAction_FUNC NAME="dispatchAllowedAction" TYPE="EXTENSION_CONTROLLER">
      <annotation>Сопоставляет enum действия с фиксированным path, методом и допустимыми полями.</annotation>
      <CrossLinks>
        <Link TARGET="browser_extension_page_executor_js" TYPE="INJECTS_FUNCTION" />
      </CrossLinks>
    </browser_extension_dispatchAllowedAction_FUNC>
  </browser_extension_background_js>

  <browser_extension_page_executor_js FILE="browser_extension/page_executor.js" TYPE="SAME_ORIGIN_EXECUTOR">
    <keywords>same-origin fetch, credentials include, CSRF, response size limit</keywords>
    <terms>executeHhAction, parseXsrf</terms>
    <annotation>Выполняется в контексте hh.ru или chatik.hh.ru, использует действующую сессию браузера и не экспортирует Cookie.</annotation>
  </browser_extension_page_executor_js>

  <config_py FILE="src/job_finder/config.py" TYPE="CONFIGURATION_MODULE">
    <keywords>autonomy policy, bridge port, LLM endpoint, thresholds, intervals</keywords>
    <terms>AutonomyConfig, BridgeConfig, LLMConfig</terms>
    <annotation>Добавляет консервативные настройки автономии, не сохраняя секреты.</annotation>
  </config_py>

  <storage_py FILE="src/job_finder/storage.py" TYPE="STATE_AND_AUDIT_MODULE">
    <keywords>SQLite, migration, deduplication, message cursor, audit, hard stop</keywords>
    <terms>autonomy_state, vacancy_decisions, processed_messages, bridge_commands</terms>
    <annotation>Хранит opt-in, дедупликацию, курсоры чатов, команды, результаты и safety-state с правами 0600.</annotation>
  </storage_py>

  <cli_py FILE="src/job_finder/cli.py" TYPE="LOCAL_CONTROL_INTERFACE">
    <keywords>pair, daemon, dry run, enable autonomy, disable autonomy, status</keywords>
    <terms>bridge pair, run once, autonomy enable</terms>
    <annotation>Предоставляет локальную настройку без вывода cookies, API-ключей или HAR-содержимого.</annotation>
  </cli_py>

  <mcp_server_py FILE="src/job_finder/mcp_server.py" TYPE="MCP_CONTROL_INTERFACE">
    <keywords>status, dry run, run report, hard stop</keywords>
    <terms>MCP tools, local stdio</terms>
    <annotation>Сохраняет наблюдение и ручной запуск цикла, но не является обязательным посредником автономной работы.</annotation>
  </mcp_server_py>

  <tests_test_web_contract_py FILE="tests/test_web_contract.py" TYPE="BACKEND_TEST_MODULE">
    <keywords>allowlist, schema drift, security response, LDD logs</keywords>
    <annotation>Проверяет запрет произвольных URL и корректную классификацию HAR-подтверждённых ответов.</annotation>
  </tests_test_web_contract_py>

  <tests_test_autonomy_py FILE="tests/test_autonomy.py" TYPE="BACKEND_TEST_MODULE">
    <keywords>dry run, opt-in, deduplication, grounded AI, safety stop, idempotency</keywords>
    <annotation>Эмулирует полный цикл без реального hh.ru и внешнего ИИ.</annotation>
  </tests_test_autonomy_py>

  <tests_test_extension_contract_py FILE="tests/test_extension_contract.py" TYPE="FRONTEND_CONTRACT_TEST_MODULE">
    <keywords>manifest permissions, JS allowlist, no arbitrary URL, no remote code</keywords>
    <annotation>Статически проверяет минимальные разрешения и соответствие action enum между Python и расширением.</annotation>
  </tests_test_extension_contract_py>

  <ProjectCrossLinks TYPE="MODULE_INTERACTIONS_OVERVIEW">
    <Link TARGET="autonomy_AutonomousWorker_run_cycle_METHOD" TYPE="ORCHESTRATES_FLOW" />
    <Link TARGET="bridge_server_BrowserBridgeServer_CLASS" TYPE="CONNECTS_BACKEND_TO_BROWSER" />
    <Link TARGET="browser_extension_dispatchAllowedAction_FUNC" TYPE="EXECUTES_ALLOWED_COMMANDS" />
    <Link TARGET="llm_validate_grounded_text_FUNC" TYPE="GUARDS_AI_OUTPUT" />
  </ProjectCrossLinks>
</DraftCodeGraph>
```

---

### 2. Step-by-step Data Flow

1. **Pairing:** `job-finder bridge pair` создаёт случайный pairing secret, сохраняет его в macOS Keychain и показывает пользователю для однократного ввода в локально загруженное расширение. Расширение сохраняет secret в `chrome.storage.local`; cookies и CSRF в backend не передаются.
2. **Extension poll:** расширение с фиксированным интервалом обращается только к `127.0.0.1` и предъявляет pairing secret. Bridge проверяет secret постоянным временем, origin/extension identity после pairing и лимит запроса.
3. **Command issue:** backend создаёт `CommandEnvelope` с enum действия и валидированными параметрами. URL, Cookie, произвольные заголовки и JavaScript в envelope отсутствуют.
4. **Same-origin execution:** расширение выбирает или открывает фоновую вкладку нужного origin и запускает локально упакованный executor. Executor строит path из собственной allowlist, выполняет `fetch(..., credentials="include")`, берёт CSRF внутри браузера и возвращает status, content type и ограниченное тело ответа.
5. **Protection boundary:** расширение не создаёт `x-gib-*`, fingerprint, CAPTCHA-токены и не повторяет защитные запросы из HAR. Любой 401, 403, 429, CAPTCHA/challenge-маркер, редирект на login либо неизвестная схема классифицируются как hard stop.
6. **Vacancy discovery:** worker запрашивает web-поиск по конфигурационным запросам, парсер извлекает публичные карточки и идентификаторы, удаляет повторы и исключает уже обработанные вакансии.
7. **AI relevance:** для ограниченного числа кандидатов в модель передаются описание вакансии и релевантные фрагменты локальной базы знаний. Модель возвращает строгий JSON: score, reasons, gaps, decision и confidence. Детерминированная политика применяет порог и запреты независимо от модели.
8. **Cover letter:** для прошедшей вакансии модель получает правила сопроводительных и подтверждённые факты. Валидатор проверяет длину из popup-state, отсутствие объявленных моделью неподтверждённых фактов, обязательные поля и правило конфиденциальности для Сбера.
9. **Application preflight:** client получает `/applicant/vacancy_response/popup`, проверяет `alreadyApplied`, `responseImpossible`, тесты, внешнюю анкету, допустимые резюме и актуальный `resume_hash`. Вакансии с тестами или внешней формой пропускаются и записываются в аудит.
10. **Application write:** при включённой автономии, отсутствии hard stop и непревышенном дневном лимите отправляется HAR-подтверждённый multipart-набор полей. HTTP 200 не считается успехом сам по себе: парсер обязан подтвердить семантический результат. Неоднозначность немедленно включает hard stop.
11. **Chat polling:** worker получает `/chatik/api/chats`, затем `chat_data` только для новых или непрочитанных диалогов. `processed_messages` не допускает повторной обработки одного recruiter message ID.
12. **AI chat answer:** модель получает историю конкретного чата, вакансию и релевантные подтверждённые знания. Результат содержит text, confidence, used_facts и unknowns. Неизвестный или чувствительный вопрос получает безопасную нейтральную формулировку и локальный флаг внимания, но не выдуманный факт.
13. **Chat write:** разрешённый текст отправляется с новым UUID `idempotencyKey`; success подтверждается наличием нового message ID. Затем исходный recruiter message ID атомарно отмечается обработанным.
14. **Audit and limits:** SQLite фиксирует решения, хеши входов, отправленный текст, IDs и результат. По умолчанию: dry-run, 5 откликов в сутки, 20 сообщений в сутки, последовательные записи и минимальный интервал 30 секунд.
15. **Autonomy consent:** `job-finder autonomy enable` требует одну точную локальную фразу и записывает opt-in. После этого отдельное подтверждение каждого действия не требуется. `disable`, hard stop или отсутствие расширения мгновенно запрещают записи.
16. **Daemon:** `job-finder daemon run` поддерживает bridge и расписание; шаблон LaunchAgent устанавливается отдельной CLI-командой. Запуск реальных записей невозможен до pairing, настройки ИИ и opt-in.
17. **Testing:** pytest вызывает backend напрямую с fake bridge/fake LLM, выводит отфильтрованные `[IMP:7-10]` записи, проверяет негативные сценарии. Расширение проверяется статически и контрактно без запуска браузера; реальные отклики в тестах не отправляются.

---

### 3. Acceptance Criteria

- [ ] **Transport:** проект больше не вызывает удалённые соискательские OAuth-методы `/negotiations`, `/resumes/mine` и `/common/chats` как основу рабочего режима.
- [ ] **No Playwright:** в зависимостях и коде отсутствуют Playwright, Selenium, CDP-управление, поиск DOM-элементов и эмуляция кликов.
- [ ] **Session containment:** Cookie и полные session credentials не покидают браузер, не попадают в stdout/stderr, SQLite, конфиг, git и LLM-запросы.
- [ ] **Least privilege:** bridge принимает только перечисленные action enum и отклоняет произвольные URL, методы, заголовки, path traversal и неизвестные параметры.
- [ ] **HAR hygiene:** исходные HAR не копируются в проект; тестовые fixtures содержат только обезличенные схемы, методы, пути и имена полей.
- [ ] **Search:** робот получает и дедуплицирует вакансии web-поиска, после чего ИИ возвращает структурированную релевантность с причинами и пробелами.
- [ ] **Grounding:** письмо и chat-answer создаются только из вакансии, истории чата и трёх указанных файлов; модель обязана явно вернуть unknowns, а валидатор блокирует непроверенный результат.
- [ ] **Confidentiality:** автоматические тексты соблюдают специальное правило анонимизации кейсов для Сбера и не выдумывают опыт или метрики.
- [ ] **Application preflight:** повторный отклик, тест, внешняя форма, недоступное резюме и `responseImpossible` не приводят к write-запросу.
- [ ] **Autonomy:** после однократного `autonomy enable` успешные допустимые вакансии и ответы отправляются без подтверждения каждого действия; до opt-in действует только dry-run.
- [ ] **Idempotency:** повтор цикла не дублирует отклик или ответ; chat send всегда содержит уникальный UUID и связывается с исходным message ID.
- [ ] **Limits:** дневные лимиты, минимальный интервал и последовательная запись применяются независимо от решения ИИ.
- [ ] **Hard stop:** 401, 403, 429, login redirect, CAPTCHA/challenge, неизвестная схема и неоднозначный результат записи блокируют последующие записи до ручного снятия.
- [ ] **AI boundary:** модель не получает сетевые URL-команды, Cookie, CSRF, pairing secret или право менять safety-политику.
- [ ] **Observability:** status и run report показывают pairing, dry-run/opt-in, счётчики, пропуски и hard stop без секретов и полного персонального контекста.
- [ ] **Tests:** offline-набор покрывает полный happy path, dry-run, disabled autonomy, dedupe, stale session, CAPTCHA/403/429, malformed response, hallucination/unknown, лимиты и ambiguous write.
- [ ] **LDD:** ключевые тесты выводят `[IMP:7-10]` логи, а фактическая последовательность совпадает с контрактами.
- [ ] **Extension contract:** Manifest V3 не содержит удалённого кода и имеет разрешения только для `hh.ru`, `chatik.hh.ru` и фиксированного loopback origin.
- [ ] **Documentation:** README и SECURITY описывают установку расширения, pairing, настройку OpenAI-compatible/local LLM, dry-run, opt-in, остаточный риск и процедуру остановки.
- [ ] **Architecture index:** изменённые Python-файлы имеют Doxygen semantic exoskeleton; `Doxyfile` генерирует XML в `doxygen_output/xml/`; отдельный `AppGraph.xml` не создаётся.
- [ ] **Verification safety:** автоматические тесты не выполняют live write-запросы в hh.ru и не используют реальные данные из HAR.

$END_DEV_PLAN

$START_SECTION_DECISIONS
### Implementation Decisions and Constraints

$START_ARTIFACT_ARCHITECTURE_DECISION
#### Browser session bridge without UI automation

**TYPE:** DECISION
**KEYWORDS:** extension, same-origin fetch, no Playwright, cookies stay in browser

$START_CONTRACT
**PURPOSE:** Зафиксировать выбранную пользователем архитектуру.
**DESCRIPTION:** Chromium Manifest V3 extension выполняет только фиксированные HTTP-действия в сессии пользователя; локальный backend принимает бизнес-решения и вызывает ИИ.
**RATIONALE:** Решение сохраняет действующую web-сессию без передачи Cookie и не требует Playwright или эмуляции интерфейса.
**ACCEPTANCE_CRITERIA:** В bridge protocol нет произвольного URL; Cookie отсутствует во всех backend payload; расширение не содержит селекторов и click/input automation.
$END_CONTRACT

$START_BODY
- Target project: `/Users/macos/job-finder`.
- Browser family: Chromium-compatible Manifest V3; первичная установка — unpacked extension.
- Backend: Python standard library и существующий SQLite/Keychain слой; новые зависимости добавляются только при доказанной необходимости.
- AI: provider abstraction для OpenAI-compatible HTTPS endpoint либо локального совместимого endpoint; ключ хранится в Keychain.
$END_BODY

$START_LINKS
**IMPLEMENTS:** GOAL_WEB_TRANSPORT, GOAL_AI_BOUNDARY
**IMPACTS:** SCENARIO_ENABLE, SCENARIO_APPLY, SCENARIO_CHAT
**REQUIRES:** Пользовательский выбор варианта 2
$END_LINKS

$END_ARTIFACT_ARCHITECTURE_DECISION

$START_ARTIFACT_NEGATIVE_CONSTRAINTS
#### Negative constraints and invariants

**TYPE:** PRINCIPLE
**KEYWORDS:** prohibition, safety, user requirements

$START_CONTRACT
**PURPOSE:** Передать реализации неизменяемые ограничения дословно и исключить расширение полномочий робота.
**DESCRIPTION:** Ни один этап реализации, тестирования или запуска не может ослабить перечисленные ограничения.
**RATIONALE:** Частные web-методы нестабильны, содержат персональные данные и защищены механизмами hh.ru.
**ACCEPTANCE_CRITERIA:** Ограничения присутствуют в кодовых контрактах, тестах и документации.
$END_CONTRACT

$START_BODY
- Не использовать Playwright, Selenium, CDP, управление интерфейсом, DOM-селекторы или эмуляцию кликов.
- Не обходить CAPTCHA, challenge, rate limit, fingerprint или `x-gib-*`; не реализовывать stealth, proxy rotation и маскировку автоматизации.
- Не передавать Cookie, CSRF, pairing secret, API keys или исходный HAR в ИИ, логи, SQLite, конфиг или git.
- Не копировать `/Users/macos/Downloads/hh.ru.har` и `/Users/macos/Downloads/hh.ru1.har` в проект и не использовать реальные идентификаторы в fixtures.
- ИИ используется только для поиска релевантности, сопроводительных писем и ответов в чате; сеть, лимиты, дедупликация и разрешение действий остаются детерминированными.
- После однократного opt-in не требовать подтверждения каждого отклика или сообщения.
- Не выдумывать опыт, метрики, навыки, условия, даты или обязательства; использовать только локальные базы знаний, вакансию и историю чата.
- При неопределённости write-результата прекратить последующие записи, а не повторять запрос.
- Не отправлять реальные отклики и сообщения из автоматических тестов.
- Сохранять личные данные локально с минимальными правами доступа.
$END_BODY

$START_LINKS
**IMPLEMENTS:** GOAL_SECURITY
**IMPACTS:** Все модули и тесты
**REQUIRES:** HAR-derived contract, macOS Keychain, SQLite audit
$END_LINKS

$END_ARTIFACT_NEGATIVE_CONSTRAINTS

$END_SECTION_DECISIONS

$END_JOB_FINDER_AUTONOMOUS_HTTP_ROBOT_PLAN
