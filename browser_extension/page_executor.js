/* Same-origin executor. Session cookies, CSRF and raw response bytes stay inside the browser. */
(() => {
  const EXACT_KEYS = Object.freeze({
    SEARCH_VACANCIES: ["text", "area"],
    GET_VACANCY: ["vacancy_id"],
    GET_RESPONSE_POPUP: ["vacancy_id"],
    APPLY: ["vacancy_id", "resume_hash", "letter", "country_ids"],
    LIST_CHATS: ["unread_only"],
    GET_CHAT_DATA: ["chat_id"],
    SEND_CHAT_MESSAGE: ["chat_id", "text", "idempotency_key"],
    MARK_READ: ["chat_id", "message_id"]
  });
  const REQUIRED = Object.freeze({
    SEARCH_VACANCIES: ["text", "area"], GET_VACANCY: ["vacancy_id"], GET_RESPONSE_POPUP: ["vacancy_id"],
    APPLY: ["vacancy_id", "resume_hash", "letter"], LIST_CHATS: [], GET_CHAT_DATA: ["chat_id"],
    SEND_CHAT_MESSAGE: ["chat_id", "text", "idempotency_key"], MARK_READ: ["chat_id", "message_id"]
  });
  const ID = /^[A-Za-z0-9_-]{1,80}$/;

  function validate(command) {
    if (!Object.hasOwn(EXACT_KEYS, command.action)) throw new Error("action_not_allowed");
    const keys = Object.keys(command.params).sort();
    if (keys.some(key => !EXACT_KEYS[command.action].includes(key)) || REQUIRED[command.action].some(key => !keys.includes(key))) throw new Error("params_not_allowed");
    for (const key of ["vacancy_id", "resume_hash", "chat_id", "message_id"]) {
      if (Object.hasOwn(command.params, key) && !ID.test(String(command.params[key]))) throw new Error("invalid_id");
    }
    if (Object.hasOwn(command.params, "country_ids") && (!Array.isArray(command.params.country_ids) || command.params.country_ids.length > 20 || command.params.country_ids.some(value => !ID.test(String(value))))) throw new Error("invalid_country_ids");
  }

  function xsrf() {
    const match = document.cookie.match(/(?:^|;\s*)_xsrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function fixedSpec(action, p) {
    if (action === "SEARCH_VACANCIES") return {host: "hh.ru", path: "/search/vacancy", method: "GET", query: {text: p.text, area: p.area}};
    if (action === "GET_VACANCY") return {host: "hh.ru", path: `/vacancy/${p.vacancy_id}`, method: "GET"};
    if (action === "GET_RESPONSE_POPUP") return {host: "hh.ru", path: "/applicant/vacancy_response/popup", method: "GET", query: {vacancyId: p.vacancy_id, isTest: "false", withoutTest: "no", lux: "true"}};
    if (action === "APPLY") {
      const multipart = {vacancy_id: p.vacancy_id, resume_hash: p.resume_hash, letter: p.letter, ignore_postponed: "true", incomplete: "false", withoutTest: "no", lux: "true", hhtmFromLabel: "", hhtmSourceLabel: "", mark_applicant_visible_in_vacancy_country: "false"};
      if (Array.isArray(p.country_ids) && p.country_ids.length) multipart.country_ids = p.country_ids;
      return {host: "hh.ru", path: "/applicant/vacancy_response/popup", method: "POST", multipart};
    }
    if (action === "LIST_CHATS") return {host: "chatik.hh.ru", path: "/chatik/api/chats", method: "GET", query: {filterUnread: p.unread_only ? "true" : "false", filterHasTextMessage: "true", do_not_track_session_events: "true"}};
    if (action === "GET_CHAT_DATA") return {host: "chatik.hh.ru", path: "/chatik/api/chat_data", method: "GET", query: {chatId: p.chat_id, do_not_track_session_events: "true"}};
    if (action === "SEND_CHAT_MESSAGE") return {host: "chatik.hh.ru", path: "/chatik/api/send", method: "POST", json: {chatId: p.chat_id, idempotencyKey: p.idempotency_key, text: p.text}};
    if (action === "MARK_READ") return {host: "chatik.hh.ru", path: "/chatik/api/mark_read", method: "POST", json: {chatId: p.chat_id, messageId: p.message_id, hasUnreadDiscardMessage: false}};
    throw new Error("action_not_allowed");
  }

  async function execute(command) {
    validate(command);
    const spec = fixedSpec(command.action, command.params);
    if (location.hostname !== spec.host) throw new Error("wrong_origin");
    const url = new URL(`https://${spec.host}${spec.path}`);
    for (const [key, value] of Object.entries(spec.query || {})) url.searchParams.set(key, String(value));
    const headers = {Accept: "application/json, text/html;q=0.9", "X-Requested-With": "XMLHttpRequest"};
    const init = {method: spec.method, credentials: "include", cache: "no-store", redirect: "follow", headers};
    if (spec.multipart) {
      const csrf = xsrf();
      if (!csrf) throw new Error("csrf_unavailable");
      const form = new FormData();
      form.append("_xsrf", csrf);
      for (const [key, value] of Object.entries(spec.multipart)) {
        if (key === "country_ids" && Array.isArray(value)) {
          for (const countryId of value) form.append(key, countryId);
        } else {
          form.append(key, value);
        }
      }
      headers["X-Xsrftoken"] = csrf;
      init.body = form;
    } else if (spec.json) {
      const csrf = xsrf();
      if (!csrf) throw new Error("csrf_unavailable");
      headers["Content-Type"] = "application/json";
      headers["X-Xsrftoken"] = csrf;
      init.body = JSON.stringify(spec.json);
    }
    const response = await fetch(url.toString(), init);
    const contentType = (response.headers.get("content-type") || "").split(";")[0];
    const raw = await response.text();
    if (!globalThis.JobFinderProjectors) throw new Error("projector_unavailable");
    // BUG_FIX_CONTEXT: Returning redacted raw HTML still leaked unknown markup and rejected the
    // recorded 2.5 MB search page. A bounded browser-side projector now emits only typed fields.
    const body = globalThis.JobFinderProjectors.projectAction(command.action, raw, contentType, command.params, response.ok);
    return {command_id: command.command_id, status: response.status, content_type: "application/json", body, redirect_path: new URL(response.url).pathname};
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message && message.type === "JOB_FINDER_READY") {
      sendResponse({ready: true, contract: 1});
      return false;
    }
    if (!message || message.type !== "JOB_FINDER_EXECUTE") return false;
    execute(message.command).then(sendResponse).catch(() => sendResponse({command_id: message.command.command_id, status: 0, content_type: "application/json", body: {executor_error: true}, redirect_path: ""}));
    return true;
  });
})();
