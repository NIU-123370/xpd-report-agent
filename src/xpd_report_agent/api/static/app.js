const form = document.querySelector("#chatForm");
const input = document.querySelector("#messageInput");
const sendButton = document.querySelector("#sendButton");
const messagesEl = document.querySelector("#messages");
const statusText = document.querySelector("#statusText");
const exampleButtons = document.querySelectorAll("[data-example]");
const sessionListEl = document.querySelector("#sessionList");
const currentSessionTitle = document.querySelector("#currentSessionTitle");
const currentSessionEyebrow = document.querySelector("#currentSessionEyebrow");
const newChatButton = document.querySelector("#newChatButton");
const renameChatButton = document.querySelector("#renameChatButton");
const endChatButton = document.querySelector("#endChatButton");
const chatPanel = document.querySelector("#chatPanel");
const memoryPanel = document.querySelector("#memoryPanel");
const memoryViewButton = document.querySelector("#memoryViewButton");
const refreshMemoryButton = document.querySelector("#refreshMemoryButton");
const backToChatButton = document.querySelector("#backToChatButton");
const memoryFilesEl = document.querySelector("#memoryFiles");
const schedulePanel = document.querySelector("#schedulePanel");
const scheduleViewButton = document.querySelector("#scheduleViewButton");
const refreshScheduleButton = document.querySelector("#refreshScheduleButton");
const newScheduleButton = document.querySelector("#newScheduleButton");
const backFromScheduleButton = document.querySelector("#backFromScheduleButton");
const scheduleListEl = document.querySelector("#scheduleList");
const scheduleNotice = document.querySelector("#scheduleNotice");
const scheduleForm = document.querySelector("#scheduleForm");
const scheduleFormTitle = document.querySelector("#scheduleFormTitle");
const scheduleFormError = document.querySelector("#scheduleFormError");
const scheduleReportType = document.querySelector("#scheduleReportType");
const scheduleFrequency = document.querySelector("#scheduleFrequency");
const scheduleDateField = document.querySelector("#scheduleDateField");
const scheduleRunDate = document.querySelector("#scheduleRunDate");
const scheduleWeekdayField = document.querySelector("#scheduleWeekdayField");
const scheduleWeekday = document.querySelector("#scheduleWeekday");
const scheduleTime = document.querySelector("#scheduleTime");
const scheduleFormat = document.querySelector("#scheduleFormat");
const cancelScheduleButton = document.querySelector("#cancelScheduleButton");
const saveScheduleButton = document.querySelector("#saveScheduleButton");
const analysisPanel = document.querySelector("#analysisPanel");
const analysisEntryButtons = document.querySelectorAll("[data-analysis-preset]");
const backFromAnalysisButton = document.querySelector("#backFromAnalysisButton");
const analysisEyebrow = document.querySelector("#analysisEyebrow");
const analysisTitle = document.querySelector("#analysisTitle");
const analysisHeading = document.querySelector("#analysisHeading");
const analysisDescription = document.querySelector("#analysisDescription");
const analysisStatus = document.querySelector("#analysisStatus");
const analysisCapabilityNote = document.querySelector("#analysisCapabilityNote");
const analysisForm = document.querySelector("#analysisForm");
const analysisPeriod = document.querySelector("#analysisPeriod");
const analysisFocusField = document.querySelector("#analysisFocusField");
const analysisFocusLabel = document.querySelector("#analysisFocusLabel");
const analysisFocus = document.querySelector("#analysisFocus");
const analysisTopField = document.querySelector("#analysisTopField");
const analysisTopN = document.querySelector("#analysisTopN");
const analysisNote = document.querySelector("#analysisNote");
const analysisFormError = document.querySelector("#analysisFormError");
const startAnalysisButton = document.querySelector("#startAnalysisButton");

const SESSION_KEY_STORAGE = "xpd-report-agent.session-key.v1";
const CURRENT_SESSION_STORAGE = "xpd-report-agent.current-session.v1";
const LOCAL_USER_ID_STORAGE = "xpd-report-agent.local-user-id.v1";

let sessions = [];
let currentSession = null;
let busy = false;
let memoryBusy = false;
let historyLoadVersion = 0;
let schedules = [];
let schedulesBusy = false;
let editingScheduleId = null;
let scheduleCapabilities = {};
let analysisPresets = new Map();
let selectedAnalysisPreset = "refund_diagnosis";
let analysisBusy = false;
const knownArtifactIdsBySession = new Map();

const ANALYSIS_PRESET_DEFINITIONS = {
  refund_diagnosis: {
    title: "退款诊断",
    eyebrow: "风险商品与场次",
    description:
      "检查退款金额、退款率及趋势，定位异常商品和直播场次。当前数据不含退款原因，因此不会虚构原因结论。",
    focusLabel: "诊断维度",
    options: [
      ["overview", "综合诊断"],
      ["items", "商品风险"],
      ["sessions", "直播场次风险"],
    ],
    defaultDays: "30",
    showTopN: true,
    notePlaceholder: "例如：重点关注退款率突然上升的商品",
    fallbackAvailable: false,
    fallbackReason: "正在检查数据库字段，请稍后重试。",
  },
  product_ranking: {
    title: "商品排行",
    eyebrow: "直播商品表现",
    description:
      "按实际数据对商品排名，对比成交、订单、流量或退款表现，识别头部商品和异常项。",
    focusLabel: "排行指标",
    options: [
      ["pay_amt", "成交金额"],
      ["pay_ord_cnt", "支付订单数"],
      ["pay_itm_qty", "支付商品件数"],
      ["refund_amt", "退款金额"],
      ["refund_rate", "退款率"],
    ],
    defaultDays: "30",
    showTopN: true,
    notePlaceholder: "例如：同时标出成交高但退款率也高的商品",
    fallbackAvailable: false,
    fallbackReason: "正在检查数据库字段，请稍后重试。",
  },
  repurchase_analysis: {
    title: "复购分析",
    eyebrow: "客户复购与留存",
    description:
      "按买家识别计算复购人数、复购率和复购周期。该分析必须有买家与订单级明细，不能使用已聚合的买家数推算。",
    focusLabel: "分析方向",
    options: [["overall", "整体复购"], ["product", "商品复购"]],
    defaultDays: "90",
    notePlaceholder: "例如：对比首购与复购用户的成交差异",
    fallbackAvailable: false,
    fallbackReason:
      "当前三张报表缺少 buyer_id/customer_id 和 order_id，无法识别同一买家的多次下单。补充买家级订单明细后才能开启。",
  },
};

const REPORT_TYPE_LABELS = {
  daily_operations: "经营日报",
  weekly_brand: "品牌表现报告",
};
const FREQUENCY_LABELS = {
  once: "仅执行一次",
  daily: "每天",
  weekly: "每周",
};
const WEEKDAY_LABELS = {
  1: "周一",
  2: "周二",
  3: "周三",
  4: "周四",
  5: "周五",
  6: "周六",
  7: "周日",
};
const FORMAT_LABELS = {
  xlsx: "XLSX",
  csv: "CSV",
  markdown: "Markdown",
  pdf: "PDF",
  json: "JSON",
};

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

function ensureLocalUserId() {
  let userId = localStorage.getItem(LOCAL_USER_ID_STORAGE);
  if (userId && userId.length <= 128 && !/\s/.test(userId)) return userId;

  if (crypto.randomUUID) {
    userId = `local-user-${crypto.randomUUID()}`;
  } else {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    const suffix = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    userId = `local-user-${suffix}`;
  }
  localStorage.setItem(LOCAL_USER_ID_STORAGE, userId);
  return userId;
}

const localUserId = ensureLocalUserId();

function sessionHeaders(json = true) {
  const headers = {
    "X-XPD-Session-Key": clientSessionKey,
    "X-User-Id": localUserId,
  };
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

function renderEmpty(text = "选择一个经营分析，或直接输入问题") {
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

function renderAssistantContent(contentEl, content, { final = false } = {}) {
  const text = sanitizeInternalTermsForDisplay(content);
  contentEl.textContent = "";
  if (!final) {
    contentEl.textContent = text;
    return;
  }

  const auditHeading = /(?:^|\n)(?:#{1,6}\s*)?查询明细(?:（[^）]*）|\([^)]*\))?\s*[:：]?\s*\n/i;
  const match = auditHeading.exec(text);
  if (!match || match.index === undefined) {
    contentEl.textContent = text;
    return;
  }

  const visibleText = text.slice(0, match.index).trimEnd();
  const auditText = text.slice(match.index + match[0].length).trim();
  if (visibleText) contentEl.append(document.createTextNode(visibleText));
  if (!auditText) return;

  const details = document.createElement("details");
  details.className = "query-audit";
  const summary = document.createElement("summary");
  summary.textContent = "查询明细（技术审计）";
  const pre = document.createElement("pre");
  pre.textContent = auditText;
  details.append(summary, pre);
  contentEl.append(details);
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
  reasoningLabel.textContent = "分析进度";
  const reasoningCount = document.createElement("span");
  reasoningCount.className = "reasoning-count";
  const reasoningEl = document.createElement("pre");
  reasoningEl.className = "reasoning-content";
  reasoningSummary.append(reasoningLabel, reasoningCount);
  reasoningDetails.append(reasoningSummary, reasoningEl);

  const contentEl = document.createElement("div");
  contentEl.className = "answer-content";
  renderAssistantContent(contentEl, content, { final: true });

  const clarificationsEl = document.createElement("div");
  clarificationsEl.className = "clarifications";
  clarificationsEl.hidden = true;

  const artifactsEl = document.createElement("div");
  artifactsEl.className = "artifacts";
  artifactsEl.hidden = true;
  artifactsEl.setAttribute("aria-label", "导出文件");

  messageEl.append(reasoningDetails, clarificationsEl, contentEl, artifactsEl);
  messagesEl.append(messageEl);

  const view = {
    messageEl,
    contentEl,
    reasoningDetails,
    reasoningEl,
    reasoningLabel,
    reasoningCount,
    reasoningProgress: [],
    clarificationsEl,
    clarificationCards: new Map(),
    artifactsEl,
    artifactCards: new Map(),
  };
  setAssistantReasoning(view, reasoning);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return view;
}

function normalizeArtifact(value) {
  if (!value || typeof value !== "object") return null;
  const artifactId = String(value.artifact_id || "").trim();
  const downloadUrl = String(value.download_url || "").trim();
  if (!artifactId || !downloadUrl) return null;

  const format = String(value.format || "file").trim().toLowerCase() || "file";
  return {
    artifactId,
    sessionId: String(value.session_id || "").trim(),
    filename: String(value.filename || `经营报告.${format}`).trim() || `经营报告.${format}`,
    format,
    mediaType: String(value.media_type || "application/octet-stream").trim(),
    sizeBytes: value.size_bytes,
    createdAt: value.created_at,
    downloadUrl,
  };
}

function knownArtifactIds(sessionId) {
  if (!knownArtifactIdsBySession.has(sessionId)) {
    knownArtifactIdsBySession.set(sessionId, new Set());
  }
  return knownArtifactIdsBySession.get(sessionId);
}

function artifactsFromResponse(data) {
  if (Array.isArray(data)) return data;
  return data?.data || data?.artifacts || [];
}

async function fetchSessionArtifacts(sessionId) {
  const data = await apiRequest(
    `/api/sessions/${encodeURIComponent(sessionId)}/artifacts`,
  );
  const artifacts = artifactsFromResponse(data);
  return Array.isArray(artifacts) ? artifacts : [];
}

async function downloadArtifact(event, artifact, artifactLink) {
  const downloadUrl = new URL(artifact.downloadUrl, window.location.origin);
  if (downloadUrl.origin !== window.location.origin) return;
  event.preventDefault();
  if (artifactLink.getAttribute("aria-busy") === "true") return;
  artifactLink.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(downloadUrl.href, {
      headers: { ...sessionHeaders(false), Accept: "application/json" },
      redirect: "error",
    });
    if (!response.ok) {
      let data = {};
      try {
        data = await response.json();
      } catch {
        data = { detail: response.statusText };
      }
      throw new Error(extractError(data));
    }

    const contentType = String(response.headers.get("content-type") || "").toLowerCase();
    if (contentType.includes("application/json")) {
      const data = await response.json();
      const freshUrl = new URL(String(data?.download_url || ""));
      if (!['http:', 'https:'].includes(freshUrl.protocol)) {
        throw new Error("服务端未返回有效的文件地址");
      }
      const downloadLink = document.createElement("a");
      downloadLink.href = freshUrl.href;
      downloadLink.download = artifact.filename;
      downloadLink.target = "_blank";
      downloadLink.rel = "noopener noreferrer";
      downloadLink.hidden = true;
      document.body.append(downloadLink);
      downloadLink.click();
      downloadLink.remove();
      return;
    }

    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const downloadLink = document.createElement("a");
    downloadLink.href = objectUrl;
    downloadLink.download = artifact.filename;
    downloadLink.hidden = true;
    document.body.append(downloadLink);
    downloadLink.click();
    downloadLink.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30_000);
  } catch (error) {
    window.alert(`下载失败：${error.message || String(error)}`);
  } finally {
    artifactLink.removeAttribute("aria-busy");
  }
}

function renderArtifactCard(view, value, expectedSessionId = "") {
  const artifact = normalizeArtifact(value);
  if (!artifact || view.artifactCards.has(artifact.artifactId)) return;
  if (expectedSessionId && artifact.sessionId && artifact.sessionId !== expectedSessionId) return;

  let downloadUrl;
  try {
    downloadUrl = new URL(artifact.downloadUrl, window.location.origin);
  } catch {
    return;
  }
  if (!['http:', 'https:'].includes(downloadUrl.protocol)) return;

  const link = document.createElement("a");
  link.className = "artifact-link";
  link.href = downloadUrl.href;
  link.textContent = artifact.filename;
  link.setAttribute("aria-label", `下载 ${artifact.filename}`);
  if (downloadUrl.origin !== window.location.origin) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  } else {
    link.addEventListener("click", (event) => downloadArtifact(event, artifact, link));
  }

  view.artifactsEl.append(link);
  view.artifactsEl.hidden = false;
  view.artifactCards.set(artifact.artifactId, link);
  knownArtifactIds(artifact.sessionId || expectedSessionId).add(artifact.artifactId);
}

function renderSessionArtifacts(artifacts, sessionId) {
  if (!Array.isArray(artifacts) || artifacts.length === 0) return;
  const view = addAssistantMessage();
  for (const artifact of artifacts) renderArtifactCard(view, artifact, sessionId);
  if (view.artifactCards.size === 0) view.messageEl.remove();
}

async function syncNewSessionArtifacts(view, sessionId) {
  const artifacts = await fetchSessionArtifacts(sessionId);
  const known = knownArtifactIds(sessionId);
  for (const value of artifacts) {
    const artifact = normalizeArtifact(value);
    if (!artifact || known.has(artifact.artifactId)) continue;
    renderArtifactCard(view, value, sessionId);
  }
}

function normalizeClarificationChoices(choices) {
  if (!Array.isArray(choices)) return [];
  return choices
    .slice(0, 4)
    .map((choice) => {
      if (typeof choice === "string" || typeof choice === "number") {
        const text = String(choice).trim();
        return text ? { label: text, answer: text } : null;
      }
      if (!choice || typeof choice !== "object") return null;
      const label = String(
        choice.label ?? choice.text ?? choice.title ?? choice.value ?? choice.answer ?? "",
      ).trim();
      const answer = String(choice.value ?? choice.answer ?? choice.id ?? label).trim();
      return label && answer ? { label, answer } : null;
    })
    .filter(Boolean);
}

function clarificationTimeoutText(args) {
  const seconds = Number(args?.timeout_seconds);
  if (Number.isFinite(seconds) && seconds > 0) {
    return `请在 ${Math.ceil(seconds)} 秒内回答，分析会在收到答案后继续。`;
  }
  if (args?.expires_at) {
    const raw = args.expires_at;
    const date = typeof raw === "number"
      ? new Date(raw < 1e12 ? raw * 1000 : raw)
      : new Date(raw);
    if (!Number.isNaN(date.getTime())) {
      return `请在 ${date.toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })} 前回答。`;
    }
  }
  return "分析会在收到答案后继续。";
}

function disableClarificationControls(state, disabled = true) {
  state.card.querySelectorAll("button, input").forEach((control) => {
    control.disabled = disabled;
  });
}

function setClarificationResult(state, status) {
  if (status !== "answered" && status !== "expired") return;
  state.status = status;
  state.card.classList.remove("submitting", "submission-error", "submitted");
  state.card.classList.add(status);
  disableClarificationControls(state);
  state.statusEl.className = "clarification-status";
  state.statusEl.textContent = status === "answered"
    ? "已回答，正在继续分析…"
    : "该问题已过期，请重新发起问题。";
}

function updateClarificationCard(view, args) {
  const clarificationId = String(args?.clarification_id ?? "").trim();
  let state = clarificationId ? view.clarificationCards.get(clarificationId) : null;
  if (!state) {
    const states = Array.from(view.clarificationCards.values());
    for (let index = states.length - 1; index >= 0; index -= 1) {
      const candidate = states[index];
      if (candidate.status !== "answered" && candidate.status !== "expired") {
        state = candidate;
        break;
      }
    }
  }
  if (state) setClarificationResult(state, args?.status);
}

function renderClarificationCard(view, args, requestSessionId) {
  const clarificationId = String(args?.clarification_id ?? "").trim();
  if (!clarificationId || view.clarificationCards.has(clarificationId)) return;

  const card = document.createElement("section");
  card.className = "clarification-card";
  card.setAttribute("aria-label", "需要确认的问题");

  const eyebrow = document.createElement("p");
  eyebrow.className = "clarification-eyebrow";
  eyebrow.textContent = "需要你确认";

  const question = document.createElement("p");
  question.className = "clarification-question";
  question.textContent = String(args?.question || "请补充这个问题所需的信息。");

  const hint = document.createElement("p");
  hint.className = "clarification-hint";
  hint.textContent = clarificationTimeoutText(args);

  const choicesEl = document.createElement("div");
  choicesEl.className = "clarification-choices";

  const otherRow = document.createElement("div");
  otherRow.className = "clarification-other";
  const otherInput = document.createElement("input");
  otherInput.type = "text";
  otherInput.className = "clarification-other-input";
  otherInput.placeholder = "其他答案";
  otherInput.maxLength = 2000;
  otherInput.setAttribute("aria-label", "输入其他答案");
  const otherButton = document.createElement("button");
  otherButton.type = "button";
  otherButton.className = "clarification-submit";
  otherButton.textContent = "提交";
  otherRow.append(otherInput, otherButton);

  const statusEl = document.createElement("p");
  statusEl.className = "clarification-status";
  statusEl.setAttribute("aria-live", "polite");

  card.append(eyebrow, question, hint, choicesEl, otherRow, statusEl);
  view.clarificationsEl.append(card);
  view.clarificationsEl.hidden = false;

  const state = { card, statusEl, status: "pending" };
  view.clarificationCards.set(clarificationId, state);

  async function submitAnswer(rawAnswer) {
    const answer = String(rawAnswer ?? "").trim();
    if (!answer) {
      statusEl.className = "clarification-status error";
      statusEl.textContent = "请输入答案后再提交。";
      otherInput.focus();
      return;
    }
    if (state.status !== "pending") return;

    state.status = "submitting";
    card.classList.remove("submission-error");
    card.classList.add("submitting");
    disableClarificationControls(state);
    statusEl.className = "clarification-status";
    statusEl.textContent = "正在提交…";

    try {
      await apiRequest(
        `/api/sessions/${encodeURIComponent(requestSessionId)}/clarifications/${encodeURIComponent(clarificationId)}/answer`,
        {
          method: "POST",
          body: JSON.stringify({ answer }),
        },
      );
      if (state.status === "answered" || state.status === "expired") return;
      state.status = "submitted";
      card.classList.remove("submitting");
      card.classList.add("submitted");
      statusEl.className = "clarification-status";
      statusEl.textContent = "已提交，正在继续分析…";
    } catch (error) {
      if (state.status === "answered" || state.status === "expired") return;
      state.status = "pending";
      card.classList.remove("submitting");
      card.classList.add("submission-error");
      disableClarificationControls(state, false);
      statusEl.className = "clarification-status error";
      statusEl.textContent = `提交失败：${error.message || String(error)}，请重试。`;
    }
  }

  for (const choice of normalizeClarificationChoices(args?.choices)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "clarification-choice";
    button.textContent = choice.label;
    button.addEventListener("click", () => submitAnswer(choice.answer));
    choicesEl.append(button);
  }

  otherButton.addEventListener("click", () => submitAnswer(otherInput.value));
  otherInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    submitAnswer(otherInput.value);
  });

  if (!view.contentEl.textContent) view.contentEl.textContent = "等待你的确认…";
}

function renderAssistantReasoning(view, text, { live = false } = {}) {
  if (!text.trim()) return;
  const isFirstVisibleReasoning = view.reasoningDetails.hidden;
  view.reasoningEl.textContent = text;
  view.reasoningLabel.textContent = live ? "分析中" : "分析进度";
  view.reasoningCount.textContent = `${text.length.toLocaleString("zh-CN")} 字`;
  view.reasoningDetails.hidden = false;
  if (live && isFirstVisibleReasoning) view.reasoningDetails.open = true;
  if (live && view.reasoningDetails.open) {
    view.reasoningEl.scrollTop = view.reasoningEl.scrollHeight;
  }
}

function setAssistantReasoning(view, reasoning, { live = false } = {}) {
  const hasReasoning = typeof reasoning === "string" && reasoning.trim();
  if (!hasReasoning && view.reasoningProgress.length === 0) return;
  const text = view.reasoningProgress.length
    ? view.reasoningProgress.join("\n")
    : live
      ? "正在理解问题并规划分析步骤…"
      : "本轮分析与结果整理已完成。";
  renderAssistantReasoning(view, text, { live });
}

function appendAssistantProgress(view, text, { live = true } = {}) {
  const safeText = sanitizeInternalTermsForDisplay(text).trim();
  if (!safeText || view.reasoningProgress.includes(safeText)) return;
  view.reasoningProgress.push(safeText);
  renderAssistantReasoning(view, view.reasoningProgress.join("\n"), { live });
}

function setBusy(isBusy) {
  busy = isBusy;
  const sessionReadOnly = currentSession?.read_only === true;
  const readOnly = !currentSession || currentSession.status !== "active" || sessionReadOnly;
  sendButton.disabled = isBusy || readOnly;
  input.disabled = isBusy || readOnly;
  newChatButton.disabled = isBusy;
  renameChatButton.disabled = isBusy || !currentSession || sessionReadOnly;
  endChatButton.disabled = isBusy || readOnly;
  form.classList.toggle("readonly", readOnly);
  updateAnalysisFormAvailability();
  input.placeholder = sessionReadOnly
    ? "自动报告为只读，只能查看结果"
    : readOnly
      ? "该对话已结束，只能查看历史"
      : "输入想分析的数据问题…";
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
    state.textContent = item.read_only
      ? "商家共享 · 只读"
      : item.at_watermark
        ? "达到整理水位"
        : "个人记忆 · 正常";
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

function setScheduleNotice(message = "", isError = false) {
  scheduleNotice.textContent = message;
  scheduleNotice.className = `schedule-notice${isError ? " error" : ""}`;
}

function setSchedulesBusy(isBusy) {
  schedulesBusy = isBusy;
  refreshScheduleButton.disabled = isBusy;
  newScheduleButton.disabled = isBusy;
  scheduleListEl.querySelectorAll("button").forEach((button) => {
    button.disabled = isBusy;
  });
  Array.from(scheduleForm.elements).forEach((control) => {
    control.disabled = isBusy;
  });
  if (!isBusy) applyScheduleCapabilities();
}

function beijingDateValue(daysFromNow = 0) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(Date.now() + daysFromNow * 86400000));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function updateScheduleFrequencyFields() {
  const frequency = scheduleFrequency.value;
  const showDate = frequency === "once";
  const showWeekday = frequency === "weekly";
  scheduleDateField.hidden = !showDate;
  scheduleWeekdayField.hidden = !showWeekday;
  scheduleRunDate.required = showDate;
  scheduleWeekday.required = showWeekday;
}

function scheduleIdentifier(schedule) {
  return String(schedule?.schedule_id ?? "").trim();
}

function scheduleReportLabel(schedule) {
  const serverLabel = String(schedule?.report_label || "").trim();
  return serverLabel || REPORT_TYPE_LABELS[schedule?.report_type] || "定时报告";
}

function applyScheduleCapabilities() {
  const brandOption = scheduleReportType.querySelector('option[value="weekly_brand"]');
  if (!brandOption) return;
  const brandCapability = scheduleCapabilities?.weekly_brand;
  const ready = brandCapability?.ready !== false;
  brandOption.disabled = !ready;
  brandOption.textContent = ready
    ? REPORT_TYPE_LABELS.weekly_brand
    : `${REPORT_TYPE_LABELS.weekly_brand}（缺少品牌维度）`;
}

function formatScheduleTimestamp(value) {
  if (value === null || value === undefined || value === "") return "";
  const numeric = Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(numeric < 1e12 ? numeric * 1000 : numeric)
    : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function scheduleRuleText(schedule) {
  const frequency = String(schedule?.frequency || "");
  const time = String(schedule?.time || "--:--");
  if (frequency === "once") {
    return `${schedule?.run_date || "未设置日期"} ${time} · 北京时间`;
  }
  if (frequency === "weekly") {
    const weekday = WEEKDAY_LABELS[Number(schedule?.weekday)] || "未设置星期";
    return `每${weekday} ${time} · 北京时间`;
  }
  if (frequency === "daily") return `每天 ${time} · 北京时间`;
  return `${FREQUENCY_LABELS[frequency] || "未设置频率"} · 北京时间`;
}

function scheduleStateView(schedule) {
  const state = String(schedule?.state || "").toLowerCase();
  const lastStatus = String(schedule?.last_status || "").toLowerCase();
  if (state === "running") return { label: "生成中", className: "running" };
  if (["failed", "error"].includes(lastStatus) || state === "failed") {
    return { label: "最近失败", className: "failed" };
  }
  if (schedule?.enabled === false || state === "paused") {
    return { label: "已暂停", className: "paused" };
  }
  if (["completed", "finished"].includes(state)) {
    return { label: "已完成", className: "completed" };
  }
  return { label: "已启用", className: "enabled" };
}

function makeScheduleAction(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.disabled = schedulesBusy;
  button.addEventListener("click", handler);
  return button;
}

function renderScheduleList() {
  scheduleListEl.replaceChildren();
  if (!schedules.length) {
    const empty = document.createElement("p");
    empty.className = "schedule-empty";
    empty.textContent = "暂无定时报告，点击“新增计划”开始设置。";
    scheduleListEl.append(empty);
    return;
  }

  for (const schedule of schedules) {
    const scheduleId = scheduleIdentifier(schedule);
    if (!scheduleId) continue;
    const state = scheduleStateView(schedule);
    const card = document.createElement("article");
    card.className = `schedule-card ${state.className}`;

    const heading = document.createElement("div");
    heading.className = "schedule-card-heading";
    const titleGroup = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = scheduleReportLabel(schedule);
    const rule = document.createElement("p");
    rule.className = "schedule-rule";
    rule.textContent = scheduleRuleText(schedule);
    titleGroup.append(title, rule);

    const badges = document.createElement("div");
    badges.className = "schedule-badges";
    const formatBadge = document.createElement("span");
    formatBadge.className = "schedule-format";
    formatBadge.textContent = FORMAT_LABELS[schedule.format] || String(schedule.format || "文件");
    const stateBadge = document.createElement("span");
    stateBadge.className = `schedule-state ${state.className}`;
    stateBadge.textContent = state.label;
    badges.append(formatBadge, stateBadge);
    heading.append(titleGroup, badges);

    const timing = document.createElement("div");
    timing.className = "schedule-timing";
    const nextRun = formatScheduleTimestamp(schedule.next_run_at);
    const lastRun = formatScheduleTimestamp(schedule.last_run_at);
    if (nextRun) {
      const next = document.createElement("span");
      next.textContent = `下次执行：${nextRun}`;
      timing.append(next);
    }
    if (lastRun) {
      const last = document.createElement("span");
      last.textContent = `最近执行：${lastRun}`;
      timing.append(last);
    }

    const lastStatus = String(schedule.last_status || "").toLowerCase();
    if (schedule.last_error && ["failed", "error"].includes(lastStatus)) {
      const error = document.createElement("p");
      error.className = "schedule-error";
      error.textContent = `最近失败：${String(schedule.last_error).slice(0, 500)}`;
      card.append(heading, timing, error);
    } else {
      card.append(heading, timing);
    }

    const actions = document.createElement("div");
    actions.className = "schedule-card-actions";
    actions.append(
      makeScheduleAction("编辑", "", () => openScheduleEditor(schedule)),
      makeScheduleAction(
        schedule.enabled === false ? "恢复" : "暂停",
        "",
        () => mutateSchedule(schedule, schedule.enabled === false ? "resume" : "pause"),
      ),
      makeScheduleAction("立即生成", "primary", () => runScheduleNow(schedule)),
      makeScheduleAction("删除", "danger", () => removeSchedule(schedule)),
    );
    card.append(actions);
    scheduleListEl.append(card);
  }
}

async function loadSchedules({ preserveNotice = false } = {}) {
  if (schedulesBusy) return;
  setSchedulesBusy(true);
  if (!preserveNotice) setScheduleNotice();
  scheduleListEl.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "schedule-empty";
  loading.textContent = "正在加载定时报告…";
  scheduleListEl.append(loading);
  try {
    const data = await apiRequest("/api/schedules");
    schedules = Array.isArray(data.data) ? data.data : [];
    scheduleCapabilities = data.capabilities || {};
    applyScheduleCapabilities();
    renderScheduleList();
  } catch (error) {
    schedules = [];
    loading.className = "schedule-empty error";
    loading.textContent = `加载失败：${error.message || String(error)}`;
  } finally {
    setSchedulesBusy(false);
  }
}

function closeScheduleEditor() {
  editingScheduleId = null;
  scheduleForm.hidden = true;
  scheduleFormError.textContent = "";
  scheduleForm.reset();
}

function openScheduleEditor(schedule = null) {
  editingScheduleId = schedule ? scheduleIdentifier(schedule) : null;
  scheduleFormTitle.textContent = editingScheduleId ? "编辑定时报告" : "新增定时报告";
  scheduleReportType.value = REPORT_TYPE_LABELS[schedule?.report_type]
    ? schedule.report_type
    : "daily_operations";
  applyScheduleCapabilities();
  scheduleFrequency.value = FREQUENCY_LABELS[schedule?.frequency]
    ? schedule.frequency
    : "daily";
  scheduleRunDate.value = String(schedule?.run_date || beijingDateValue(1));
  scheduleWeekday.value = String(WEEKDAY_LABELS[Number(schedule?.weekday)] ? schedule.weekday : 1);
  scheduleTime.value = /^\d{2}:\d{2}$/.test(String(schedule?.time || ""))
    ? schedule.time
    : "08:00";
  scheduleFormat.value = FORMAT_LABELS[schedule?.format] ? schedule.format : "xlsx";
  scheduleFormError.textContent = "";
  updateScheduleFrequencyFields();
  scheduleForm.hidden = false;
  scheduleForm.scrollIntoView({ behavior: "smooth", block: "start" });
  scheduleReportType.focus();
}

function schedulePayload() {
  const reportType = scheduleReportType.value;
  const frequency = scheduleFrequency.value;
  const time = scheduleTime.value;
  const format = scheduleFormat.value;
  if (!REPORT_TYPE_LABELS[reportType]) throw new Error("请选择报告类型。");
  const capability = scheduleCapabilities?.[reportType];
  if (capability?.ready === false) {
    throw new Error(capability.reason || "该报告所需的数据维度尚未准备好。");
  }
  if (!FREQUENCY_LABELS[frequency]) throw new Error("请选择执行频率。");
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time)) {
    throw new Error("请选择精确到分钟的执行时间。");
  }
  if (!FORMAT_LABELS[format]) throw new Error("请选择文件格式。");

  const payload = { report_type: reportType, frequency, time, format };
  if (frequency === "once") {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(scheduleRunDate.value)) {
      throw new Error("请选择执行日期。");
    }
    payload.run_date = scheduleRunDate.value;
  } else if (frequency === "weekly") {
    const weekday = Number(scheduleWeekday.value);
    if (!Number.isInteger(weekday) || weekday < 1 || weekday > 7) {
      throw new Error("请选择执行星期。");
    }
    payload.weekday = weekday;
  }
  return payload;
}

async function saveSchedule(event) {
  event.preventDefault();
  if (schedulesBusy || !scheduleForm.reportValidity()) return;
  scheduleFormError.textContent = "";
  let payload;
  try {
    payload = schedulePayload();
  } catch (error) {
    scheduleFormError.textContent = error.message || String(error);
    return;
  }

  setSchedulesBusy(true);
  try {
    const editing = Boolean(editingScheduleId);
    const path = editing
      ? `/api/schedules/${encodeURIComponent(editingScheduleId)}`
      : "/api/schedules";
    await apiRequest(path, {
      method: editing ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    closeScheduleEditor();
    setScheduleNotice(editing ? "定时报告已更新。" : "定时报告已创建。");
  } catch (error) {
    scheduleFormError.textContent = `保存失败：${error.message || String(error)}`;
    setSchedulesBusy(false);
    return;
  }
  setSchedulesBusy(false);
  await loadSchedules({ preserveNotice: true });
}

async function mutateSchedule(schedule, action) {
  if (schedulesBusy) return;
  const scheduleId = scheduleIdentifier(schedule);
  if (!scheduleId || !["pause", "resume"].includes(action)) return;
  setSchedulesBusy(true);
  try {
    await apiRequest(`/api/schedules/${encodeURIComponent(scheduleId)}/${action}`, {
      method: "POST",
    });
    setScheduleNotice(action === "pause" ? "计划已暂停。" : "计划已恢复。");
  } catch (error) {
    setScheduleNotice(`操作失败：${error.message || String(error)}`, true);
    setSchedulesBusy(false);
    return;
  }
  setSchedulesBusy(false);
  await loadSchedules({ preserveNotice: true });
}

async function runScheduleNow(schedule) {
  if (schedulesBusy) return;
  const scheduleId = scheduleIdentifier(schedule);
  if (!scheduleId) return;
  if (!window.confirm(`确定立即生成“${scheduleReportLabel(schedule)}”吗？`)) return;
  setSchedulesBusy(true);
  try {
    await apiRequest(`/api/schedules/${encodeURIComponent(scheduleId)}/run`, {
      method: "POST",
    });
    setScheduleNotice("已提交生成任务，可在历史记录查看进度和结果。");
  } catch (error) {
    setScheduleNotice(`立即生成失败：${error.message || String(error)}`, true);
    setSchedulesBusy(false);
    return;
  }
  setSchedulesBusy(false);
  await loadSchedules({ preserveNotice: true });
  await refreshSessions().catch(() => {});
}

async function removeSchedule(schedule) {
  if (schedulesBusy) return;
  const scheduleId = scheduleIdentifier(schedule);
  if (!scheduleId) return;
  if (!window.confirm(`确定删除“${scheduleReportLabel(schedule)}”计划吗？已生成的历史报告不会被删除。`)) {
    return;
  }
  setSchedulesBusy(true);
  try {
    await apiRequest(`/api/schedules/${encodeURIComponent(scheduleId)}`, {
      method: "DELETE",
    });
    setScheduleNotice("定时报告计划已删除，历史报告仍可查看。");
  } catch (error) {
    setScheduleNotice(`删除失败：${error.message || String(error)}`, true);
    setSchedulesBusy(false);
    return;
  }
  setSchedulesBusy(false);
  await loadSchedules({ preserveNotice: true });
}

function analysisPresetDefinition(presetId) {
  return ANALYSIS_PRESET_DEFINITIONS[presetId] || ANALYSIS_PRESET_DEFINITIONS.refund_diagnosis;
}

function normalizedAnalysisPreset(raw, presetId = "") {
  const id = String(raw?.preset_id || raw?.id || presetId || "").trim();
  if (!id || !ANALYSIS_PRESET_DEFINITIONS[id]) return null;
  const fallback = analysisPresetDefinition(id);
  const ready =
    typeof raw?.ready === "boolean"
      ? raw.ready
      : typeof raw?.available === "boolean"
        ? raw.available
        : typeof raw?.enabled === "boolean"
          ? raw.enabled
          : fallback.fallbackAvailable !== false;
  const focusOptions = Array.isArray(raw?.focus_options)
    ? raw.focus_options
        .map((option) => {
          if (!option || typeof option !== "object") return null;
          const value = String(option.value || "").trim();
          const label = String(option.label || value).trim();
          return value && label ? { value, label } : null;
        })
        .filter(Boolean)
    : fallback.options.map(([value, label]) => ({ value, label }));
  const allowedDays = Array.isArray(raw?.allowed_days)
    ? raw.allowed_days.map(Number).filter((value) => [7, 30, 60, 90].includes(value))
    : [7, 30, 60, 90];
  const limitations = Array.isArray(raw?.limitations)
    ? raw.limitations.map((item) => String(item).trim()).filter(Boolean)
    : [];
  return {
    presetId: id,
    title: String(raw?.title || fallback.title).trim() || fallback.title,
    summary: String(raw?.summary || "").trim(),
    description: String(raw?.description || fallback.description).trim() || fallback.description,
    ready,
    reason: String(
      raw?.reason || raw?.blocked_reason || raw?.unavailable_reason || fallback.fallbackReason || "",
    ).trim(),
    limitations,
    allowedDays: allowedDays.length ? allowedDays : [7, 30, 60, 90],
    defaultDays: allowedDays.includes(Number(raw?.default_days))
      ? Number(raw.default_days)
      : Number(fallback.defaultDays),
    focusOptions,
    defaultFocus: String(raw?.default_focus || focusOptions[0]?.value || "").trim(),
    supportsTopN:
      typeof raw?.supports_top_n === "boolean" ? raw.supports_top_n : Boolean(fallback.showTopN),
    defaultTopN: [10, 20, 50].includes(Number(raw?.default_top_n))
      ? Number(raw.default_top_n)
      : 10,
  };
}

function fallbackAnalysisPreset(presetId) {
  return normalizedAnalysisPreset({}, presetId);
}

function currentAnalysisPreset() {
  return analysisPresets.get(selectedAnalysisPreset) || fallbackAnalysisPreset(selectedAnalysisPreset);
}

function updateAnalysisEntries() {
  analysisEntryButtons.forEach((button) => {
    const presetId = button.dataset.analysisPreset || "";
    const preset = analysisPresets.get(presetId) || fallbackAnalysisPreset(presetId);
    const blocked = preset?.ready === false;
    button.classList.toggle("blocked", blocked);
    button.setAttribute("aria-disabled", blocked ? "true" : "false");
    button.title = blocked
      ? `${preset.title}：${preset.reason || "当前数据不支持"}（点击查看说明）`
      : preset?.summary || preset?.description || "";
  });
}

function setAnalysisOptions(select, options, selectedValue = "") {
  select.replaceChildren();
  for (const option of options) {
    const element = document.createElement("option");
    element.value = option.value;
    element.textContent = option.label;
    select.append(element);
  }
  if (options.some((option) => option.value === selectedValue)) {
    select.value = selectedValue;
  }
}

function updateAnalysisFormAvailability() {
  if (!analysisForm || !startAnalysisButton) return;
  const preset = currentAnalysisPreset();
  const disabled = analysisBusy || busy || preset?.ready === false;
  Array.from(analysisForm.elements).forEach((control) => {
    control.disabled = disabled;
  });
  startAnalysisButton.disabled = disabled;
}

function renderAnalysisPreset(presetId, { reset = false } = {}) {
  selectedAnalysisPreset = ANALYSIS_PRESET_DEFINITIONS[presetId]
    ? presetId
    : "refund_diagnosis";
  const fallback = analysisPresetDefinition(selectedAnalysisPreset);
  const preset = currentAnalysisPreset();
  analysisEyebrow.textContent = fallback.eyebrow;
  analysisTitle.textContent = preset.title;
  analysisHeading.textContent = preset.title;
  analysisDescription.textContent = preset.description;
  analysisStatus.textContent = preset.ready ? "数据能力已就绪" : "当前数据暂不支持";
  analysisStatus.className = `analysis-status${preset.ready ? "" : " blocked"}`;

  const capabilityNotes = [];
  if (preset.ready && preset.limitations.length) capabilityNotes.push(...preset.limitations);
  if (!preset.ready) {
    capabilityNotes.push(preset.reason || "当前报表缺少该分析所需的明细维度。");
  }
  analysisCapabilityNote.textContent = capabilityNotes.join("\n");
  analysisCapabilityNote.className = `analysis-capability-note${preset.ready ? "" : " blocked"}`;

  const periodOptions = preset.allowedDays.map((days) => ({
    value: String(days),
    label: `最近 ${days} 天`,
  }));
  const currentDays = reset ? String(preset.defaultDays) : analysisPeriod.value;
  setAnalysisOptions(analysisPeriod, periodOptions, currentDays);
  analysisFocusLabel.textContent = fallback.focusLabel;
  const currentFocus = reset ? preset.defaultFocus : analysisFocus.value;
  setAnalysisOptions(analysisFocus, preset.focusOptions, currentFocus);
  analysisFocusField.hidden = preset.focusOptions.length === 0;
  analysisTopField.hidden = !preset.supportsTopN;
  if (reset) {
    analysisTopN.value = String(preset.defaultTopN);
    analysisNote.value = "";
  }
  analysisNote.placeholder = fallback.notePlaceholder;
  analysisFormError.textContent = "";
  updateAnalysisFormAvailability();
  updateAnalysisEntries();
}

async function loadAnalysisPresets() {
  try {
    const data = await apiRequest("/api/analysis-presets");
    const rows = Array.isArray(data?.data) ? data.data : [];
    analysisPresets = new Map(
      rows
        .map((row) => normalizedAnalysisPreset(row))
        .filter(Boolean)
        .map((preset) => [preset.presetId, preset]),
    );
  } catch (error) {
    analysisPresets = new Map();
    if (!analysisPanel.hidden) {
      analysisFormError.textContent = `能力检查失败：${error.message || String(error)}`;
    }
  }
  updateAnalysisEntries();
  if (!analysisPanel.hidden) renderAnalysisPreset(selectedAnalysisPreset);
}

async function showMemoryView() {
  chatPanel.hidden = true;
  schedulePanel.hidden = true;
  analysisPanel.hidden = true;
  memoryPanel.hidden = false;
  memoryViewButton.classList.add("active");
  scheduleViewButton.classList.remove("active");
  analysisEntryButtons.forEach((button) => button.classList.remove("active"));
  await loadMemoryFiles();
}

async function showScheduleView() {
  chatPanel.hidden = true;
  memoryPanel.hidden = true;
  analysisPanel.hidden = true;
  schedulePanel.hidden = false;
  memoryViewButton.classList.remove("active");
  scheduleViewButton.classList.add("active");
  analysisEntryButtons.forEach((button) => button.classList.remove("active"));
  await loadSchedules();
}

async function showAnalysisView(presetId) {
  chatPanel.hidden = true;
  memoryPanel.hidden = true;
  schedulePanel.hidden = true;
  analysisPanel.hidden = false;
  memoryViewButton.classList.remove("active");
  scheduleViewButton.classList.remove("active");
  analysisEntryButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.analysisPreset === presetId);
  });
  renderAnalysisPreset(presetId, { reset: true });
  if (analysisPresets.size === 0) await loadAnalysisPresets();
}

function showChatView() {
  memoryPanel.hidden = true;
  schedulePanel.hidden = true;
  analysisPanel.hidden = true;
  chatPanel.hidden = false;
  memoryViewButton.classList.remove("active");
  scheduleViewButton.classList.remove("active");
  analysisEntryButtons.forEach((button) => button.classList.remove("active"));
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

function displaySessionTitle(value) {
  const title = String(value || "").trim();
  const preset = title.match(
    /^执行预设分析「([^」]+)」[。，][\s\S]*?最近\s+(\d+)/,
  );
  if (preset) return `${preset[1]} · 最近 ${preset[2]} 天`;
  return title || "新对话";
}

function isScheduledSession(session) {
  return String(session?.origin || "").toLowerCase() === "scheduled";
}

function scheduledRunStatus(session) {
  const status = String(session?.run_status || "").toLowerCase();
  if (["running", "in_progress"].includes(status)) {
    return { label: "生成中", className: "running" };
  }
  if (["failed", "error"].includes(status)) {
    return { label: "失败", className: "failed" };
  }
  if (["queued", "pending"].includes(status)) {
    return { label: "等待执行", className: "pending" };
  }
  if (["succeeded", "success", "completed"].includes(status)) {
    return { label: "已完成", className: "succeeded" };
  }
  return {
    label: session?.status === "closed" ? "已结束" : "自动执行",
    className: "scheduled",
  };
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
    const scheduled = isScheduledSession(session);
    const runStatus = scheduled ? scheduledRunStatus(session) : null;
    const item = document.createElement("div");
    item.className = `session-item${
      currentSession?.session_id === session.session_id ? " active" : ""
    }${scheduled ? " scheduled" : ""}${runStatus ? ` ${runStatus.className}` : ""}`;

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "session-open";
    const visibleTitle = displaySessionTitle(session.title);
    openButton.title = scheduled
      ? `${visibleTitle || "自动报告"}（自动生成）`
      : visibleTitle;

    const titleRow = document.createElement("span");
    titleRow.className = "session-title-row";
    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = visibleTitle;
    titleRow.append(title);
    if (scheduled) {
      const originBadge = document.createElement("span");
      originBadge.className = "session-origin-badge";
      originBadge.textContent = "自动";
      titleRow.append(originBadge);
    }

    const meta = document.createElement("span");
    meta.className = "session-meta";
    const status = scheduled
      ? runStatus.label
      : session.status === "closed"
        ? "已结束"
        : `${session.completed_turn_count} 轮`;
    meta.textContent = `${status} · ${formatSessionTime(session.last_active_at)}`;

    openButton.append(titleRow, meta);
    openButton.addEventListener("click", () => {
      showChatView();
      openSession(session.session_id);
    });

    const menu = document.createElement("div");
    menu.className = "session-menu";
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.title = "删除对话";
    deleteButton.setAttribute("aria-label", `删除 ${visibleTitle || "对话"}`);
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
  currentSessionTitle.textContent = displaySessionTitle(currentSession?.title);
  if (isScheduledSession(currentSession)) {
    currentSessionEyebrow.textContent = `自动报告 · ${scheduledRunStatus(currentSession).label}`;
  } else {
    currentSessionEyebrow.textContent = "当前对话";
  }
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

function displayUserMessage(value) {
  const content = messageText(value);
  const preset = content.match(
    /^执行预设分析「([^」]+)」[。，][\s\S]*?最近\s+(\d+)\s+个完整自然日/,
  );
  if (preset) {
    return `发起${preset[1]} · 最近 ${preset[2]} 天 · 使用预设分析口径`;
  }
  return content;
}

function renderHistoryMessages(messages) {
  let reasoningParts = [];
  let progressParts = [];
  let answerParts = [];

  function flushAssistantTurn() {
    if (!reasoningParts.length && !progressParts.length && !answerParts.length) return;
    const finalAnswer = answerParts.at(-1) || "";
    const view = addAssistantMessage(finalAnswer);
    for (const progress of progressParts) {
      appendAssistantProgress(view, progress, { live: false });
    }
    setAssistantReasoning(view, reasoningParts.join("\n\n"));
    reasoningParts = [];
    progressParts = [];
    answerParts = [];
  }

  for (const message of messages || []) {
    if (message.role === "user") {
      flushAssistantTurn();
      const content = displayUserMessage(message.content);
      if (content) addMessage("user", content);
      continue;
    }
    if (message.role === "tool") {
      progressParts.push(
        toolProgressText("tool.completed", { tool_name: message.tool_name }),
      );
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

function renderScheduledRunNotice(session) {
  if (!isScheduledSession(session)) return;
  const status = scheduledRunStatus(session);
  if (status.className === "failed") {
    const rawSummary = String(session?.error_summary || "").trim();
    const summary = (rawSummary || "请稍后重试或检查任务配置。").slice(0, 500);
    addMessage("assistant", `自动报告生成失败：${summary}`, "error");
  } else if (status.className === "running") {
    addMessage("assistant", "自动报告正在生成，完成后将在本会话显示下载文件。");
  } else if (status.className === "pending") {
    addMessage("assistant", "自动报告已进入执行队列，请稍后刷新查看结果。");
  }
}

async function loadMessages(sessionId, loadVersion) {
  clearMessages();
  renderEmpty("正在加载历史记录…");
  const artifactRequest = fetchSessionArtifacts(sessionId).catch(() => null);
  const data = await apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
  const artifactData = await artifactRequest;
  if (
    loadVersion !== historyLoadVersion ||
    currentSession?.session_id !== sessionId
  ) return;
  clearMessages();
  renderHistoryMessages(data.data);
  renderScheduledRunNotice(currentSession);
  if (artifactData !== null) {
    knownArtifactIdsBySession.set(sessionId, new Set());
    renderSessionArtifacts(artifactData, sessionId);
  }
  renderEmpty(currentSession?.status === "closed" ? "该对话已结束" : undefined);
  if (data.session_id && data.session_id !== sessionId) {
    localStorage.setItem(CURRENT_SESSION_STORAGE, data.session_id);
  }
}

async function openSession(sessionId, { allowBusy = false } = {}) {
  if (busy && !allowBusy) return;
  const session = sessions.find((item) => item.session_id === sessionId);
  if (!session) return;
  const loadVersion = ++historyLoadVersion;
  updateCurrentSession(session);
  try {
    await loadMessages(sessionId, loadVersion);
  } catch (error) {
    if (
      loadVersion !== historyLoadVersion ||
      currentSession?.session_id !== sessionId
    ) return;
    clearMessages();
    addMessage("assistant", error.message || String(error), "error");
  }
}

async function createSession({ closeCurrent = false } = {}) {
  setBusy(true);
  try {
    const body = {};
    if (
      closeCurrent &&
      currentSession?.status === "active" &&
      currentSession?.read_only !== true
    ) {
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
  if (session?.read_only === true) return;
  const title = window.prompt("输入新的对话名称", displaySessionTitle(session.title))?.trim();
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
  if (
    !currentSession ||
    currentSession.status !== "active" ||
    currentSession.read_only === true ||
    busy
  ) return;
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
  if (!window.confirm(`确定删除“${displaySessionTitle(session.title)}”及其历史记录吗？`)) return;
  if (busy) return;
  setBusy(true);
  try {
    await apiRequest(`/api/sessions/${encodeURIComponent(session.session_id)}`, {
      method: "DELETE",
    });
    knownArtifactIdsBySession.delete(session.session_id);
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

const INTERNAL_TERM_LABELS = Object.freeze({
  db_get_schema_ddl: "数据结构检查",
  db_schema_search: "相关数据检索",
  db_get_table_profile: "字段与口径确认",
  db_get_join_paths: "数据关联检查",
  db_validate_sql: "查询方案校验",
  db_execute_sql: "数据查询",
  export_report_file: "报表生成",
  _xpd_clarify: "口径确认",
  _thinking: "分析进度",
});

const TOOL_PROGRESS_LABELS = Object.freeze({
  db_get_schema_ddl: {
    started: "正在检查数据结构…",
    completed: "数据结构检查完成，正在继续分析…",
  },
  db_schema_search: {
    started: "正在查找相关数据…",
    completed: "已找到相关数据，正在确认字段与口径…",
  },
  db_get_table_profile: {
    started: "正在确认字段与数据口径…",
    completed: "字段与口径确认完成，正在规划查询…",
  },
  db_get_join_paths: {
    started: "正在检查数据关联关系…",
    completed: "数据关联关系检查完成，正在规划查询…",
  },
  db_validate_sql: {
    started: "正在校验查询方案…",
    completed: "查询方案校验完成，正在获取数据…",
  },
  db_execute_sql: {
    started: "正在获取并汇总数据…",
    completed: "数据获取完成，正在整理结果…",
  },
  export_report_file: {
    started: "正在生成报表文件…",
    completed: "报表文件生成完成，正在整理下载信息…",
  },
});

function sanitizeInternalTermsForDisplay(value) {
  let text = typeof value === "string" ? value : "";
  for (const [internalName, displayName] of Object.entries(INTERNAL_TERM_LABELS)) {
    text = text.split(internalName).join(displayName);
  }
  return text;
}

function toolProgressText(event, data) {
  const toolName = data?.tool_name || data?.tool || "";
  const labels = TOOL_PROGRESS_LABELS[toolName];
  if (event === "tool.failed" || data?.status === "failed" || data?.status === "error") {
    return "数据处理遇到问题，请稍后重试";
  }
  if (event === "tool.completed" || data?.status === "completed") {
    return labels?.completed || "数据处理完成，正在整理结果…";
  }
  return labels?.started || "正在处理数据…";
}

async function sendStreamingRequest(url, payload, assistantView, requestSessionId) {
  const response = await fetch(url, {
    method: "POST",
    headers: sessionHeaders(true),
    body: JSON.stringify(payload),
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
  let reasoning = "";
  let streamError = null;

  function handleEvent(block) {
    if (!block.trim()) return;
    const { event, payload } = parseSseEvent(block);
    if (!payload) return;
    const data = parseJson(payload);

    if (event === "error") {
      streamError = new Error(sanitizeInternalTermsForDisplay(data?.message || payload));
      return;
    }
    if (
      data?.session_id &&
      data.session_id !== requestSessionId &&
      currentSession?.session_id === requestSessionId
    ) {
      currentSession.session_id = data.session_id;
      localStorage.setItem(CURRENT_SESSION_STORAGE, data.session_id);
    }
    if (event === "artifact.ready") {
      renderArtifactCard(assistantView, data, requestSessionId);
    } else if (event === "tool.started" && data?.tool_name === "_xpd_clarify") {
      renderClarificationCard(assistantView, data.args, requestSessionId);
    } else if (event === "tool.completed" && data?.tool_name === "_xpd_clarify") {
      updateClarificationCard(assistantView, data.args);
    } else if (event === "assistant.delta" && typeof data?.delta === "string") {
      content += data.delta;
      assistantView.contentEl.textContent = sanitizeInternalTermsForDisplay(content);
    } else if (event === "assistant.completed" && typeof data?.content === "string") {
      content = data.content;
      renderAssistantContent(assistantView.contentEl, content, { final: true });
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
      const progressText = toolProgressText(event, data);
      appendAssistantProgress(assistantView, progressText);
    } else {
      const reasoningPiece = legacyReasoningDelta(data);
      if (reasoningPiece) {
        reasoning += reasoningPiece;
        setAssistantReasoning(assistantView, reasoning, { live: true });
      }
      const piece = legacyDelta(data);
      if (piece) {
        content += piece;
        assistantView.contentEl.textContent = sanitizeInternalTermsForDisplay(content);
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
  if (streamError) throw streamError;
  return { content, reasoning };
}

function sendStreaming(message, assistantView, requestSessionId) {
  return sendStreamingRequest(
    `/api/sessions/${encodeURIComponent(requestSessionId)}/chat/stream`,
    { message, stream: true },
    assistantView,
    requestSessionId,
  );
}

function sendPresetAnalysis(payload, assistantView, requestSessionId) {
  return sendStreamingRequest(
    `/api/sessions/${encodeURIComponent(requestSessionId)}/analyses`,
    payload,
    assistantView,
    requestSessionId,
  );
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
  if (
    !message ||
    busy ||
    currentSession?.status !== "active" ||
    currentSession?.read_only === true
  ) return;
  const requestSessionId = currentSession.session_id;

  input.value = "";
  addMessage("user", message);
  const assistantView = addAssistantMessage();
  setBusy(true);

  try {
    const result = await sendStreaming(message, assistantView, requestSessionId);
    try {
      await syncNewSessionArtifacts(assistantView, requestSessionId);
    } catch {
      // The streamed answer remains valid if the best-effort artifact refresh fails.
    }
    const clarificationExpired = Array.from(
      assistantView.clarificationCards.values(),
    ).some((state) => state.status === "expired");
    renderAssistantContent(assistantView.contentEl,
      clarificationExpired
        ? "澄清已超时，本轮分析已停止，未执行依赖该答案的查询。"
        : result.content || assistantView.contentEl.textContent || "已完成",
      { final: true },
    );
    setAssistantReasoning(assistantView, result.reasoning);
    await refreshSessions();
  } catch (error) {
    try {
      await syncNewSessionArtifacts(assistantView, requestSessionId);
    } catch {
      // The stream error remains the primary error shown to the user.
    }
    if (assistantView.artifactCards.size > 0) {
      assistantView.contentEl.textContent = sanitizeInternalTermsForDisplay(
        `回答中断：${error.message || String(error)}。已生成的文件仍可下载。`,
      );
      assistantView.messageEl.classList.add("error");
    } else {
      assistantView.messageEl.remove();
      addMessage(
        "assistant",
        sanitizeInternalTermsForDisplay(error.message || String(error)),
        "error",
      );
    }
  } finally {
    setBusy(false);
    input.focus();
  }
}

async function handleAnalysisSubmit(event) {
  event.preventDefault();
  if (analysisBusy || busy) return;
  const preset = currentAnalysisPreset();
  if (!preset?.ready) {
    analysisFormError.textContent = preset?.reason || "当前数据暂不支持该分析。";
    return;
  }

  const days = Number(analysisPeriod.value);
  const topN = Number(analysisTopN.value);
  const focus = analysisFocus.value;
  const note = analysisNote.value.trim();
  if (!preset.allowedDays.includes(days)) {
    analysisFormError.textContent = "请选择有效的分析周期。";
    return;
  }
  if (preset.focusOptions.length && !preset.focusOptions.some((item) => item.value === focus)) {
    analysisFormError.textContent = "请选择有效的分析维度。";
    return;
  }
  if (preset.supportsTopN && ![10, 20, 50].includes(topN)) {
    analysisFormError.textContent = "请选择有效的排行数量。";
    return;
  }

  analysisBusy = true;
  analysisFormError.textContent = "";
  updateAnalysisFormAvailability();
  try {
    if (
      !currentSession ||
      currentSession.status !== "active" ||
      currentSession.read_only === true
    ) {
      await createSession();
    }
    if (!currentSession?.session_id) throw new Error("无法创建分析对话。");

    const requestSessionId = currentSession.session_id;
    const payload = {
      preset_id: preset.presetId,
      days,
    };
    if (preset.focusOptions.length) payload.focus = focus;
    if (preset.supportsTopN) payload.top_n = topN;
    if (note) payload.note = note;

    if (!currentSession.title || currentSession.title === "新对话") {
      try {
        const renamed = await apiRequest(
          `/api/sessions/${encodeURIComponent(requestSessionId)}`,
          {
            method: "PATCH",
            body: JSON.stringify({ title: `${preset.title} · 最近 ${days} 天` }),
          },
        );
        if (renamed.session) {
          sessions = sessions.map((item) =>
            item.session_id === requestSessionId ? renamed.session : item,
          );
          updateCurrentSession(renamed.session);
        }
      } catch {
        // A cosmetic title update must never block the analysis itself.
      }
    }

    const focusLabel = preset.focusOptions.find((item) => item.value === focus)?.label;
    const requestParts = [`最近 ${days} 天`];
    if (focusLabel) requestParts.push(focusLabel);
    if (preset.supportsTopN) requestParts.push(`Top ${topN}`);
    if (note) requestParts.push(note);

    showChatView();
    addMessage("user", `发起${preset.title}：${requestParts.join("·")}`);
    const assistantView = addAssistantMessage();
    setBusy(true);
    try {
      const result = await sendPresetAnalysis(payload, assistantView, requestSessionId);
      try {
        await syncNewSessionArtifacts(assistantView, requestSessionId);
      } catch {
        // The streamed answer remains valid if the best-effort artifact refresh fails.
      }
      const clarificationExpired = Array.from(
        assistantView.clarificationCards.values(),
      ).some((state) => state.status === "expired");
      renderAssistantContent(assistantView.contentEl,
        clarificationExpired
          ? "澄清已超时，本轮分析已停止，未执行依赖该答案的查询。"
          : result.content || assistantView.contentEl.textContent || "已完成",
        { final: true },
      );
      setAssistantReasoning(assistantView, result.reasoning);
      await refreshSessions();
    } catch (error) {
      try {
        await syncNewSessionArtifacts(assistantView, requestSessionId);
      } catch {
        // The stream error remains the primary error shown to the user.
      }
      if (assistantView.artifactCards.size > 0) {
        assistantView.contentEl.textContent = sanitizeInternalTermsForDisplay(
          `回答中断：${error.message || String(error)}。已生成的文件仍可下载。`,
        );
        assistantView.messageEl.classList.add("error");
      } else {
        assistantView.messageEl.remove();
        addMessage(
          "assistant",
          sanitizeInternalTermsForDisplay(error.message || String(error)),
          "error",
        );
      }
    } finally {
      setBusy(false);
    }
  } catch (error) {
    analysisFormError.textContent = error.message || String(error);
  } finally {
    analysisBusy = false;
    updateAnalysisFormAvailability();
  }
}

async function checkHealth() {
  try {
    const response = await fetch("/health");
    const data = await response.json();
    if (data.ok) {
      setStatus("服务正常 · 记忆已开启", "ready");
    } else if (data.hermes?.ok && !data.clarify?.ok) {
      setStatus("澄清功能未就绪", "error");
    } else if (data.hermes?.ok && !data.memory?.ok) {
      setStatus("记忆功能未就绪", "error");
    } else if (data.hermes?.ok && !data.report_files?.ok) {
      setStatus("文件导出未就绪", "error");
    } else if (data.hermes_api_key_configured) {
      setStatus("Hermes 服务离线", "error");
    } else {
      setStatus("API Key 未配置", "error");
    }
  } catch {
    setStatus("页面服务离线", "error");
  }
}

async function initialize() {
  setBusy(true);
  await checkHealth();
  await loadAnalysisPresets();
  try {
    await refreshSessions();
    const savedId = localStorage.getItem(CURRENT_SESSION_STORAGE);
    const saved = sessions.find((item) => item.session_id === savedId);
    const savedActive = saved?.status === "active" && saved?.read_only !== true;
    const active = savedActive
      ? saved
      : sessions.find((item) => item.status === "active" && item.read_only !== true);
    if (active) {
      await openSession(active.session_id, { allowBusy: true });
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
newChatButton.addEventListener("click", () => {
  showChatView();
  createSession({ closeCurrent: true });
});
renameChatButton.addEventListener("click", () => currentSession && renameSession(currentSession));
endChatButton.addEventListener("click", endCurrentSession);
memoryViewButton.addEventListener("click", showMemoryView);
refreshMemoryButton.addEventListener("click", loadMemoryFiles);
backToChatButton.addEventListener("click", showChatView);
scheduleViewButton.addEventListener("click", showScheduleView);
refreshScheduleButton.addEventListener("click", () => loadSchedules());
newScheduleButton.addEventListener("click", () => openScheduleEditor());
backFromScheduleButton.addEventListener("click", showChatView);
cancelScheduleButton.addEventListener("click", closeScheduleEditor);
scheduleFrequency.addEventListener("change", updateScheduleFrequencyFields);
scheduleForm.addEventListener("submit", saveSchedule);
backFromAnalysisButton.addEventListener("click", showChatView);
analysisForm.addEventListener("submit", handleAnalysisSubmit);
analysisEntryButtons.forEach((button) => {
  button.addEventListener("click", () => showAnalysisView(button.dataset.analysisPreset));
});

exampleButtons.forEach((button) => {
  button.addEventListener("click", () => {
    input.value = button.dataset.example || "";
    input.focus();
  });
});

initialize();
