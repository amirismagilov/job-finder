const secretInput = document.getElementById("secret");
const status = document.getElementById("status");
const revealButton = document.getElementById("reveal");

function generateSecret() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  secretInput.value = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  status.textContent = "Новый секрет создан. Сохраните его и введите в локальной CLI-команде.";
}

async function copySecret() {
  const value = secretInput.value;
  if (!/^[0-9a-f]{64}$/.test(value)) {
    status.textContent = "Сначала создайте корректный секрет.";
    return;
  }
  let copied = false;
  try {
    await navigator.clipboard.writeText(value);
    copied = true;
  } catch (_error) {
    // BUG_FIX_CONTEXT: На некоторых Chromium-сборках Clipboard API недоступен странице
    // настроек; синхронный fallback в обработчике клика сохраняет ручной pairing рабочим.
    const originalType = secretInput.type;
    secretInput.type = "text";
    secretInput.focus();
    secretInput.select();
    secretInput.setSelectionRange(0, value.length);
    copied = document.execCommand("copy");
    secretInput.type = originalType;
  }
  status.textContent = copied
    ? "Секрет скопирован. Вставьте его в скрытый запрос команды bridge pair."
    : "Автокопирование недоступно. Нажмите «Показать» и скопируйте секрет вручную.";
}

function toggleSecretVisibility() {
  const reveal = secretInput.type === "password";
  secretInput.type = reveal ? "text" : "password";
  revealButton.textContent = reveal ? "Скрыть" : "Показать";
  status.textContent = reveal
    ? "Секрет показан только на этой локальной странице настроек."
    : "Секрет снова скрыт.";
}

function connectionMessage(result) {
  const messages = {
    connected_idle: "Соединение установлено: bridge доступен, активных команд нет.",
    command_completed: "Соединение установлено: команда получена и обработана.",
    secret_missing: "Сначала создайте и сохраните pairing secret.",
    pairing_rejected: "Bridge отклонил pairing secret. Создайте новый секрет и повторите bridge pair.",
    bridge_unreachable: "Локальный bridge недоступен. Сначала запустите bridge или dry-run в Терминале.",
    bridge_http_error: "Bridge вернул неожиданный HTTP-ответ.",
    result_rejected: "Bridge получил команду, но отклонил её результат."
  };
  return messages[result && result.code] || "Не удалось определить состояние bridge.";
}

async function checkConnection() {
  status.textContent = "Проверяю локальное соединение…";
  try {
    const result = await chrome.runtime.sendMessage({type: "JOB_FINDER_POLL_NOW"});
    status.textContent = connectionMessage(result);
  } catch (_error) {
    status.textContent = "Service worker расширения недоступен. Перезагрузите расширение.";
  }
}

async function load() {
  const state = await chrome.storage.local.get(["pairingSecret", "enabled"]);
  secretInput.value = state.pairingSecret || "";
  status.textContent = state.enabled ? "Bridge включён." : "Bridge выключен.";
}

document.getElementById("generate").addEventListener("click", generateSecret);
document.getElementById("copy").addEventListener("click", copySecret);
revealButton.addEventListener("click", toggleSecretVisibility);
document.getElementById("save").addEventListener("click", async () => {
  if (!/^[0-9a-f]{64}$/.test(secretInput.value)) { status.textContent = "Сначала создайте корректный секрет."; return; }
  await chrome.storage.local.set({pairingSecret: secretInput.value, enabled: true});
  await checkConnection();
});
document.getElementById("check").addEventListener("click", checkConnection);
document.getElementById("disable").addEventListener("click", async () => {
  await chrome.storage.local.set({enabled: false});
  status.textContent = "Bridge выключен.";
});
load();
