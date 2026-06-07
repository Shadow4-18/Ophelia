const $ = (id) => document.getElementById(id);

const messagesEl = $("messages");
const innerStream = $("innerStream");
const sysLog = $("sysLog");
const chatForm = $("chatForm");
const chatInput = $("chatInput");
const sendBtn = $("sendBtn");
const pauseBtn = $("pauseBtn");

let ws = null;
let paused = false;
let sending = false;

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

  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const data = await r.json();
    typing.remove();
    if (data.reply) appendMessage("assistant", data.reply);
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

chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.requestSubmit();
  }
});

connect();
chatInput.focus();
