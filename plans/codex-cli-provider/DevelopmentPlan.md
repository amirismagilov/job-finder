$START_CODEX_CLI_PROVIDER_PLAN

**PURPOSE:** Подключить `job-finder` к уже авторизованному через ChatGPT Codex CLI без OpenAI Platform API key, используя `gpt-5.6-sol` с `reasoning_effort=xhigh`, строгие JSON-схемы и изолированный процесс без инструментов.
**SCOPE:** Конфигурация провайдера, безопасный subprocess-транспорт, структурированные AI-задачи, локальный TOML, документация и офлайн-тесты.
**KEYWORDS:** Codex CLI, ChatGPT login, gpt-5.6-sol, xhigh, subprocess isolation, JSON Schema, prompt injection

$START_DOCUMENT_PLAN
### Document Plan

**SECTION_GOALS:**
- GOAL [Использовать существующую ChatGPT-авторизацию Codex CLI без API-ключа] => GOAL_NO_API_KEY
- GOAL [Лишить обрабатываемый моделью контент вакансий и чатов доступа к shell, web, MCP и пользовательским правилам] => GOAL_TOOL_ISOLATION
- GOAL [Сохранить строгий JSON-контракт и детерминированную grounded-валидацию] => GOAL_STRUCTURED_OUTPUT
- GOAL [Оставить существующий HTTP-провайдер доступным как явную альтернативу] => GOAL_BACKWARD_COMPATIBILITY

**SECTION_USE_CASES:**
- USE_CASE [Worker -> оценивает вакансию -> Codex CLI возвращает RelevanceDecision по JSON Schema] => SCENARIO_RELEVANCE
- USE_CASE [Worker -> формирует сопроводительное/ответ -> Codex CLI возвращает GeneratedText по JSON Schema] => SCENARIO_TEXT
- USE_CASE [Оператор -> запускает job-finder -> provider выбирается из локального config.toml] => SCENARIO_PROVIDER_SELECTION

$END_DOCUMENT_PLAN

$START_DEV_PLAN

**PURPOSE:** Зафиксировать безопасный контракт CLI-провайдера до реализации и не расширять полномочия ИИ за пределы трёх семантических задач.

---

### 1. Draft Code Graph (Pre-Code Design Artifact)

```xml
<DraftCodeGraph>
  <JobFinder_Codex_CLI_Provider_0_3_0_Info TYPE="PROJECT_INFO">
    <keywords>Codex CLI, ChatGPT OAuth, subprocess, tool isolation, structured output</keywords>
    <terms>LLMConfig, CodexCLITransport, LLMProvider, JSON Schema</terms>
    <annotation>Локальный worker передаёт только очищенный контекст в эфемерный Codex CLI процесс без инструментов и принимает только JSON заданной схемы.</annotation>
    <BusinessScenarios>
      <Scenario NAME="AssessVacancy">Worker -&gt; LLMProvider -&gt; CodexCLITransport -&gt; schema-validated JSON -&gt; RelevanceDecision</Scenario>
      <Scenario NAME="GenerateGroundedText">Worker -&gt; LLMProvider -&gt; CodexCLITransport -&gt; schema-validated JSON -&gt; GeneratedText -&gt; deterministic grounding gate</Scenario>
    </BusinessScenarios>
  </JobFinder_Codex_CLI_Provider_0_3_0_Info>

  <config_py FILE="src/job_finder/config.py" TYPE="CONFIGURATION_MODULE">
    <keywords>provider allowlist, absolute executable, timeout clamp, model, reasoning</keywords>
    <terms>LLMConfig, load_config, _validate_llm_provider, _validate_cli_executable</terms>
    <annotation>Выбирает http или codex_cli; CLI-режим требует абсолютный исполняемый файл, но не API endpoint или Keychain key.</annotation>
  </config_py>

  <codex_cli_py FILE="src/job_finder/codex_cli.py" TYPE="AI_TRANSPORT_MODULE">
    <keywords>argv allowlist, stdin prompt, TemporaryDirectory, output schema, no shell</keywords>
    <terms>CodexCLITransport, CodexCLIError</terms>
    <annotation>Запускает фиксированный argv без shell=True в приватном временном cwd и удаляет схему/результат после вызова.</annotation>
    <codex_cli_CodexCLITransport_complete_METHOD NAME="complete" TYPE="IS_METHOD_OF_CLASS">
      <annotation>Передаёт prompt через stdin, отключает shell/unified_exec/web/apps, ограничивает timeout/размер результата и возвращает JSON-объект.</annotation>
      <CrossLinks>
        <Link TARGET="config_py" TYPE="READS_DATA_FROM" />
      </CrossLinks>
    </codex_cli_CodexCLITransport_complete_METHOD>
  </codex_cli_py>

  <llm_py FILE="src/job_finder/llm.py" TYPE="AI_PROVIDER_MODULE">
    <keywords>provider routing, sanitized context, task schemas, grounded parsing</keywords>
    <terms>LLMProvider._complete, RELEVANCE_SCHEMA, GENERATED_TEXT_SCHEMA</terms>
    <annotation>Маршрутизирует очищенный контекст в выбранный транспорт и сохраняет существующую typed/grounded-валидацию.</annotation>
    <llm_LLMProvider_complete_METHOD NAME="_complete" TYPE="IS_METHOD_OF_CLASS">
      <annotation>Формирует системное ограничение и выбирает HTTP либо Codex CLI без предоставления модели сетевых полномочий.</annotation>
      <CrossLinks>
        <Link TARGET="codex_cli_CodexCLITransport_complete_METHOD" TYPE="CALLS" />
      </CrossLinks>
    </llm_LLMProvider_complete_METHOD>
  </llm_py>

  <tests_test_codex_cli_py FILE="tests/test_codex_cli.py" TYPE="BACKEND_TEST_MODULE">
    <keywords>offline subprocess, argv, stdin, temporary isolation, timeout, output bounds</keywords>
    <terms>FakeRunner, CodexCLITransport</terms>
    <annotation>Проверяет CLI-контракт без запуска Codex и без сети.</annotation>
  </tests_test_codex_cli_py>

  <tests_test_llm_provider_py FILE="tests/test_llm_provider.py" TYPE="BACKEND_TEST_MODULE">
    <keywords>provider selection, schema routing, HTTP regression</keywords>
    <terms>LLMConfigTest, LLMProviderCLITransportTest, LLMProviderHTTPTest</terms>
    <annotation>Доказывает корректный выбор провайдера, строгие схемы и сохранение HTTP-совместимости.</annotation>
  </tests_test_llm_provider_py>

  <ProjectCrossLinks TYPE="MODULE_INTERACTIONS_OVERVIEW">
    <Link TARGET="llm_LLMProvider_complete_METHOD" TYPE="ORCHESTRATES_FLOW" />
    <Link TARGET="codex_cli_CodexCLITransport_complete_METHOD" TYPE="IMPLEMENTS_TRANSPORT" />
    <Link TARGET="tests_test_codex_cli_py" TYPE="VERIFIES_SECURITY_BOUNDARY" />
    <Link TARGET="tests_test_llm_provider_py" TYPE="VERIFIES_PROVIDER_ROUTING" />
  </ProjectCrossLinks>
</DraftCodeGraph>
```

---

### 2. Step-by-step Data Flow

1. `load_config` принимает только `provider = "http"` или `provider = "codex_cli"`; для CLI валидирует абсолютный путь к исполняемому файлу, модель, reasoning и bounded timeout.
2. `LLMProvider` рекурсивно удаляет URL, credentials и идентификаторы из vacancy/chat context.
3. Для каждой из двух форм результата выбирается закрытая JSON Schema с `additionalProperties=false` и обязательными полями.
4. `CodexCLITransport` создаёт приватный временный каталог, записывает туда только JSON Schema и задаёт этот пустой каталог как cwd.
5. CLI запускается списком аргументов без `shell=True`: `exec`, `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, фиксированные model/reasoning, `approval_policy=never`, выключенные shell/unified_exec/web/apps, `sandbox=read-only`, `--output-schema` и `--output-last-message`.
6. Системная инструкция и очищенный JSON передаются только через stdin; vacancy/chat text не попадает в argv, логи или сохранённую сессию.
7. После bounded timeout транспорт читает ограниченный result-файл, требует JSON-объект и автоматически удаляет временный каталог.
8. `LLMProvider` преобразует объект в `RelevanceDecision` или `GeneratedText`; текущий deterministic grounding gate продолжает блокировать неподтверждённые факты.
9. HTTP-ветка продолжает работать по прежнему контракту для локальных/Platform endpoints, но локальный рабочий `config.toml` выбирает `codex_cli` и не требует `llm_api_key`.

---

### 3. Security Invariants

- Контент вакансии или чата считается недоверенными данными, а не инструкциями.
- CLI не получает shell, unified exec, web search, apps, пользовательские правила или MCP-конфигурацию.
- Запуск не использует `shell=True`, строковую команду, vacancy data в argv или логирование stdout/stderr.
- CLI cwd не содержит репозиторий, knowledge base, HAR, browser profile или иные пользовательские файлы.
- Временные файлы приватны и удаляются после каждого запроса; `--ephemeral` отключает сохранение сессии.
- Ошибки не раскрывают prompt, environment, stderr, stdout, credentials или абсолютные пути пользовательских данных.
- Выход ограничен JSON Schema, размером и timeout; неизвестная схема закрывает действие ошибкой.
- AI по-прежнему не выполняет HTTP-команды hh.ru и не меняет safety/consent policy.

---

### 4. Acceptance Criteria

- [ ] Локальный `config.toml` использует `provider="codex_cli"`, абсолютный Codex executable, `gpt-5.6-sol`, `xhigh`; API key отсутствует и не требуется.
- [ ] CLI argv построен только из валидированной конфигурации и фиксированных allowlisted flags; prompt передаётся через stdin.
- [ ] Shell/unified exec, web search, apps, user config и repo rules отключены; cwd является пустым приватным temp-каталогом.
- [ ] Relevance и generated-text ответы ограничены отдельными закрытыми JSON Schema.
- [ ] Timeout, nonzero exit, missing/oversized/malformed result и runner exceptions дают безопасный `LLMError` без сырых данных.
- [ ] Существующая HTTP-ветка и все её тесты остаются рабочими.
- [ ] README объясняет, что `codex login` использует ChatGPT-авторизацию, и отдельно маркирует HTTP API key как необязательную альтернативу.
- [ ] Офлайн-тесты подтверждают argv, stdin, cwd isolation, cleanup, отсутствие shell/API key и обработку ошибок.
- [ ] Один синтетический live-smoke через фактический `LLMProvider` успешно возвращает schema-valid результат; пользовательские knowledge/HAR не передаются.
- [ ] Полный тестовый набор, compileall, Doxygen и `git diff --check` проходят.

$END_DEV_PLAN

$START_SECTION_DECISION
### Selected Architecture

$START_ARTIFACT_DIRECT_CODEX_CLI
#### Direct Ephemeral Codex CLI Subprocess

**TYPE:** DECISION
**KEYWORDS:** ChatGPT auth, no API key, ephemeral subprocess, strict isolation

$START_CONTRACT
**PURPOSE:** Зафиксировать выбранный пользователем способ AI-доступа без Platform API key.
**DESCRIPTION:** `job-finder` вызывает установленный и уже авторизованный Codex CLI отдельным эфемерным процессом для каждой семантической задачи.
**RATIONALE:** Вариант использует доступную ChatGPT-авторизацию и точную выбранную модель. Прямой API отвергнут из-за отсутствия ключа; локальный proxy вокруг CLI добавил бы отдельный постоянно работающий процесс и новую поверхность атаки без необходимой пользы.
**ACCEPTANCE_CRITERIA:** Команда CLI ограничена фиксированными флагами, контент поступает через stdin, инструменты отключены, результат проходит schema/grounding validation.
$END_CONTRACT

$START_BODY
- Выбран прямой вызов `codex exec` через `subprocess.run` без shell.
- Авторизация берётся из существующего `codex login`; секреты Codex не читаются приложением.
- HTTP-провайдер остаётся совместимой альтернативой, но не используется в локальной конфигурации.
- Один process-per-call имеет заметный token/latency overhead; это сознательный компромисс ради отсутствия API key и простой изоляции.
- Полномочия ИИ остаются ограничены оценкой релевантности и генерацией сопроводительных/ответов.
$END_BODY

$START_LINKS
**IMPLEMENTS:** GOAL_NO_API_KEY, GOAL_TOOL_ISOLATION, GOAL_STRUCTURED_OUTPUT, GOAL_BACKWARD_COMPATIBILITY
**IMPACTS:** SCENARIO_RELEVANCE, SCENARIO_TEXT, SCENARIO_PROVIDER_SELECTION
**REQUIRES:** Установленный Codex CLI, успешный `codex login`, поддержка модели `gpt-5.6-sol`
$END_LINKS

$END_ARTIFACT_DIRECT_CODEX_CLI

$END_SECTION_DECISION

$END_CODEX_CLI_PROVIDER_PLAN
