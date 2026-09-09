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
  chrome.runtime.sendMessage({type: "JOB_FINDER_POLL_NOW"});
  status.textContent = "Bridge включён.";
});
document.getElementById("disable").addEventListener("click", async () => {
  await chrome.storage.local.set({enabled: false});
  status.textContent = "Bridge выключен.";
});
load();
