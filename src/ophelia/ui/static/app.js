const $ = (id) => document.getElementById(id);

const messagesEl = $("messages");
const innerStream = $("innerStream");
const sysLog = $("sysLog");
const chatForm = $("chatForm");
const chatInput = $("chatInput");
const sendBtn = $("sendBtn");
const pauseBtn = $("pauseBtn");
const panelToggle = $("panelToggle");
const modelsToggle = $("modelsToggle");
const sideDrawer = $("sideDrawer");
const connLabel = $("connLabel");
const modelSelect = $("modelSelect");
const modelCustom = $("modelCustom");
const modelRole = $("modelRole");
const modelStatus = $("modelStatus");
const compareA = $("compareA");
const compareB = $("compareB");

let ws = null;
let paused = false;
let sending = false;
let liveViaWs = false;
let reconnectTimer = null;
let pollTimer = null;
let modelsCache = null;
let activeTab = "models";

const stage = new OpheliaAvatarStage($("avatarCanvas"), $("vrmCanvas"));
stage.start();

function appendMessage(role, content, extraClass = "") {
  const div = document.createElement("div");
  div.className = `msg ${role} ${extraClass}`.trim();
  div.textContent = content;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function logSystem(text) {
  const line = `[${new Date().toLocaleTimeString()}] ${text}\n`;
  sysLog.textContent = (sysLog.textContent + line).slice(-2000);
}

function setConnState(state, detail) {
  const dot = $("statusDot");
  dot.classList.remove("live", "warn", "off");
  if (state === "live") {
    dot.classList.add("live");
    connLabel.textContent = detail || "live";
  } else if (state === "reconnecting" || state === "connecting") {
    dot.classList.add("warn");
    connLabel.textContent = detail || state;
  } else {
    dot.classList.add("off");
    connLabel.textContent = detail || "offline";
  }
}

function pct(v, min = 0, max = 1) {
  const n = Math.max(min, Math.min(max, Number(v) || 0));
  return `${((n - min) / (max - min)) * 100}%`;
}

function applyAvatar(data) {
  if (!data) return;
  stage.apply(data);
  $("exprLabel").textContent = data.expression || "neutral";
  const backend = data.backend || "procedural";
  const activity = data.activity || (data.speaking ? "speaking" : "idle");
  $("backendLabel").textContent = `${backend} · ${activity}`;
  $("footAvatar").textContent = data.enabled === false
    ? "avatar off"
    : `avatar ${backend} · ${activity}${data.animation ? ` · ${data.animation}` : ""}`;
  let line;
  if (data.speaking || activity === "speaking") {
    line = data.viseme && data.viseme !== "sil" ? `speaking · ${data.viseme}` : "speaking…";
  } else if (activity === "thinking") {
    line = data.thought_snippet ? `thinking — ${data.thought_snippet.slice(0, 48)}` : "thinking…";
  } else if (activity === "listening") {
    line = "listening…";
  } else if (activity === "reacting") {
    line = `${data.expression || "neutral"} reaction`;
  } else if ((backend === "vroid" || backend === "vrchat") && !stage._webgl?.ready) {
    line = stage._webgl?.loading || stage._webglLoading
      ? `loading ${backend === "vrchat" ? "VRChat" : "VRoid"}…`
      : (stage._webgl?.error ? `${backend} load failed` : `${data.expression || "neutral"} presence`);
  } else {
    line = data.expression ? `${data.expression} presence` : "presence online";
  }
  $("stageLine").textContent = line;
}

function renderStatus(data) {
  if (!data) return;
  $("providerLabel").textContent = `${data.chat_provider || "?"} · ${data.chat_model || "?"}`;
  $("footModel").textContent = `model ${data.chat_model || "—"}`;
  $("footPressure").textContent = `pressure ${data.drives?.pressure ?? "—"}`;
  $("footMind").textContent = data.consciousness_paused
    ? "mind paused"
    : data.consciousness
      ? "mind active"
      : "mind off";

  const mood = data.mood || {};
  $("moodLabel").textContent = mood.label || "—";
  $("moodCore").classList.toggle("pulse", (mood.arousal || 0) > 0.55);
  $("valenceBar").style.width = pct((mood.valence || 0) + 1, 0, 2);
  $("arousalBar").style.width = pct(mood.arousal || 0);

  const feelings = $("feelings");
  feelings.innerHTML = "";
  (data.feelings || []).forEach((f) => {
    const s = document.createElement("span");
    s.className = "chip";
    s.textContent = f;
    feelings.appendChild(s);
  });

  const drivesEl = $("drives");
  drivesEl.innerHTML = "";
  const d = data.drives || {};
  ["social", "curiosity", "boredom", "agency", "expressiveness"].forEach((key) => {
    const row = document.createElement("div");
    row.className = "drive-row";
    row.innerHTML = `<span>${key}</span><div class="bar"><div class="fill" style="width:${pct(d[key])}"></div></div><span>${d[key] ?? ""}</span>`;
    drivesEl.appendChild(row);
  });

  const urges = $("urges");
  urges.innerHTML = "";
  (data.urges || []).forEach((u) => {
    const li = document.createElement("li");
    li.textContent = u;
    urges.appendChild(li);
  });

  $("thought").textContent = data.thought ? `"${data.thought}"` : "";
  paused = !!data.consciousness_paused;
  pauseBtn.textContent = paused ? "resume mind" : "pause mind";

  if (data.avatar) applyAvatar(data.avatar);
  syncModelSelectsFromStatus(data);
}

function fillSelect(el, options, selected) {
  const prev = selected || el.value;
  el.innerHTML = "";
  const seen = new Set();
  options.forEach((name) => {
    if (!name || seen.has(name)) return;
    seen.add(name);
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    el.appendChild(opt);
  });
  if (prev && [...el.options].some((o) => o.value === prev)) {
    el.value = prev;
  }
}

function syncModelSelectsFromStatus(data) {
  if (!modelRole || !modelsCache) return;
  const role = modelRole.value || "chat";
  const routing = modelsCache.routing || {};
  const current = routing[role] || data.chat_model;
  if (current && modelSelect && [...modelSelect.options].some((o) => o.value === current)) {
    modelSelect.value = current;
  }
}

function renderModels(info) {
  modelsCache = info || {};
  const installed = info.installed || [];
  const routing = info.routing || {};
  const providers = info.providers || {};
  const recommended = info.recommended || [];

  const routingEl = $("modelRouting");
  routingEl.innerHTML = "";
  ["chat", "consciousness", "vision"].forEach((role) => {
    const row = document.createElement("div");
    row.className = "route-row";
    row.innerHTML = `<span>${role}</span><span class="mono">${providers[role] || "—"}</span><span class="mono accent">${routing[role] || "—"}</span>`;
    routingEl.appendChild(row);
  });

  const role = modelRole.value || "chat";
  const current = routing[role] || info.chat_model || "";
  const options = [...installed];
  if (current && !options.includes(current)) options.unshift(current);
  if (!options.length) options.push(current || "llama3.2:1b");

  fillSelect(modelSelect, options, current);
  fillSelect(compareA, options, options[0]);
  fillSelect(compareB, options, options[1] || options[0]);

  const inst = $("installedList");
  inst.innerHTML = "";
  if (!installed.length) {
    const li = document.createElement("li");
    li.textContent = "No Ollama models listed (is ollama serve running?)";
    inst.appendChild(li);
  } else {
    installed.forEach((m) => {
      const li = document.createElement("li");
      li.textContent = m;
      if (m === routing.chat) li.classList.add("active");
      li.tabIndex = 0;
      li.addEventListener("click", () => {
        modelRole.value = "chat";
        modelSelect.value = m;
        modelCustom.value = "";
      });
      inst.appendChild(li);
    });
  }

  const rec = $("recommendedList");
  rec.innerHTML = "";
  recommended.slice(0, 8).forEach((r) => {
    const li = document.createElement("li");
    const pull = r.pull || r;
    li.textContent = typeof pull === "string"
      ? `${pull}${r.role ? ` · ${r.role}` : ""}${r.ram_gb ? ` · ~${r.ram_gb}GB` : ""}`
      : String(pull);
    li.tabIndex = 0;
    li.addEventListener("click", () => {
      modelCustom.value = typeof pull === "string" ? pull : "";
    });
    rec.appendChild(li);
  });

  const profile = info.profile || {};
  $("modelsHint").textContent = profile.ram_gb
    ? `System ~${profile.ram_gb}GB RAM${profile.gpu ? ` · ${profile.gpu}` : ""}. Select a model for chat or consciousness.`
    : "Pick which model handles chat and mind.";
}

async function loadModels() {
  modelStatus.textContent = "loading models…";
  try {
    const r = await fetch("/api/models");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const info = await r.json();
    renderModels(info);
    modelStatus.textContent = `${(info.installed || []).length} installed · chat ${info.chat_model || "—"}`;
  } catch (err) {
    modelStatus.textContent = `Could not load models: ${err.message}`;
  }
}

async function applyModel() {
  const role = modelRole.value || "chat";
  const model = (modelCustom.value.trim() || modelSelect.value || "").trim();
  if (!model) {
    modelStatus.textContent = "Choose or type a model first.";
    return;
  }
  modelStatus.textContent = `switching ${role} → ${model}…`;
  $("modelApplyBtn").disabled = true;
  try {
    const r = await fetch("/api/models/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        role,
        model,
        persist: $("modelPersist").checked,
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    renderModels(data);
    modelCustom.value = "";
    const sel = data.selected || {};
    modelStatus.textContent = sel.persisted
      ? `Using ${sel.model} for ${sel.role} (saved to .env)`
      : `Using ${sel.model} for ${sel.role} (this session)`;
    logSystem(`model ${sel.role} → ${sel.model}`);
  } catch (err) {
    modelStatus.textContent = `Select failed: ${err.message}`;
  } finally {
    $("modelApplyBtn").disabled = false;
  }
}

async function runCompare() {
  const message = ($("comparePrompt").value || "").trim() || "Say hello in one short line.";
  const models = [compareA.value, compareB.value].filter(Boolean);
  const uniq = [...new Set(models)].slice(0, 2);
  if (!uniq.length) {
    $("compareResults").textContent = "Pick at least one model.";
    return;
  }
  $("compareBtn").disabled = true;
  $("compareResults").textContent = "comparing…";
  try {
    const r = await fetch("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, models: uniq }),
    });
    const data = await r.json();
    const box = $("compareResults");
    box.innerHTML = "";
    (data.results || []).forEach((row) => {
      const block = document.createElement("div");
      block.className = "compare-card";
      const h = document.createElement("strong");
      h.textContent = row.model;
      const p = document.createElement("pre");
      p.textContent = row.reply || "(empty)";
      block.appendChild(h);
      block.appendChild(p);
      box.appendChild(block);
    });
  } catch (err) {
    $("compareResults").textContent = `Compare failed: ${err.message}`;
  } finally {
    $("compareBtn").disabled = false;
  }
}

async function pollStatus() {
  try {
    const r = await fetch("/api/status");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    renderStatus(data);
    if (!liveViaWs) {
      setConnState("live", "http · live");
    }
  } catch {
    if (!liveViaWs) setConnState("offline", "offline");
  }
}

function startPolling() {
  if (pollTimer) return;
  pollStatus();
  pollTimer = setInterval(pollStatus, 4000);
}

function stopPollingSoft() {
  // Keep a slow poll even when WS is up so status stays fresh if events stall.
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  pollTimer = setInterval(pollStatus, 15000);
}

function connect() {
  setConnState("connecting", "connecting…");
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  try {
    ws = new WebSocket(`${proto}//${location.host}/ws`);
  } catch {
    liveViaWs = false;
    setConnState("reconnecting", "ws failed · http");
    startPolling();
    reconnectTimer = setTimeout(connect, 2500);
    return;
  }

  ws.onopen = () => {
    liveViaWs = true;
    setConnState("live", "live");
    logSystem("websocket connected");
    stopPollingSoft();
  };

  ws.onclose = () => {
    liveViaWs = false;
    setConnState("reconnecting", "reconnecting…");
    logSystem("disconnected — retrying");
    startPolling();
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2500);
  };

  ws.onerror = () => {
    liveViaWs = false;
    setConnState("reconnecting", "ws error · http");
    startPolling();
  };

  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    switch (msg.type) {
      case "status":
        renderStatus(msg.data);
        break;
      case "avatar":
        applyAvatar(msg.data);
        break;
      case "chat":
        appendMessage(msg.role === "user" ? "user" : "assistant", msg.text || msg.content || "");
        break;
      case "initiative":
        appendMessage("assistant", msg.text, "initiative");
        break;
      case "inner":
        innerStream.textContent += `\n💭 ${msg.text}\n`;
        innerStream.scrollTop = innerStream.scrollHeight;
        break;
      case "inner_block":
        if (msg.text) innerStream.textContent = msg.text;
        break;
      case "system":
        logSystem(msg.text);
        appendMessage("system", msg.text, "system");
        break;
      default:
        break;
    }
  };
}

function openDrawer(tab) {
  sideDrawer.removeAttribute("hidden");
  document.body.classList.add("drawer-open");
  setTab(tab || activeTab || "models");
  modelsToggle.setAttribute("aria-expanded", "true");
  panelToggle.setAttribute("aria-expanded", "true");
  modelsToggle.textContent = "models";
  panelToggle.textContent = "state";
  if (tab === "models" || activeTab === "models") loadModels();
}

function closeDrawer() {
  sideDrawer.setAttribute("hidden", "");
  document.body.classList.remove("drawer-open");
  modelsToggle.setAttribute("aria-expanded", "false");
  panelToggle.setAttribute("aria-expanded", "false");
}

function setTab(name) {
  activeTab = name;
  document.querySelectorAll(".drawer-tab").forEach((btn) => {
    const on = btn.dataset.tab === name;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".drawer-pane").forEach((pane) => {
    const on = pane.id === `pane${name.charAt(0).toUpperCase()}${name.slice(1)}`;
    pane.classList.toggle("active", on);
    if (on) pane.removeAttribute("hidden");
    else pane.setAttribute("hidden", "");
  });
}

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = chatInput.value.trim();
  if (!text || sending) return;
  sending = true;
  sendBtn.disabled = true;
  appendMessage("user", text);
  chatInput.value = "";

  const typing = document.createElement("div");
  typing.className = "msg assistant typing";
  typing.textContent = "Ophelia";
  messagesEl.appendChild(typing);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  $("stageLine").textContent = "thinking…";

  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const data = await r.json();
    typing.remove();
    if (data.reply) {
      const last = messagesEl.lastElementChild;
      const already =
        last &&
        last.classList.contains("assistant") &&
        last.textContent === data.reply;
      if (!already) appendMessage("assistant", data.reply);
    }
  } catch (err) {
    typing.remove();
    appendMessage("system", `Error: ${err.message}`, "system");
  } finally {
    sending = false;
    sendBtn.disabled = false;
    chatInput.focus();
  }
});

pauseBtn.addEventListener("click", async () => {
  const path = paused ? "/api/consciousness/resume" : "/api/consciousness/pause";
  await fetch(path, { method: "POST" });
  paused = !paused;
  pauseBtn.textContent = paused ? "resume mind" : "pause mind";
  logSystem(paused ? "consciousness paused" : "consciousness resumed");
});

modelsToggle.addEventListener("click", () => {
  const open = sideDrawer.hasAttribute("hidden");
  if (open) openDrawer("models");
  else if (activeTab !== "models") setTab("models");
  else closeDrawer();
});

panelToggle.addEventListener("click", () => {
  const open = sideDrawer.hasAttribute("hidden");
  if (open) openDrawer("state");
  else if (activeTab !== "state") setTab("state");
  else closeDrawer();
});

document.querySelectorAll(".drawer-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    setTab(btn.dataset.tab);
    if (btn.dataset.tab === "models") loadModels();
  });
});

$("modelApplyBtn").addEventListener("click", applyModel);
$("modelRefreshBtn").addEventListener("click", loadModels);
$("compareBtn").addEventListener("click", runCompare);
modelRole.addEventListener("change", () => {
  if (modelsCache) renderModels(modelsCache);
});

chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.requestSubmit();
  }
});

function resizeCanvas() {
  const canvas = $("avatarCanvas");
  const vrm = $("vrmCanvas");
  const panel = canvas.parentElement;
  const rect = panel.getBoundingClientRect();
  const cssW = Math.max(280, Math.floor(rect.width));
  const cssH = Math.max(200, Math.floor(rect.height));
  canvas.width = cssW;
  canvas.height = cssH;
  if (vrm) {
    vrm.width = cssW;
    vrm.height = cssH;
  }
  stage.resize();
}

window.addEventListener("resize", resizeCanvas);
resizeCanvas();
startPolling();
connect();
chatInput.focus();
