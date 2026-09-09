$START_OPENAI_PROVIDER_PLAN

**PURPOSE:** Подключить локально запущенный `job-finder` напрямую к официальному OpenAI API с моделью `gpt-5.6-sol` и `reasoning_effort=xhigh`, сохранив ключ только в macOS Keychain и совместимость с локальными OpenAI-compatible провайдерами.
**SCOPE:** Конфигурация LLM, тело Chat Completions запроса, локальный TOML, документация и офлайн-тесты HTTP-контракта.
**KEYWORDS:** OpenAI API, gpt-5.6-sol, xhigh, Chat Completions, Keychain, provider-neutral

$START_DOCUMENT_PLAN
### Document Plan

**SECTION_GOALS:**
- GOAL [Передавать точную выбранную модель и глубину reasoning в официальный OpenAI API] => GOAL_OPENAI_SOL
- GOAL [Не сохранять API-ключ в конфиге, argv, логах или git] => GOAL_KEY_SAFETY
- GOAL [Сохранить поддержку локальных OpenAI-compatible серверов без reasoning] => GOAL_PROVIDER_COMPATIBILITY

**SECTION_USE_CASES:**
- USE_CASE [Локальный worker -> запрашивает структурированную оценку или текст -> OpenAI возвращает JSON через Chat Completions] => SCENARIO_GENERATE
- USE_CASE [Пользователь -> вводит ключ скрытой CLI-командой -> приложение читает его из Keychain] => SCENARIO_SET_KEY

$END_DOCUMENT_PLAN

$START_DEV_PLAN

**PURPOSE:** Дать реализации точный контракт подключения OpenAI без изменения полномочий автономного агента.

---

### 1. Draft Code Graph (Pre-Code Design Artifact)

```xml
<DraftCodeGraph>
  <JobFinder_OpenAI_Provider_0_2_1_Info TYPE="PROJECT_INFO">
    <keywords>OpenAI API, gpt-5.6-sol, xhigh, structured JSON, Keychain</keywords>
    <terms>LLMConfig, reasoning_effort, LLMProvider, ChatCompletions</terms>
    <annotation>Локальный job-finder вызывает выбранную облачную модель по HTTPS, не раскрывая ключ и не передавая модели управление сетью или safety-политикой.</annotation>
    <BusinessScenarios>
      <Scenario NAME="GenerateGroundedText">Worker -&gt; LLMProvider -&gt; OpenAI Chat Completions -&gt; validated JSON</Scenario>
      <Scenario NAME="KeepLocalCompatibility">Local provider without reasoning_effort -&gt; legacy-compatible request with temperature</Scenario>
    </BusinessScenarios>
  </JobFinder_OpenAI_Provider_0_2_1_Info>

  <config_py FILE="src/job_finder/config.py" TYPE="CONFIGURATION_MODULE">
    <keywords>reasoning allowlist, HTTPS endpoint, optional provider capability</keywords>
    <terms>LLMConfig, load_config</terms>
    <annotation>Хранит опциональный reasoning_effort и отклоняет неизвестные значения.</annotation>
  </config_py>

  <llm_py FILE="src/job_finder/llm.py" TYPE="AI_PROVIDER_MODULE">
    <keywords>Chat Completions, reasoning_effort, temperature compatibility, bearer</keywords>
    <terms>LLMProvider._complete</terms>
    <annotation>Добавляет reasoning_effort в запрос и не отправляет temperature при включённом reasoning.</annotation>
    <llm_LLMProvider_complete_METHOD NAME="_complete" TYPE="IS_METHOD_OF_CLASS">
      <annotation>Создаёт ограниченный структурированный запрос и разбирает совместимый ответ.</annotation>
      <CrossLinks>
        <Link TARGET="config_py" TYPE="READS_DATA_FROM" />
      </CrossLinks>
    </llm_LLMProvider_complete_METHOD>
  </llm_py>

  <tests_test_llm_provider_py FILE="tests/test_llm_provider.py" TYPE="BACKEND_TEST_MODULE">
    <keywords>mocked HTTP, request body, bearer, response parsing</keywords>
    <terms>reasoning_effort, temperature, MemorySecrets</terms>
    <annotation>Проверяет сетевой контракт полностью офлайн и не выполняет платных или live-запросов.</annotation>
  </tests_test_llm_provider_py>

  <ProjectCrossLinks TYPE="MODULE_INTERACTIONS_OVERVIEW">
    <Link TARGET="llm_LLMProvider_complete_METHOD" TYPE="ORCHESTRATES_FLOW" />
    <Link TARGET="tests_test_llm_provider_py" TYPE="VERIFIES_CONTRACT" />
  </ProjectCrossLinks>
</DraftCodeGraph>
```

---

### 2. Step-by-step Data Flow

1. `load_config` читает endpoint, model и опциональный `reasoning_effort` из локального TOML, принимает только официальный набор значений и не читает секрет.
2. `LLMProvider` очищает контекст от URL, идентификаторов и credentials.
3. Для конфигурации с reasoning запрос получает `reasoning_effort`; `temperature` опускается. Без reasoning сохраняется прежний provider-compatible `temperature=0.1`.
4. API-ключ читается из Keychain account `llm_api_key` и используется только как Bearer-заголовок текущего HTTPS-запроса.
5. Ответ разбирается по существующему Chat Completions контракту и проходит текущую grounded-валидацию.
6. Офлайн-тесты перехватывают HTTP-вызов, проверяют тело и заголовки без раскрытия или сетевой передачи секрета.
7. Локальный `config.toml` выбирает `https://api.openai.com/v1/chat/completions`, `gpt-5.6-sol`, `xhigh` и timeout 180 секунд.

---

### 3. Acceptance Criteria

- [ ] `config.toml` выбирает официальный HTTPS endpoint, `gpt-5.6-sol` и `xhigh`.
- [ ] В committed-файлах отсутствует API-ключ; ввод остаётся скрытым через `job-finder llm set-key`.
- [ ] Chat Completions запрос содержит `reasoning_effort=xhigh` и не содержит `temperature`.
- [ ] Конфигурация без reasoning сохраняет текущую совместимость с локальными провайдерами.
- [ ] Неизвестное значение reasoning отклоняется при загрузке конфигурации.
- [ ] Офлайн-тесты проверяют request body, Bearer и разбор структурированного ответа.
- [ ] Полный тестовый набор, compileall, Doxygen и `git diff --check` проходят без live-запросов.

$END_DEV_PLAN

$START_SECTION_DECISION
### Selected Architecture

$START_ARTIFACT_DIRECT_OPENAI
#### Direct OpenAI Chat Completions

**TYPE:** DECISION
**KEYWORDS:** direct HTTPS, no proxy, exact model, provider compatibility

$START_CONTRACT
**PURPOSE:** Зафиксировать выбранный пользователем способ подключения.
**DESCRIPTION:** `job-finder` напрямую вызывает официальный OpenAI Chat Completions endpoint из локального процесса.
**RATIONALE:** Это единственный вариант из рассмотренных, который использует точную модель `gpt-5.6-sol`, не добавляет локальный прокси и сохраняет существующую архитектуру stdlib HTTP-клиента. Ollama не предоставляет эту облачную модель.
**ACCEPTANCE_CRITERIA:** Endpoint, model и reasoning совпадают с выбором пользователя; новые runtime-зависимости не добавлены.
$END_CONTRACT

$START_BODY
- Выбран вариант прямого HTTPS-вызова OpenAI.
- Локальный inference server не устанавливается.
- Авторизация Codex не переиспользуется приложением; `job-finder` использует отдельный Platform API key из Keychain.
- Полномочия ИИ остаются ограничены оценкой релевантности и генерацией текстов.
$END_BODY

$START_LINKS
**IMPLEMENTS:** GOAL_OPENAI_SOL, GOAL_KEY_SAFETY, GOAL_PROVIDER_COMPATIBILITY
**IMPACTS:** SCENARIO_GENERATE, SCENARIO_SET_KEY
**REQUIRES:** Существующий LLMProvider, macOS Keychain, пользовательский Platform API key
$END_LINKS

$END_ARTIFACT_DIRECT_OPENAI

$END_SECTION_DECISION

$END_OPENAI_PROVIDER_PLAN
