$START_SEARCH_EXCLUSIONS_PLAN

**PURPOSE:** Исключить из автономного поиска и откликов все вакансии компаний экосистемы Сбера, Яндекса и betting/букмекерского сектора до передачи данных в ИИ.
**SCOPE:** Валидируемая конфигурация exclusion terms, детерминированная классификация полной вакансии, сохранение terminal-решения в SQLite, отчётность и офлайн-тесты.
**KEYWORDS:** employer exclusion, betting, Sber, Yandex, deterministic policy, pre-LLM gate

$START_DOCUMENT_PLAN
### Document Plan

**SECTION_GOALS:**
- GOAL [Ни одна запрещённая компания не достигает AI relevance, preflight или отправки] => GOAL_FAIL_CLOSED_EXCLUSION
- GOAL [Пользователь может расширять список компаний и отраслевых маркеров через TOML] => GOAL_CONFIGURABLE_POLICY
- GOAL [Существующие dry-run решения запрещённых компаний становятся terminal ineligible при следующем цикле] => GOAL_STATE_RECONCILIATION

**SECTION_USE_CASES:**
- USE_CASE [Worker -> получает полную вакансию -> обнаруживает Сбер/Яндекс в работодателе -> сохраняет ineligible] => SCENARIO_EMPLOYER_BLOCK
- USE_CASE [Worker -> обнаруживает betting/букмекерский маркер в работодателе, названии или описании -> сохраняет ineligible] => SCENARIO_INDUSTRY_BLOCK
- USE_CASE [Worker -> получает обычную вакансию -> продолжает текущий AI/preflight pipeline] => SCENARIO_ALLOWED_VACANCY

$END_DOCUMENT_PLAN

$START_DEV_PLAN

**PURPOSE:** Добавить конфигурируемый deterministic policy gate без расширения полномочий модели и без новых зависимостей.

---

### 1. Draft Code Graph (Pre-Code Design Artifact)

```xml
<DraftCodeGraph>
  <JobFinder_Search_Exclusions_0_3_1_Info TYPE="PROJECT_INFO">
    <keywords>company exclusion, industry exclusion, pre-LLM policy, terminal state</keywords>
    <terms>SearchConfig, AutonomousWorker, vacancy_decisions, ineligible</terms>
    <annotation>Полная вакансия проверяется детерминированно до knowledge retrieval и LLM; совпадение навсегда исключает текущую vacancy_id из автономного pipeline.</annotation>
    <BusinessScenarios>
      <Scenario NAME="ExcludeCompany">GET_VACANCY -&gt; normalized employer -&gt; employer term match -&gt; ineligible</Scenario>
      <Scenario NAME="ExcludeBetting">GET_VACANCY -&gt; normalized public vacancy text -&gt; industry marker match -&gt; ineligible</Scenario>
      <Scenario NAME="ContinueAllowed">no match -&gt; knowledge retrieval -&gt; LLM relevance -&gt; existing preflight/write guards</Scenario>
    </BusinessScenarios>
  </JobFinder_Search_Exclusions_0_3_1_Info>

  <config_py FILE="src/job_finder/config.py" TYPE="CONFIGURATION_MODULE">
    <keywords>excluded employer terms, excluded industry terms, bounded TOML lists</keywords>
    <terms>SearchConfig, _validate_exclusion_terms, load_config</terms>
    <annotation>Хранит bounded непустые строки и безопасные значения по умолчанию для Сбера, Яндекса и betting-сектора.</annotation>
    <config_validate_exclusion_terms_FUNC NAME="_validate_exclusion_terms" TYPE="IS_FUNCTION_OF_MODULE">
      <annotation>Отклоняет не-списки, пустые элементы, чрезмерные размеры и нестроковые значения.</annotation>
    </config_validate_exclusion_terms_FUNC>
  </config_py>

  <autonomy_py FILE="src/job_finder/autonomy.py" TYPE="POLICY_ORCHESTRATION_MODULE">
    <keywords>Unicode normalization, deterministic exclusion, no LLM call, terminal state</keywords>
    <terms>_vacancy_exclusion_reason, AutonomousWorker.run_cycle</terms>
    <annotation>Проверяет employer отдельно от отраслевых признаков и блокирует вакансию до получения базы знаний.</annotation>
    <autonomy_vacancy_exclusion_reason_FUNC NAME="_vacancy_exclusion_reason" TYPE="IS_FUNCTION_OF_MODULE">
      <annotation>Возвращает стабильную reason-code или None без обращения к модели.</annotation>
      <CrossLinks>
        <Link TARGET="config_py" TYPE="READS_DATA_FROM" />
      </CrossLinks>
    </autonomy_vacancy_exclusion_reason_FUNC>
    <autonomy_AutonomousWorker_run_cycle_METHOD NAME="run_cycle" TYPE="IS_METHOD_OF_CLASS">
      <annotation>До knowledge/LLM сохраняет excluded vacancy как ineligible, очищая прежнее generated_text.</annotation>
      <CrossLinks>
        <Link TARGET="autonomy_vacancy_exclusion_reason_FUNC" TYPE="CALLS_FUNCTION" />
      </CrossLinks>
    </autonomy_AutonomousWorker_run_cycle_METHOD>
  </autonomy_py>

  <tests_test_autonomy_py FILE="tests/test_autonomy.py" TYPE="BACKEND_TEST_MODULE">
    <keywords>Sber, SberTech, Yandex, bookmaker, no AI, no preflight, no write</keywords>
    <terms>AutonomyExclusionTest, CountingLLM, CountingClient</terms>
    <annotation>Доказывает terminal exclusion и отсутствие всех downstream-вызовов на запрещённой вакансии.</annotation>
  </tests_test_autonomy_py>

  <tests_test_config_py FILE="tests/test_config.py" TYPE="BACKEND_TEST_MODULE">
    <keywords>TOML parsing, list validation, default exclusions</keywords>
    <terms>SearchConfig exclusions</terms>
    <annotation>Проверяет значения по умолчанию, пользовательское расширение и fail-closed validation.</annotation>
  </tests_test_config_py>

  <ProjectCrossLinks TYPE="MODULE_INTERACTIONS_OVERVIEW">
    <Link TARGET="config_validate_exclusion_terms_FUNC" TYPE="PROVIDES_VALIDATED_POLICY" />
    <Link TARGET="autonomy_vacancy_exclusion_reason_FUNC" TYPE="IMPLEMENTS_POLICY" />
    <Link TARGET="tests_test_autonomy_py" TYPE="VERIFIES_NO_DOWNSTREAM_ACTIONS" />
  </ProjectCrossLinks>
</DraftCodeGraph>
```

---

### 2. Step-by-step Data Flow

1. `load_config` читает `excluded_employer_terms` и `excluded_industry_terms` из `[search]`; отсутствие полей включает безопасные project defaults.
2. Значения валидируются как ограниченные списки непустых строк и сохраняются в immutable `SearchConfig`.
3. Worker выполняет существующий `GET_VACANCY`, чтобы получить работодателя, название и описание.
4. Текст нормализуется через Unicode NFKC, casefold, замену `ё/е` и схлопывание разделителей.
5. Employer terms проверяются только по имени работодателя и включают Сбер/Sber и Яндекс/Yandex, поэтому дочерние бренды с этими маркерами также блокируются.
6. Betting terms проверяются по работодателю, названию и описанию; используются отраслевые слова и известные бренды без широкого маркера `bet`, создающего ложные совпадения.
7. При совпадении worker вычисляет hash только из публичной вакансии, сохраняет `status=ineligible`, `score=NULL`, `generated_text=NULL`, пишет reason-code и увеличивает skip counter.
8. Ветка exclusion завершает обработку vacancy до `_knowledge_for`, `LLMProvider.assess_relevance`, application preflight и любых remote writes.
9. При отсутствии совпадения выполняется существующий pipeline без изменения.
10. Офлайн-тесты используют fake client/LLM и доказывают нулевые AI, preflight и apply вызовы для каждой категории, включая ранее `dry_run_ready` запись.

---

### 3. Considered Alternatives

- Query-level отрицательные слова отклонены: web-поиск hh.ru не гарантирует фильтрацию по экосистеме работодателя, а скрытые юридические названия останутся.
- LLM-классификация отрасли отклонена: она недетерминирована, расходует CLI-квоту и может пропустить запрещённую компанию.
- Выбран post-detail/pre-LLM deterministic gate: использует наиболее полный доступный публичный контекст и технически не позволяет продолжить запрещённой вакансии.

---

### 4. Acceptance Criteria

- [x] Сбер, Сбербанк, Sber, SberTech, Яндекс и Yandex блокируются по employer name независимо от регистра и разделителей.
- [x] Букмекер/беттинг и заданные известные бренды блокируются по employer/title/description без общего substring `bet`.
- [x] Запрещённая вакансия не вызывает knowledge retrieval, LLM relevance, preflight, cover generation или apply.
- [x] Результат сохраняется как terminal `ineligible` с `excluded_employer` или `excluded_betting`; прежнее письмо очищается.
- [x] Обычные вакансии продолжают существующий pipeline.
- [x] Локальный `config.toml` и `config.example.toml` содержат явные списки; секретов и новых зависимостей нет.
- [x] Тесты, compileall, Doxygen, JavaScript checks и `git diff --check` проходят; live hh writes не выполняются.

$END_DEV_PLAN

$START_SECTION_DECISION
### Selected Architecture

$START_ARTIFACT_PRE_LLM_POLICY_GATE
#### Configurable Post-Detail / Pre-LLM Policy Gate

**TYPE:** DECISION
**KEYWORDS:** deterministic exclusion, full vacancy context, terminal ineligible

$START_CONTRACT
**PURPOSE:** Гарантировать, что запрещённые работодатели и отрасли технически не могут дойти до AI или отклика.
**DESCRIPTION:** Worker применяет валидируемые TOML-правила сразу после получения полной публичной вакансии.
**RATIONALE:** Это наиболее ранняя точка, где одновременно доступны employer, title и description, и наиболее поздняя точка до затратного/недетерминированного AI pipeline.
**ACCEPTANCE_CRITERIA:** Для совпадения downstream call counters равны нулю, а SQLite содержит terminal ineligible без письма.
$END_CONTRACT

$START_BODY
- Выбор следует прямому запрету пользователя и не требует решения модели.
- Конфиг позволяет дополнять бренды без релиза кода.
- Существующие safety limits, bridge contract и автономный consent не меняются.
$END_BODY

$START_LINKS
**IMPLEMENTS:** GOAL_FAIL_CLOSED_EXCLUSION, GOAL_CONFIGURABLE_POLICY, GOAL_STATE_RECONCILIATION
**IMPACTS:** SCENARIO_EMPLOYER_BLOCK, SCENARIO_INDUSTRY_BLOCK, SCENARIO_ALLOWED_VACANCY
**REQUIRES:** Existing GET_VACANCY projection, SearchConfig, Storage.record_vacancy
$END_LINKS

$END_ARTIFACT_PRE_LLM_POLICY_GATE

$END_SECTION_DECISION

$END_SEARCH_EXCLUSIONS_PLAN
