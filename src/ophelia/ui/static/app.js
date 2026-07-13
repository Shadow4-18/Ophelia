const $ = (id) => document.getElementById(id);

const messagesEl = $("messages");
const innerStream = $("innerStream");
const sysLog = $("sysLog");
const chatForm = $("chatForm");
const chatInput = $("chatInput");
const sendBtn = $("sendBtn");
const pauseBtn = $("pauseBtn");
const panelToggle = $("panelToggle");
const sideDrawer = $("sideDrawer");

let ws = null;
let paused = false;
let sending = false;

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

function pct(v, min = 0, max = 1) {
  const n = Math.max(min, Math.min(max, Number(v) || 0));
  return `${((n - min) / (max - min)) * 100}%`;
}

function applyAvatar(data) {
  if (!data) return;
  stage.apply(data);
  $("exprLabel").textContent = data.expression || "neutral";
  $("backendLabel").textContent = data.backend || "procedural";
  $("footAvatar").textContent = data.enabled === false
    ? "avatar off"
    : `avatar ${data.backend || "procedural"}${data.speaking ? " · speaking" : ""}`;
  const line = data.speaking
    ? "speaking…"
    : (data.backend === "vroid" || data.backend === "vrchat") && !stage._webgl?.ready
      ? (stage._webgl?.loading || stage._webglLoading
          ? `loading ${data.backend === "vrchat" ? "VRChat" : "VRoid"}…`
          : (stage._webgl?.error
              ? `${data.backend} load failed`
              : `${data.expression || "neutral"} presence`))
      : data.expression
        ? `${data.expression} presence`
        : "presence online";
  $("stageLine").textContent = line;
}

function renderStatus(data) {
  if (!data) return;
  $("statusDot").classList.toggle("live", data.ready);
  $("runtimeLabel").textContent = data.runtime || "—";
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
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    logSystem("connected");
    $("statusDot").classList.add("live");
  };

  ws.onclose = () => {
    logSystem("disconnected — retrying");
    $("statusDot").classList.remove("live");
    setTimeout(connect, 2500);
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
    // Reply also arrives via WS; avoid duplicate if already streamed
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

panelToggle.addEventListener("click", () => {
  const open = sideDrawer.hasAttribute("hidden");
  if (open) sideDrawer.removeAttribute("hidden");
  else sideDrawer.setAttribute("hidden", "");
  panelToggle.setAttribute("aria-expanded", open ? "true" : "false");
  panelToggle.textContent = open ? "hide state" : "state";
  document.body.classList.toggle("drawer-open", open);
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
  const cssH = Math.max(360, Math.floor(rect.height));
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
connect();
chatInput.focus();
