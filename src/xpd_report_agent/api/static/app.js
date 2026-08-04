const form = document.querySelector("#chatForm");
const input = document.querySelector("#messageInput");
const sendButton = document.querySelector("#sendButton");
const messagesEl = document.querySelector("#messages");
const statusText = document.querySelector("#statusText");
const streamToggle = document.querySelector("#streamToggle");
const exampleButtons = document.querySelectorAll("[data-example]");
const sessionListEl = document.querySelector("#sessionList");
const currentSessionTitle = document.querySelector("#currentSessionTitle");
const newChatButton = document.querySelector("#newChatButton");
const renameChatButton = document.querySelector("#renameChatButton");
const endChatButton = document.querySelector("#endChatButton");
const chatPanel = document.querySelector("#chatPanel");
const memoryPanel = document.querySelector("#memoryPanel");
const memoryViewButton = document.querySelector("#memoryViewButton");
const refreshMemoryButton = document.querySelector("#refreshMemoryButton");
const backToChatButton = document.querySelector("#backToChatButton");
const memoryFilesEl = document.querySelector("#memoryFiles");

const SESSION_KEY_STORAGE = "xpd-report-agent.session-key.v1";
const CURRENT_SESSION_STORAGE = "xpd-report-agent.current-session.v1";

let sessions = [];
let currentSession = null;
let busy = false;
let memoryBusy = false;

function ensureSessionKey() {
  let key = localStorage.getItem(SESSION_KEY_STORAGE);
  if (key && key.length >= 24) return key;

  if (crypto.randomUUID) {
    key = `${crypto.randomUUID()}-${crypto.randomUUID()}`;
  } else {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    key = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  localStorage.setItem(SESSION_KEY_STORAGE, key);
  return key;
}

const clientSessionKey = ensureSessionKey();

function sessionHeaders(json = true) {
  const headers = { "X-XPD-Session-Key": clientSessionKey };
  if (json) headers["Content-Type"] = "application/json";
  return headers;
}

async function apiRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { ...sessionHeaders(true), ...(options.headers || {}) },
  });
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = { detail: response.statusText };
  }
  if (!response.ok) throw new Error(extractError(data));
  return data;
}

function setStatus(text, className = "") {
  statusText.textContent = text;
  statusText.className = className;
}

function renderEmpty(text = "选择示例或直接提问") {
  if (messagesEl.children.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = text;
    messagesEl.append(empty);
  }
}

function clearMessages() {
  messagesEl.replaceChildren();
}

function clearEmpty() {
  messagesEl.querySelector(".empty")?.remove();
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

function addAssistantMessage(content = "", reasoning = "") {
  clearEmpty();
  const messageEl = document.createElement("div");
  messageEl.className = "message assistant structured";

  const reasoningDetails = document.createElement("details");
  reasoningDetails.className = "reasoning-panel";
  reasoningDetails.hidden = true;
  const reasoningSummary = document.createElement("summary");
  const reasoningLabel = document.createElement("span");
  reasoningLabel.textContent = "模型思考";
  const reasoningCount = document.createElement("span");
  reasoningCount.className = "reasoning-count";
  const reasoningEl = document.createElement("pre");
  reasoningEl.className = "reasoning-content";
  reasoningSummary.append(reasoningLabel, reasoningCount);
  reasoningDetails.append(reasoningSummary, reasoningEl);

  const contentEl = document.createElement("div");
  contentEl.className = "answer-content";
  contentEl.textContent = content;
  messageEl.append(reasoningDetails, contentEl);
  messagesEl.append(messageEl);

  const view = {
    messageEl,
    contentEl,
    reasoningDetails,
    reasoningEl,
    reasoningLabel,
    reasoningCount,
  };
  setAssistantReasoning(view, reasoning);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return view;
}

function setAssistantReasoning(view, reasoning, { live = false } = {}) {
  const text = typeof reasoning === "string" ? reasoning : "";
  if (!text.trim()) return;
  const isFirstVisibleReasoning = view.reasoningDetails.hidden;
  view.reasoningEl.textContent = text;
  view.reasoningLabel.textContent = live ? "模型思考中" : "模型思考";
  view.reasoningCount.textContent = `${text.length.toLocaleString("zh-CN")} 字`;
  view.reasoningDetails.hidden = false;
  if (live && isFirstVisibleReasoning) view.reasoningDetails.open = true;
  if (live && view.reasoningDetails.open) {
    view.reasoningEl.scrollTop = view.reasoningEl.scrollHeight;
  }
}

function setBusy(isBusy) {
  busy = isBusy;
  const readOnly = !currentSession || currentSession.status !== "active";
  sendButton.disabled = isBusy || readOnly;
  input.disabled = isBusy || readOnly;
  newChatButton.disabled = isBusy;
  renameChatButton.disabled = isBusy || !currentSession;
  endChatButton.disabled = isBusy || readOnly;
  form.classList.toggle("readonly", readOnly);
  input.placeholder = readOnly ? "该对话已结束，只能查看历史" : "输入数据问题";
}

function formatMemoryTime(value) {
  if (!value) return "文件尚未创建";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value * 1000));
}

function renderMemoryFiles(files) {
  memoryFilesEl.replaceChildren();
  for (const item of files) {
    const card = document.createElement("article");
    card.className = `memory-file${item.at_watermark ? " warning" : ""}`;

    const heading = document.createElement("div");
    heading.className = "memory-file-heading";
    const titleGroup = document.createElement("div");
    const label = document.createElement("h3");
    label.textContent = item.label;
    const filename = document.createElement("code");
    filename.textContent = item.filename;
    titleGroup.append(label, filename);

    const state = document.createElement("span");
    state.className = "memory-state";
    state.textContent = item.at_watermark ? "达到整理水位" : "正常";
    heading.append(titleGroup, state);

    const usage = document.createElement("div");
    usage.className = "memory-usage";
    const ratio = Math.round(item.usage_ratio * 100);
    const usageText = document.createElement("span");
    usageText.textContent = `${item.used_chars.toLocaleString("zh-CN")} / ${item.limit_chars.toLocaleString("zh-CN")} 字符（${ratio}%）`;
    const modified = document.createElement("span");
    modified.textContent = `更新于 ${formatMemoryTime(item.modified_at)}`;
    usage.append(usageText, modified);

    const progress = document.createElement("progress");
    progress.max = item.limit_chars;
    progress.value = Math.min(item.used_chars, item.limit_chars);
    progress.setAttribute("aria-label", `${item.filename} 容量`);

    const content = document.createElement("pre");
    content.className = "memory-file-content";
    content.textContent = item.exists ? item.content || "（空文件）" : "（文件尚未创建）";

    card.append(heading, usage, progress, content);
    memoryFilesEl.append(card);
  }
}

async function loadMemoryFiles() {
  if (memoryBusy) return;
  memoryBusy = true;
  refreshMemoryButton.disabled = true;
  memoryFilesEl.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "memory-loading";
  loading.textContent = "正在读取记忆文件…";
  memoryFilesEl.append(loading);
  try {
    const data = await apiRequest("/api/memories");
    renderMemoryFiles(data.data || []);
  } catch (error) {
    loading.className = "memory-loading error";
    loading.textContent = error.message || String(error);
  } finally {
    memoryBusy = false;
    refreshMemoryButton.disabled = false;
  }
}

async function showMemoryView() {
  chatPanel.hidden = true;
  memoryPanel.hidden = false;
  memoryViewButton.classList.add("active");
  await loadMemoryFiles();
}

function showChatView() {
  memoryPanel.hidden = true;
  chatPanel.hidden = false;
  memoryViewButton.classList.remove("active");
  input.focus();
}

function formatSessionTime(value) {
  if (!value) return "刚刚";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function renderSessionList() {
  sessionListEl.replaceChildren();
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "session-empty";
    empty.textContent = "暂无历史对话";
    sessionListEl.append(empty);
    return;
  }

  for (const session of sessions) {
    const item = document.createElement("div");
    item.className = `session-item${
      currentSession?.session_id === session.session_id ? " active" : ""
    }`;

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "session-open";
    openButton.title = session.title;

    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = session.title || "新对话";

    const meta = document.createElement("span");
    meta.className = "session-meta";
    const status = session.status === "closed" ? "已结束" : `${session.completed_turn_count} 轮`;
    meta.textContent = `${status} · ${formatSessionTime(session.last_active_at)}`;

    openButton.append(title, meta);
    openButton.addEventListener("click", () => openSession(session.session_id));

    const menu = document.createElement("div");
    menu.className = "session-menu";
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.title = "删除对话";
    deleteButton.setAttribute("aria-label", `删除 ${session.title || "对话"}`);
    deleteButton.textContent = "×";
    deleteButton.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteSession(session);
    });
    menu.append(deleteButton);
    item.append(openButton, menu);
    sessionListEl.append(item);
  }
}

function updateCurrentSession(session) {
  currentSession = session || null;
  currentSessionTitle.textContent = currentSession?.title || "新对话";
  if (currentSession) {
    localStorage.setItem(CURRENT_SESSION_STORAGE, currentSession.session_id);
  } else {
    localStorage.removeItem(CURRENT_SESSION_STORAGE);
  }
  renderSessionList();
  setBusy(busy);
}

async function refreshSessions() {
  const data = await apiRequest("/api/sessions?limit=100");
  sessions = data.data || [];
  if (currentSession) {
    const fresh = sessions.find((item) => item.session_id === currentSession.session_id);
    if (fresh) currentSession = fresh;
  }
  updateCurrentSession(currentSession);
  return sessions;
}

function messageText(value) {
  if (typeof value === "string") return value;
  return value ? JSON.stringify(value, null, 2) : "";
}

function renderHistoryMessages(messages) {
  let reasoningParts = [];
  let answerParts = [];

  function flushAssistantTurn() {
    if (!reasoningParts.length && !answerParts.length) return;
    const finalAnswer = answerParts.at(-1) || "";
    addAssistantMessage(finalAnswer, reasoningParts.join("\n\n"));
    reasoningParts = [];
    answerParts = [];
  }

  for (const message of messages || []) {
    if (message.role === "user") {
      flushAssistantTurn();
      const content = messageText(message.content);
      if (content) addMessage("user", content);
      continue;
    }
    if (message.role !== "assistant") continue;

    if (typeof message.reasoning === "string" && message.reasoning.trim()) {
      reasoningParts.push(message.reasoning);
    }
    const content = messageText(message.content);
    if (content) answerParts.push(content);
  }
  flushAssistantTurn();
}

async function loadMessages(sessionId) {
  clearMessages();
  renderEmpty("正在加载历史记录…");
  const data = await apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
  clearMessages();
  renderHistoryMessages(data.data);
  renderEmpty(currentSession?.status === "closed" ? "该对话已结束" : undefined);
  if (data.session_id && data.session_id !== sessionId) {
    localStorage.setItem(CURRENT_SESSION_STORAGE, data.session_id);
  }
}

async function openSession(sessionId) {
  const session = sessions.find((item) => item.session_id === sessionId);
  if (!session) return;
  updateCurrentSession(session);
  try {
    await loadMessages(sessionId);
  } catch (error) {
    clearMessages();
    addMessage("assistant", error.message || String(error), "error");
  }
}

async function createSession({ closeCurrent = false } = {}) {
  setBusy(true);
  try {
    const body = {};
    if (closeCurrent && currentSession?.status === "active") {
      body.close_session_id = currentSession.session_id;
    }
    const data = await apiRequest("/api/sessions", {
      method: "POST",
      body: JSON.stringify(body),
    });
    sessions = [data.session, ...sessions.filter((item) => item.session_id !== data.session.session_id)];
    updateCurrentSession(data.session);
    clearMessages();
    renderEmpty();
    await refreshSessions();
  } finally {
    setBusy(false);
    input.focus();
  }
}

async function renameSession(session) {
  const title = window.prompt("输入新的对话名称", session.title || "新对话")?.trim();
  if (!title || title === session.title) return;
  try {
    const data = await apiRequest(`/api/sessions/${encodeURIComponent(session.session_id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
    sessions = sessions.map((item) =>
      item.session_id === session.session_id ? data.session : item,
    );
    if (currentSession?.session_id === session.session_id) updateCurrentSession(data.session);
    renderSessionList();
  } catch (error) {
    addMessage("assistant", error.message || String(error), "error");
  }
}

async function endCurrentSession() {
  if (!currentSession || currentSession.status !== "active" || busy) return;
  setBusy(true);
  try {
    const data = await apiRequest(
      `/api/sessions/${encodeURIComponent(currentSession.session_id)}/close`,
      { method: "POST", body: JSON.stringify({ reason: "user_close" }) },
    );
    updateCurrentSession(data.session);
    await refreshSessions();
  } catch (error) {
    addMessage("assistant", error.message || String(error), "error");
  } finally {
    setBusy(false);
  }
}

async function deleteSession(session) {
  if (!window.confirm(`确定删除“${session.title || "新对话"}”及其历史记录吗？`)) return;
  if (busy) return;
  setBusy(true);
  try {
    await apiRequest(`/api/sessions/${encodeURIComponent(session.session_id)}`, {
      method: "DELETE",
    });
    sessions = sessions.filter((item) => item.session_id !== session.session_id);
    if (currentSession?.session_id === session.session_id) {
      updateCurrentSession(null);
      clearMessages();
      await createSession();
    } else {
      renderSessionList();
    }
  } catch (error) {
    addMessage("assistant", error.message || String(error), "error");
  } finally {
    setBusy(false);
  }
}

function parseSseEvent(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
    } else if (line === "data" || line.startsWith("data:")) {
      dataLines.push(line === "data" ? "" : line.slice(5).trimStart());
    }
  }
  return { event, payload: dataLines.join("\n") };
}

function parseJson(payload) {
  try {
    return JSON.parse(payload);
  } catch {
    return null;
  }
}

function legacyDelta(data) {
  const choice = data?.choices?.[0];
  if (typeof choice?.delta?.content === "string") return choice.delta.content;
  if (typeof choice?.message?.content === "string") return choice.message.content;
  return "";
}

function legacyReasoningDelta(data) {
  const choice = data?.choices?.[0];
  const delta = choice?.delta || choice?.message || {};
  if (typeof delta.reasoning_content === "string") return delta.reasoning_content;
  if (typeof delta.reasoning === "string") return delta.reasoning;
  return "";
}

function reasoningFromMessages(messages) {
  const parts = [];
  for (const message of messages || []) {
    if (message?.role !== "assistant") continue;
    const reasoning = message.reasoning_content || message.reasoning;
    if (typeof reasoning === "string" && reasoning.trim()) parts.push(reasoning);
  }
  return parts.join("\n\n");
}

function toolProgressText(event, data) {
  const tool = data?.tool_name || data?.label || data?.tool || "数据库工具";
  if (event === "tool.completed" || data?.status === "completed") {
    return `${tool} 执行完成，正在整理结果…`;
  }
  if (event === "tool.failed" || data?.status === "failed" || data?.status === "error") {
    return `${tool} 执行失败`;
  }
  return `${tool} 执行中…`;
}

async function sendNonStreaming(message) {
  const data = await apiRequest(
    `/api/sessions/${encodeURIComponent(currentSession.session_id)}/chat`,
    {
      method: "POST",
      body: JSON.stringify({ message, stream: false }),
    },
  );
  if (data.session_id && data.session_id !== currentSession.session_id) {
    currentSession.session_id = data.session_id;
    localStorage.setItem(CURRENT_SESSION_STORAGE, data.session_id);
  }
  return { content: data.content || "", reasoning: data.reasoning || "" };
}

async function sendStreaming(message, assistantView) {
  const response = await fetch(
    `/api/sessions/${encodeURIComponent(currentSession.session_id)}/chat/stream`,
    {
      method: "POST",
      headers: sessionHeaders(true),
      body: JSON.stringify({ message, stream: true }),
    },
  );
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
  let reasoning = "";

  function handleEvent(block) {
    if (!block.trim()) return;
    const { event, payload } = parseSseEvent(block);
    if (!payload) return;
    const data = parseJson(payload);

    if (event === "error") throw new Error(data?.message || payload);
    if (data?.session_id && data.session_id !== currentSession.session_id) {
      currentSession.session_id = data.session_id;
      localStorage.setItem(CURRENT_SESSION_STORAGE, data.session_id);
    }
    if (event === "assistant.delta" && typeof data?.delta === "string") {
      content += data.delta;
      assistantView.contentEl.textContent = content;
    } else if (event === "assistant.completed" && typeof data?.content === "string") {
      content = data.content;
      assistantView.contentEl.textContent = content;
    } else if (
      (event === "tool.progress" || event === "hermes.tool.progress") &&
      data?.tool_name === "_thinking" &&
      typeof data?.delta === "string" &&
      data.delta.trim() &&
      !content
    ) {
      reasoning += data.delta;
      setAssistantReasoning(assistantView, reasoning, { live: true });
    } else if (event === "run.completed") {
      const completedReasoning = reasoningFromMessages(data?.messages);
      if (completedReasoning) {
        reasoning = completedReasoning;
        setAssistantReasoning(assistantView, reasoning);
      }
    } else if (
      event === "tool.started" ||
      event === "tool.completed" ||
      event === "tool.failed" ||
      event === "tool.progress" ||
      event === "hermes.tool.progress"
    ) {
      if (!content) assistantView.contentEl.textContent = toolProgressText(event, data);
    } else {
      const reasoningPiece = legacyReasoningDelta(data);
      if (reasoningPiece) {
        reasoning += reasoningPiece;
        setAssistantReasoning(assistantView, reasoning, { live: true });
      }
      const piece = legacyDelta(data);
      if (piece) {
        content += piece;
        assistantView.contentEl.textContent = content;
      }
    }
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function consumeEvents(flush = false) {
    while (true) {
      const boundary = buffer.match(/\r?\n\r?\n/);
      if (!boundary || boundary.index === undefined) break;
      const block = buffer.slice(0, boundary.index);
      buffer = buffer.slice(boundary.index + boundary[0].length);
      handleEvent(block);
    }
    if (flush && buffer.trim()) {
      handleEvent(buffer);
      buffer = "";
    }
  }

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    consumeEvents();
  }
  buffer += decoder.decode();
  consumeEvents(true);
  return { content, reasoning };
}

function extractError(data) {
  const detail = data?.detail;
  if (typeof detail === "string") return detail;
  if (typeof detail?.message === "string") {
    if (typeof detail.body === "string") return `${detail.message} ${detail.body}`;
    if (detail.body?.error?.message) return `${detail.message} ${detail.body.error.message}`;
    return detail.error ? `${detail.message} ${detail.error}` : detail.message;
  }
  return "请求失败";
}

async function handleSubmit(event) {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || busy || currentSession?.status !== "active") return;

  input.value = "";
  addMessage("user", message);
  const assistantView = addAssistantMessage();
  setBusy(true);

  try {
    const result = streamToggle.checked
      ? await sendStreaming(message, assistantView)
      : await sendNonStreaming(message);
    assistantView.contentEl.textContent =
      result.content || assistantView.contentEl.textContent || "已完成";
    setAssistantReasoning(assistantView, result.reasoning);
    await refreshSessions();
  } catch (error) {
    assistantView.messageEl.remove();
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
    if (data.ok) {
      setStatus("Connected · Memory on", "ready");
    } else if (data.hermes?.ok && !data.memory?.ok) {
      setStatus("Memory incomplete", "error");
    } else if (data.hermes_api_key_configured) {
      setStatus("Hermes offline", "error");
    } else {
      setStatus("API key missing", "error");
    }
  } catch {
    setStatus("Wrapper offline", "error");
  }
}

async function initialize() {
  setBusy(true);
  await checkHealth();
  try {
    await refreshSessions();
    const savedId = localStorage.getItem(CURRENT_SESSION_STORAGE);
    const saved = sessions.find((item) => item.session_id === savedId);
    const active = saved?.status === "active" ? saved : sessions.find((item) => item.status === "active");
    if (active) {
      await openSession(active.session_id);
    } else {
      await createSession();
    }
  } catch (error) {
    clearMessages();
    addMessage("assistant", error.message || String(error), "error");
  } finally {
    setBusy(false);
    input.focus();
  }
}

form.addEventListener("submit", handleSubmit);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});
newChatButton.addEventListener("click", () => createSession({ closeCurrent: true }));
renameChatButton.addEventListener("click", () => currentSession && renameSession(currentSession));
endChatButton.addEventListener("click", endCurrentSession);
memoryViewButton.addEventListener("click", showMemoryView);
refreshMemoryButton.addEventListener("click", loadMemoryFiles);
backToChatButton.addEventListener("click", showChatView);

exampleButtons.forEach((button) => {
  button.addEventListener("click", () => {
    input.value = button.dataset.example || "";
    input.focus();
  });
});

initialize();
