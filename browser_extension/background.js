/* Closed bridge controller: no URL, method, header or script is accepted from localhost. */
const BRIDGE = "http://127.0.0.1:8766";
const TAB_LOAD_TIMEOUT_MS = 15000;
const READY_ATTEMPTS = 20;
const READY_RETRY_MS = 100;
const ACTION_ORIGIN = Object.freeze({
  SEARCH_VACANCIES: "https://hh.ru",
  GET_VACANCY: "https://hh.ru",
  GET_RESPONSE_POPUP: "https://hh.ru",
  APPLY: "https://hh.ru",
  LIST_CHATS: "https://chatik.hh.ru",
  GET_CHAT_DATA: "https://chatik.hh.ru",
  SEND_CHAT_MESSAGE: "https://chatik.hh.ru",
  MARK_READ: "https://chatik.hh.ru"
});

async function pairedSecret() {
  const state = await chrome.storage.local.get(["pairingSecret", "enabled"]);
  return state.enabled === true && typeof state.pairingSecret === "string" ? state.pairingSecret : "";
}

async function bridgeFetch(path, secret, init = {}) {
  const headers = Object.assign({}, init.headers || {}, {
    Authorization: `Bearer ${secret}`,
    "X-Job-Finder-Extension": chrome.runtime.id
  });
  return fetch(BRIDGE + path, Object.assign({}, init, {headers, cache: "no-store"}));
}

function delay(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function waitForTabComplete(tabId, timeoutMilliseconds = TAB_LOAD_TIMEOUT_MS) {
  const current = await chrome.tabs.get(tabId);
  if (current.status === "complete") return;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("tab_load_timeout"));
    }, timeoutMilliseconds);
    function listener(updatedId, changeInfo) {
      if (updatedId !== tabId || changeInfo.status !== "complete") return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function pingExecutor(tabId, attempts = READY_ATTEMPTS) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const reply = await chrome.tabs.sendMessage(tabId, {type: "JOB_FINDER_READY"});
      if (reply && reply.ready === true && reply.contract === 1) return true;
    } catch (_error) {
      // A newly loaded tab may not have installed its content scripts at the first message boundary.
    }
    await delay(READY_RETRY_MS);
  }
  return false;
}

async function executorTab(origin) {
  const tabs = await chrome.tabs.query({url: `${origin}/*`});
  let tabId;
  if (tabs.length && Number.isInteger(tabs[0].id)) {
    tabId = tabs[0].id;
  } else {
    const tab = await chrome.tabs.create({url: origin + "/", active: false});
    if (!Number.isInteger(tab.id)) throw new Error("tab_unavailable");
    tabId = tab.id;
  }
  await waitForTabComplete(tabId);
  if (await pingExecutor(tabId)) return tabId;
  // BUG_FIX_CONTEXT: An existing tab loaded before extension installation has no content script;
  // a single bounded reload plus explicit readiness handshake replaces the fragile fixed sleep.
  await chrome.tabs.reload(tabId);
  await waitForTabComplete(tabId);
  if (!await pingExecutor(tabId)) throw new Error("executor_not_ready");
  return tabId;
}

async function dispatchAllowedAction(command) {
  if (!command || !Object.hasOwn(ACTION_ORIGIN, command.action)) throw new Error("action_not_allowed");
  if (!command.params || typeof command.params !== "object" || Array.isArray(command.params)) throw new Error("params_invalid");
  const tabId = await executorTab(ACTION_ORIGIN[command.action]);
  const result = await chrome.tabs.sendMessage(tabId, {type: "JOB_FINDER_EXECUTE", command});
  if (!result || result.command_id !== command.command_id) throw new Error("executor_contract_error");
  return result;
}

async function pollCommand() {
  const secret = await pairedSecret();
  if (!secret) return;
  let response;
  try {
    response = await bridgeFetch("/v1/commands/next", secret);
    if (!response.ok) return;
    const payload = await response.json();
    if (!payload.command) return;
    let result;
    try {
      result = await dispatchAllowedAction(payload.command);
    } catch (_error) {
      result = {command_id: payload.command.command_id, status: 0, content_type: "application/json", body: {executor_error: true}, redirect_path: ""};
    }
    await bridgeFetch(`/v1/commands/${payload.command.command_id}/result`, secret, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(result)
    });
  } catch (_error) {
    // Local bridge can legitimately be offline. Credentials and response bodies are never logged.
  }
}

chrome.runtime.onInstalled.addListener(() => chrome.runtime.openOptionsPage());
chrome.alarms.create("job-finder-poll", {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === "job-finder-poll") pollCommand(); });
chrome.runtime.onMessage.addListener(message => { if (message && message.type === "JOB_FINDER_POLL_NOW") pollCommand(); });
pollCommand();
