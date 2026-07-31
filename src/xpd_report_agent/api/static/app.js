const form = document.querySelector("#chatForm");
const input = document.querySelector("#messageInput");
const sendButton = document.querySelector("#sendButton");
const messagesEl = document.querySelector("#messages");
const statusText = document.querySelector("#statusText");
const streamToggle = document.querySelector("#streamToggle");
const exampleButtons = document.querySelectorAll("[data-example]");

const history = [];

function setStatus(text, className = "") {
  statusText.textContent = text;
  statusText.className = className;
}

function renderEmpty() {
  if (messagesEl.children.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "选择示例或直接提问";
    messagesEl.append(empty);
  }
}

function clearEmpty() {
  const empty = messagesEl.querySelector(".empty");
  if (empty) empty.remove();
}

function addMessage(role, content, extraClass = "") {
  clearEmpty();
  const el = document.createElement("div");
  el.className = `message ${role}${extraClass ? ` ${extraClass}` : ""}`;
  el.textContent = content;
  messagesEl.append(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return el;
}

function setBusy(isBusy) {
  sendButton.disabled = isBusy;
  input.disabled = isBusy;
}

function parseStreamPayload(payload) {
  if (!payload || payload === "[DONE]") return "";
  try {
    const data = JSON.parse(payload);
    const choice = data.choices && data.choices[0];
    if (!choice) return "";
    if (choice.delta && typeof choice.delta.content === "string") {
      return choice.delta.content;
    }
    if (choice.message && typeof choice.message.content === "string") {
      return choice.message.content;
    }
    return "";
  } catch {
    return "";
  }
}

async function sendNonStreaming(message) {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, stream: false, history }),
  });

  const data = await response.json();
  if (!response.ok) {
    throw new Error(extractError(data));
  }
  return data.content || "";
}

async function sendStreaming(message, targetEl) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, stream: true, history }),
  });

  if (!response.ok || !response.body) {
    let data = {};
    try {
      data = await response.json();
    } catch {
      data = { detail: response.statusText };
    }
    throw new Error(extractError(data));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let content = "";
  let currentEvent = "message";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("event:")) {
        currentEvent = line.slice(6).trim() || "message";
        continue;
      }
      if (!line.startsWith("data:")) {
        continue;
      }
      const payload = line.slice(5).trim();
      if (currentEvent === "error") {
        let data = {};
        try {
          data = JSON.parse(payload);
        } catch {
          data = { message: payload };
        }
        throw new Error(extractError({ detail: data }));
      }
      if (payload === "[DONE]") {
        currentEvent = "message";
        continue;
      }
      const piece = parseStreamPayload(payload);
      if (piece) {
        content += piece;
        targetEl.textContent = content;
        messagesEl.scrollTop = messagesEl.scrollHeight;
      } else {
        let data = null;
        try {
          data = JSON.parse(payload);
        } catch {
          data = null;
        }
        if (data && (data.message || data.error)) {
          throw new Error(data.message || data.error);
        }
      }
      currentEvent = "message";
    }
  }

  return content;
}

function extractError(data) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail.message === "string") {
    return detail.error ? `${detail.message} ${detail.error}` : detail.message;
  }
  if (detail && detail.body) return String(detail.body);
  return "Request failed";
}

async function handleSubmit(event) {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) return;

  input.value = "";
  addMessage("user", message);
  history.push({ role: "user", content: message });

  const assistantEl = addMessage("assistant", "");
  setBusy(true);

  try {
    let content;
    if (streamToggle.checked) {
      content = await sendStreaming(message, assistantEl);
    } else {
      content = await sendNonStreaming(message);
      assistantEl.textContent = content;
    }
    history.push({ role: "assistant", content: content || assistantEl.textContent });
  } catch (error) {
    assistantEl.remove();
    addMessage("assistant", error.message || String(error), "error");
  } finally {
    setBusy(false);
    input.focus();
  }
}

async function checkHealth() {
  try {
    const response = await fetch("/health");
    const data = await response.json();
    if (data.hermes && data.hermes.ok) {
      setStatus("Connected", "ready");
    } else if (data.hermes_api_key_configured) {
      setStatus("Hermes offline", "error");
    } else {
      setStatus("API key missing", "error");
    }
  } catch {
    setStatus("Wrapper offline", "error");
  }
}

form.addEventListener("submit", handleSubmit);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

exampleButtons.forEach((button) => {
  button.addEventListener("click", () => {
    input.value = button.dataset.example || "";
    input.focus();
  });
});

renderEmpty();
checkHealth();
