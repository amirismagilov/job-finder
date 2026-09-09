import json
from pathlib import Path
import re
import subprocess
import unittest

from job_finder.web_contract import WebAction


ROOT = Path(__file__).resolve().parents[1]


class ExtensionContractTest(unittest.TestCase):
    def test_manifest_is_mv3_with_exact_hosts_and_no_remote_code(self):
        manifest = json.loads((ROOT / "browser_extension/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(set(manifest["host_permissions"]), {"https://hh.ru/*", "https://chatik.hh.ru/*", "http://127.0.0.1:8766/*"})
        self.assertNotIn("cookies", manifest["permissions"])
        self.assertIn("script-src 'self'", manifest["content_security_policy"]["extension_pages"])
        self.assertEqual(manifest["content_scripts"][0]["js"], ["projectors.js", "page_executor.js"])

    def test_action_enum_matches_and_ui_automation_is_absent(self):
        background = (ROOT / "browser_extension/background.js").read_text(encoding="utf-8")
        executor = (ROOT / "browser_extension/page_executor.js").read_text(encoding="utf-8")
        projectors = (ROOT / "browser_extension/projectors.js").read_text(encoding="utf-8")
        block = background.split("const ACTION_ORIGIN", 1)[1].split("});", 1)[0]
        actions = set(re.findall(r"^\s{2}([A-Z_]+):", block, re.M))
        self.assertEqual(actions, {action.value for action in WebAction})
        joined = (background + executor + projectors).lower()
        for forbidden in ("playwright", "selenium", "queryselector", ".click(", "chrome.debugger", "x-gib-", "eval(", "new function"):
            self.assertNotIn(forbidden, joined)
        for arbitrary in ("command.url", "command.method", "command.headers"):
            self.assertNotIn(arbitrary, joined)

    def test_pairing_secret_never_leaves_browser_except_authorization_to_loopback(self):
        background = (ROOT / "browser_extension/background.js").read_text(encoding="utf-8")
        self.assertIn('const BRIDGE = "http://127.0.0.1:8766"', background)
        self.assertNotIn("console.log", background)
        self.assertNotIn("pairingSecret: result", background)

    def test_pairing_options_offer_copy_and_reveal_without_clipboard_permission(self):
        manifest = json.loads((ROOT / "browser_extension/manifest.json").read_text(encoding="utf-8"))
        html = (ROOT / "browser_extension/options.html").read_text(encoding="utf-8")
        script = (ROOT / "browser_extension/options.js").read_text(encoding="utf-8")
        self.assertIn('id="copy"', html)
        self.assertIn('id="reveal"', html)
        self.assertIn("navigator.clipboard.writeText", script)
        self.assertIn('document.execCommand("copy")', script)
        self.assertIn('secretInput.type = reveal ? "text" : "password"', script)
        self.assertNotIn("clipboardWrite", manifest["permissions"])

    def test_pairing_options_report_fixed_loopback_diagnostics_without_secrets(self):
        html = (ROOT / "browser_extension/options.html").read_text(encoding="utf-8")
        options = (ROOT / "browser_extension/options.js").read_text(encoding="utf-8")
        background = (ROOT / "browser_extension/background.js").read_text(encoding="utf-8")
        self.assertIn('id="check"', html)
        for code in ("connected_idle", "command_completed", "secret_missing", "pairing_rejected", "bridge_unreachable"):
            self.assertIn(code, options)
            self.assertIn(code, background)
        self.assertIn("pollCommand().then(sendResponse)", background)
        self.assertNotIn("console.log", background + options)

    def test_large_html_is_projected_inside_browser_and_csrf_variants_do_not_leave(self):
        module_path = json.dumps(str(ROOT / "browser_extension/projectors.js"))
        script = f'''
const p = require({module_path});
const prefix = 'x'.repeat(1200000) + '<meta name="csrf-token" content="META_SECRET"><form data-csrf="FORM_SECRET"><input name="_xsrf" value="INPUT_SECRET"></form><script nonce="SCRIPT_SECRET">window.csrf="JS_SECRET"</script>';
const search = p.projectAction('SEARCH_VACANCIES', prefix + '<a href="https://hh.ru/vacancy/123">AI Lead</a>', 'text/html', {{}}, true);
const detail = JSON.stringify({{'@type':'JobPosting', title:'AI Lead', description:'<form csrf="ATTR_SECRET"><input value="FORM_VALUE"></form>csrf=DESC_SECRET LLM', hiringOrganization:{{name:'Company'}}}});
const vacancy = p.projectAction('GET_VACANCY', '<script type="application/ld+json">' + detail + '</script>', 'text/html', {{vacancy_id:'123'}}, true);
const popup = p.projectAction('GET_RESPONSE_POPUP', JSON.stringify({{csrfToken:'JSON_SECRET', responseStatus:{{alreadyApplied:false,responseImpossible:false,test:{{hasTests:false}},shortVacancy:{{userTestPresent:false}},unusedResumeIds:[],resumes:{{}},letterMaxLength:1000}},responsePopup:{{startedWithQuestion:false}}}}), 'application/json', {{vacancy_id:'123'}}, true);
process.stdout.write(JSON.stringify({{search,vacancy,popup}}));
'''
        completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        output = completed.stdout
        value = json.loads(output)
        self.assertEqual(value["search"]["items"], [{"id": "123", "name": "AI Lead"}])
        self.assertEqual(value["vacancy"]["id"], "123")
        self.assertLess(len(output.encode("utf-8")), 1_000_000)
        for secret in ("META_SECRET", "FORM_SECRET", "INPUT_SECRET", "SCRIPT_SECRET", "JS_SECRET", "ATTR_SECRET", "FORM_VALUE", "DESC_SECRET", "JSON_SECRET"):
            self.assertNotIn(secret, output)

    def test_apply_fields_match_recorded_benign_contract_without_user_ids(self):
        executor = (ROOT / "browser_extension/page_executor.js").read_text(encoding="utf-8")
        self.assertIn('withoutTest: "no"', executor)
        self.assertIn('hhtmFromLabel: ""', executor)
        self.assertIn('hhtmSourceLabel: ""', executor)
        self.assertIn('mark_applicant_visible_in_vacancy_country: "false"', executor)
        self.assertIn("p.country_ids", executor)
        self.assertNotIn("x-gib", executor.lower())

    def test_html_reads_do_not_impersonate_json_ajax_requests(self):
        executor = (ROOT / "browser_extension/page_executor.js").read_text(encoding="utf-8")
        self.assertIn('command.action === "SEARCH_VACANCIES" || command.action === "GET_VACANCY"', executor)
        self.assertIn('{Accept: "text/html,application/xhtml+xml;q=0.9"}', executor)
        self.assertIn('{Accept: "application/json", "X-Requested-With": "XMLHttpRequest"}', executor)

    def test_protection_detection_uses_visible_html_and_explicit_json_signals(self):
        module_path = json.dumps(str(ROOT / "browser_extension/projectors.js"))
        script = f'''
const p = require({module_path});
const normalHtml = '<html><head><script>const captcha = "challenge";</script><style>.captcha-challenge {{ display:none }}</style><noscript>captcha challenge</noscript><template>captcha challenge</template></head><body><a href="/vacancy/123">AI Lead</a></body></html>';
const normalSearch = p.projectAction('SEARCH_VACANCIES', normalHtml, 'text/html', {{}}, true);
const normalVacancy = p.projectAction('GET_VACANCY', '<script>captcha challenge</script><h1>AI Lead</h1>', 'text/html', {{vacancy_id:'123'}}, true);
const visibleProtection = p.projectAction('SEARCH_VACANCIES', '<main><h1>Подтвердите, что вы не робот</h1></main>', 'text/html', {{}}, true);
const visibleVacancyProtection = p.projectAction('GET_VACANCY', '<main><h1>Подтвердите, что вы не робот</h1></main>', 'text/html', {{vacancy_id:'123'}}, true);
const unrelatedJson = p.projectAction('LIST_CHATS', JSON.stringify({{items:[{{id:'chat-a',unreadCount:0,note:'captcha challenge'}}]}}), 'application/json', {{}}, true);
const explicitCaptcha = p.projectAction('LIST_CHATS', JSON.stringify({{captcha:true}}), 'application/json', {{}}, true);
const explicitChallenge = p.projectAction('LIST_CHATS', JSON.stringify({{challenge:{{required:true}}}}), 'application/json', {{}}, true);
const jsonRobotCheck = p.projectAction('LIST_CHATS', JSON.stringify({{message:'Подтвердите, что вы не робот'}}), 'application/json', {{}}, true);
process.stdout.write(JSON.stringify({{normalSearch,normalVacancy,visibleProtection,visibleVacancyProtection,unrelatedJson,explicitCaptcha,explicitChallenge,jsonRobotCheck}}));
'''
        completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        value = json.loads(completed.stdout)
        self.assertEqual(value["normalSearch"], {"items": [{"id": "123", "name": "AI Lead"}], "found": 1})
        self.assertEqual(value["normalVacancy"], {"id": "123", "name": "AI Lead", "description": ""})
        self.assertEqual(value["unrelatedJson"], {"items": [{"id": "chat-a", "unreadCount": 0}]})
        for key in ("visibleProtection", "visibleVacancyProtection", "explicitCaptcha", "explicitChallenge", "jsonRobotCheck"):
            self.assertEqual(value[key], {"protection": "challenge"})

    def test_executor_readiness_uses_handshake_and_bounded_tab_load(self):
        background = (ROOT / "browser_extension/background.js").read_text(encoding="utf-8")
        executor = (ROOT / "browser_extension/page_executor.js").read_text(encoding="utf-8")
        self.assertIn("JOB_FINDER_READY", background)
        self.assertIn("JOB_FINDER_READY", executor)
        self.assertIn("READY_ATTEMPTS", background)
        self.assertIn("TAB_LOAD_TIMEOUT_MS", background)
        self.assertIn("waitForTabComplete", background)
        self.assertNotIn("setTimeout(resolve, 1500)", background)


if __name__ == "__main__":
    unittest.main()
