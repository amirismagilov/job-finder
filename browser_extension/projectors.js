/* Browser-side response minimization. Only typed public/business fields can leave the browser. */
(function initializeProjectors(root) {
  "use strict";

  const ID = /^[A-Za-z0-9_-]{1,80}$/;
  const RAW_HTML_LIMIT = 4 * 1024 * 1024;
  const RAW_JSON_LIMIT = 1024 * 1024;
  const SEARCH_ITEM_LIMIT = 100;
  const CHAT_ITEM_LIMIT = 200;
  const PROTECTION_TEXT_LIMIT = 64 * 1024;

  function byteLength(value) {
    return new TextEncoder().encode(value).length;
  }

  function identifier(value) {
    const result = typeof value === "string" || typeof value === "number" ? String(value) : "";
    return ID.test(result) ? result : "";
  }

  function decodeEntities(value) {
    return String(value)
      .replace(/&#(\d+);/g, (_match, number) => String.fromCodePoint(Number(number)))
      .replace(/&#x([0-9a-f]+);/gi, (_match, number) => String.fromCodePoint(parseInt(number, 16)))
      .replace(/&quot;/gi, "\"")
      .replace(/&apos;|&#39;/gi, "'")
      .replace(/&lt;/gi, "<")
      .replace(/&gt;/gi, ">")
      .replace(/&amp;/gi, "&")
      .replace(/&nbsp;/gi, " ");
  }

  function publicText(value, maximum) {
    if (value === null || value === undefined) return "";
    return decodeEntities(String(value))
      .replace(/<[^>]*>/g, " ")
      .replace(/\b(?:csrf|xsrf|authorization|cookie|token)\b\s*[:=]\s*["']?[^\s"'<>&]{1,500}/gi, "[credential removed]")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, maximum);
  }

  function contractDrift() {
    return {contract_drift: true};
  }

  function boundedTextSample(value) {
    const normalized = String(value).replace(/\s+/g, " ").trim().toLowerCase();
    if (normalized.length <= PROTECTION_TEXT_LIMIT) return normalized;
    const half = Math.floor(PROTECTION_TEXT_LIMIT / 2);
    return `${normalized.slice(0, half)} ${normalized.slice(-half)}`;
  }

  function strongProtectionText(value) {
    const sample = boundedTextSample(value);
    return /(?:\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435|\u0434\u043e\u043a\u0430\u0436\u0438\u0442\u0435|\u043f\u0440\u043e\u0439\u0434\u0438\u0442\u0435 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0443)[^.!?]{0,100}(?:\u0432\u044b )?(?:\u043d\u0435 )?\u0440\u043e\u0431\u043e\u0442|(?:verify|prove)[^.!?]{0,100}(?:you are|you're)?[^.!?]{0,40}human|complete (?:the )?captcha|captcha verification|security challenge required/.test(sample);
  }

  function htmlProtection(raw) {
    // BUG_FIX_CONTEXT: A normal hh.ru bundle contains generic captcha/challenge vocabulary.
    // Only bounded visible text is safety-classified; executable/non-visible containers and tags
    // cannot turn an otherwise valid vacancy page into a false protection response.
    const visible = raw
      .replace(/<!--[\s\S]*?-->/g, " ")
      .replace(/<(script|style|noscript|template)\b[^>]*>[\s\S]*?(?:<\/\1\s*>|$)/gi, " ")
      .replace(/<[^>]*>/g, " ");
    return strongProtectionText(decodeEntities(visible));
  }

  function explicitProtectionValue(value) {
    if (value === null || value === undefined || value === false || value === 0) return false;
    if (typeof value === "string") return !/^(?:|false|none|no|0)$/i.test(value.trim());
    return true;
  }

  function jsonProtection(source) {
    if (!source || typeof source !== "object") return false;
    const stack = [{value: source, depth: 0}];
    let visited = 0;
    while (stack.length && visited < 256) {
      const current = stack.pop();
      visited += 1;
      if (!current || current.depth > 6 || !current.value || typeof current.value !== "object") continue;
      const entries = Array.isArray(current.value)
        ? current.value.slice(0, 50).map((value, index) => [String(index), value])
        : Object.entries(current.value).slice(0, 50);
      for (const [keyValue, value] of entries) {
        const key = keyValue.toLowerCase().replace(/[_-]/g, "");
        if ((key === "captcha" || key === "challenge") && explicitProtectionValue(value)) return true;
        if (key === "protection" && typeof value === "string" && /captcha|challenge|cf-chl-/i.test(value.slice(0, 100))) return true;
        if ((key === "message" || key === "error" || key === "errormessage" || key === "detail") && typeof value === "string" && value.length <= 4096 && strongProtectionText(value)) return true;
        if (value && typeof value === "object") stack.push({value, depth: current.depth + 1});
      }
    }
    return false;
  }

  function jsonObject(raw) {
    try {
      const value = JSON.parse(raw);
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    } catch (_error) {
      return null;
    }
  }

  function publicVacancy(source, vacancyId, detailed) {
    if (!source || typeof source !== "object") return null;
    const id = identifier(vacancyId || source.id);
    const name = publicText(source.title || source.name, 500);
    if (!id || !name) return null;
    const output = {id, name};
    const description = publicText(source.description, 200000);
    if (detailed || description) output.description = description;
    const employerSource = source.hiringOrganization || source.employer;
    if (employerSource && typeof employerSource === "object") {
      output.employer = {name: publicText(employerSource.name, 500)};
    }
    const location = source.jobLocation && typeof source.jobLocation === "object" ? source.jobLocation : source.area;
    const address = location && typeof location.address === "object" ? location.address : location;
    if (address && typeof address === "object") {
      output.area = {name: publicText(address.addressLocality || address.name, 500)};
    }
    const published = source.datePosted || source.published_at;
    if (typeof published === "string") output.published_at = published.slice(0, 100);
    return output;
  }

  function jsonLdObjects(raw) {
    const values = [];
    const pattern = /<script\b[^>]*\btype\s*=\s*["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script\s*>/gi;
    let match;
    while (values.length < 50 && (match = pattern.exec(raw)) !== null) {
      const value = jsonObject(decodeEntities(match[1]));
      if (value) values.push(value);
    }
    return values;
  }

  function projectSearch(raw) {
    const found = new Map();
    for (const data of jsonLdObjects(raw)) {
      const entries = Array.isArray(data.itemListElement) ? data.itemListElement : [];
      for (const entry of entries.slice(0, SEARCH_ITEM_LIMIT)) {
        const item = entry && typeof entry === "object" ? (entry.item || entry) : null;
        const url = item && typeof item.url === "string" ? item.url : "";
        const match = url.match(/\/vacancy\/([A-Za-z0-9_-]{1,80})(?:[/?#]|$)/);
        const vacancy = match ? publicVacancy(item, match[1], false) : null;
        if (vacancy) found.set(vacancy.id, vacancy);
      }
    }
    const anchor = /<a\b[^>]*\bhref\s*=\s*["'][^"']*\/vacancy\/([A-Za-z0-9_-]{1,80})[^"']*["'][^>]*>([\s\S]*?)<\/a\s*>/gi;
    let match;
    while (found.size < SEARCH_ITEM_LIMIT && (match = anchor.exec(raw)) !== null) {
      const id = identifier(match[1]);
      const name = publicText(match[2], 500);
      if (id && name && !found.has(id)) found.set(id, {id, name});
    }
    return {items: Array.from(found.values()), found: found.size};
  }

  function projectVacancy(raw, vacancyId) {
    const id = identifier(vacancyId);
    if (!id) return contractDrift();
    for (const data of jsonLdObjects(raw)) {
      if (data["@type"] === "JobPosting") {
        const result = publicVacancy(data, id, true);
        if (result) return result;
      }
    }
    const title = raw.match(/<h1\b[^>]*>([\s\S]*?)<\/h1\s*>/i);
    const name = title ? publicText(title[1], 500) : "";
    return name ? {id, name, description: ""} : contractDrift();
  }

  function projectPopup(source) {
    const status = source.responseStatus;
    if (!status || typeof status !== "object" || Array.isArray(status)) return contractDrift();
    const test = status.test && typeof status.test === "object" ? status.test : {};
    const short = status.shortVacancy && typeof status.shortVacancy === "object" ? status.shortVacancy : {};
    const popup = source.responsePopup && typeof source.responsePopup === "object" ? source.responsePopup : {};
    const unused = Array.isArray(status.unusedResumeIds) ? status.unusedResumeIds.map(identifier).filter(Boolean).slice(0, 20) : null;
    if (typeof status.alreadyApplied !== "boolean" || typeof status.responseImpossible !== "boolean" || typeof test.hasTests !== "boolean" || typeof short.userTestPresent !== "boolean" || typeof popup.startedWithQuestion !== "boolean" || !unused) return contractDrift();
    if (!Number.isInteger(status.letterMaxLength) || status.letterMaxLength < 1 || status.letterMaxLength > 10000) return contractDrift();
    if (!status.resumes || typeof status.resumes !== "object" || Array.isArray(status.resumes)) return contractDrift();
    const resumes = {};
    for (const [resumeIdValue, resume] of Object.entries(status.resumes).slice(0, 20)) {
      const resumeId = identifier(resumeIdValue);
      const hash = resume && identifier(resume.hash);
      if (!resumeId || !hash || typeof resume.isIncomplete !== "boolean") return contractDrift();
      resumes[resumeId] = {hash, isIncomplete: resume.isIncomplete, forbidden: resume.forbidden === null || resume.forbidden === undefined ? null : Boolean(resume.forbidden)};
    }
    const countrySource = source.countryIds || status.countryIds || popup.countryIds || [];
    const countryIds = Array.isArray(countrySource) ? countrySource.map(identifier).filter(Boolean).slice(0, 20) : [];
    return {
      responseStatus: {
        alreadyApplied: status.alreadyApplied,
        responseImpossible: status.responseImpossible,
        test: {hasTests: test.hasTests},
        shortVacancy: {userTestPresent: short.userTestPresent},
        unusedResumeIds: unused,
        resumes,
        letterMaxLength: status.letterMaxLength
      },
      responsePopup: {startedWithQuestion: popup.startedWithQuestion},
      countryIds
    };
  }

  function projectChats(source) {
    const items = source.chats && typeof source.chats === "object" ? source.chats.items : source.items;
    if (!Array.isArray(items) || items.length > CHAT_ITEM_LIMIT) return contractDrift();
    const output = [];
    for (const item of items) {
      const id = item && identifier(item.id);
      const unread = item && item.unreadCount;
      if (!id || !Number.isInteger(unread) || unread < 0) return contractDrift();
      output.push({id, unreadCount: unread});
    }
    return {items: output};
  }

  function projectChatData(source) {
    const chat = source.chat;
    const states = source.chatStates;
    if (!chat || typeof chat !== "object" || !states || typeof states !== "object") return contractDrift();
    const items = chat.messages && typeof chat.messages === "object" ? chat.messages.items : null;
    const currentParticipantId = identifier(chat.currentParticipantId);
    const writeState = states.writeMessageState;
    if (!Array.isArray(items) || items.length > CHAT_ITEM_LIMIT || !currentParticipantId || !writeState || typeof writeState.allowed !== "boolean") return contractDrift();
    const messages = [];
    for (const item of items) {
      const id = item && identifier(item.id);
      const participantId = item && identifier(item.participantId || item.senderId);
      const payloadText = item && item.payload && typeof item.payload === "object" ? item.payload.text : "";
      if (!id || !participantId) return contractDrift();
      messages.push({id, participantId, text: publicText(item.text || payloadText, 5000), creationTime: typeof item.creationTime === "string" ? item.creationTime.slice(0, 100) : null});
    }
    const vacancySource = chat.resources && Array.isArray(chat.resources.VACANCY) ? chat.resources.VACANCY : [];
    const vacancies = vacancySource.map(identifier).filter(Boolean).slice(0, 20);
    return {chat: {messages: {items: messages}, currentParticipantId, resources: {VACANCY: vacancies}}, chatStates: {writeMessageState: {allowed: writeState.allowed}}};
  }

  function projectJsonAction(action, source, httpOk) {
    if (!source) return contractDrift();
    if (action === "GET_RESPONSE_POPUP") return projectPopup(source);
    if (action === "LIST_CHATS") return projectChats(source);
    if (action === "GET_CHAT_DATA") return projectChatData(source);
    if (action === "APPLY") {
      const topic = identifier(source.topic_id || source.topicId);
      const chat = identifier(source.chat_id || source.chatId);
      const success = source.success === true || source.success === "true" || source.success === "success" || source.success === "ok";
      return {success, topic_id: topic, chat_id: chat};
    }
    if (action === "SEND_CHAT_MESSAGE") return {id: identifier(source.id), chatId: identifier(source.chatId)};
    if (action === "MARK_READ") return {success: httpOk === true};
    return contractDrift();
  }

  function projectAction(action, raw, contentType, params, httpOk) {
    if (typeof raw !== "string") throw new Error("raw_response_invalid");
    const htmlAction = action === "SEARCH_VACANCIES" || action === "GET_VACANCY";
    if (byteLength(raw) > (htmlAction ? RAW_HTML_LIMIT : RAW_JSON_LIMIT)) throw new Error("response_too_large");
    if (htmlAction && htmlProtection(raw)) return {protection: "challenge"};
    if (action === "SEARCH_VACANCIES") return projectSearch(raw);
    if (action === "GET_VACANCY") return projectVacancy(raw, params.vacancy_id);
    const source = jsonObject(raw);
    if (jsonProtection(source)) return {protection: "challenge"};
    return projectJsonAction(action, source, httpOk);
  }

  const api = Object.freeze({RAW_HTML_LIMIT, RAW_JSON_LIMIT, projectAction, projectSearch, projectVacancy, publicText});
  root.JobFinderProjectors = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
