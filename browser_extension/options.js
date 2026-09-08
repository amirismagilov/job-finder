const secretInput = document.getElementById("secret");
const status = document.getElementById("status");

function generateSecret() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  secretInput.value = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  status.textContent = "Новый секрет создан. Сохраните его и введите в локальной CLI-команде.";
}

async function load() {
  const state = await chrome.storage.local.get(["pairingSecret", "enabled"]);
  secretInput.value = state.pairingSecret || "";
  status.textContent = state.enabled ? "Bridge включён." : "Bridge выключен.";
}

document.getElementById("generate").addEventListener("click", generateSecret);
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
