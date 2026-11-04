"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const MODALITY_LABELS = { text: "文字", image: "图像", audio: "语音" };
const CAPABILITY_LABELS = { text: "文字", image: "图像", audio: "语音" };

const state = {
  token: sessionStorage.getItem("dounai.token") || "",
  sessionId: sessionStorage.getItem("dounai.session") || newSessionId(),
  tab: "text",
  imageFile: null,
  audioFile: null,
  imageUrl: null,
  audioUrl: null,
  providers: [],
  providerStatus: null,
  privacy: null,
  policy: null,
  models: [],
  apiConnections: [],
  apiPresets: [],
  apiDiscovered: [],
  apiSelectedModels: [],
  apiProbes: {},
  apiDeleteId: null,
  history: [],
  event: null,
  diagnostics: null,
  recording: null,
  recordStartedAt: 0,
  recordTimer: null,
  recordStream: null,
  voiceSamples: [],
};

sessionStorage.setItem("dounai.session", state.sessionId);

function newSessionId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // Fallback for old browsers: build an RFC 4122 v4 UUID from random bytes.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  let body = options.body;
  if (body && !(body instanceof FormData) && typeof body !== "string") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, { method: options.method || "GET", headers, body });
  } catch (error) {
    throw new ApiError("无法连接输入层，请确认服务正在运行。", 0, null);
  }
  if (response.status === 401) {
    openTokenDialog();
  }
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    const message = payload?.error?.message || `请求失败（HTTP ${response.status}）`;
    throw new ApiError(message, response.status, payload);
  }
  if (options.raw) return response;
  if (response.status === 204) return null;
  return response.json();
}

function toast(message, kind = "info") {
  const stack = $("#toastStack");
  const element = document.createElement("div");
  element.className = `toast${kind === "error" ? " is-error" : kind === "success" ? " is-success" : ""}`;
  element.textContent = message;
  stack.appendChild(element);
  window.setTimeout(() => {
    element.classList.add("is-leaving");
    window.setTimeout(() => element.remove(), 220);
  }, 4200);
}

async function checkHealth() {
  const pill = $("#healthPill");
  try {
    const response = await fetch("/healthz", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("unhealthy");
    const data = await response.json();
    pill.className = "status-pill is-online";
    $("#healthText").textContent = `在线 · ${data.active_provider || "offline"}`;
  } catch (_) {
    pill.className = "status-pill is-offline";
    $("#healthText").textContent = "未连接";
  }
}

function renderProviders() {
  const select = $("#providerSelect");
  const current = state.providerStatus?.active_provider;
  select.innerHTML = "";
  state.providers.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider.id;
    const suffix = provider.available ? "" : "（未配置）";
    option.textContent = `${provider.name}${suffix}`;
    if (!provider.available) option.disabled = true;
    if (provider.id === current) option.selected = true;
    select.appendChild(option);
  });
  if (!state.providers.length) {
    const option = document.createElement("option");
    option.textContent = "没有可用模型";
    select.appendChild(option);
  }

  const active = state.providers.find((provider) => provider.id === current);
  const badge = $("#providerPrivacy");
  if (active) {
    const remote = active.privacy === "remote";
    badge.textContent = remote ? "云端" : "本地";
    badge.className = `privacy-badge${remote ? " is-remote" : ""}`;
    $("#providerNote").textContent = remote
      ? "语音会发送到该厂商；密钥只保存在本机加密存储，不写入浏览器。"
      : "处理留在本机；本地模型权重需单独安装。";
    const caps = $("#providerCapabilities");
    caps.innerHTML = "";
    (active.capabilities || []).forEach((capability) => {
      const chip = document.createElement("span");
      chip.className = "cap-chip";
      chip.textContent = CAPABILITY_LABELS[capability] || capability;
      caps.appendChild(chip);
    });
  } else {
    badge.textContent = "未知";
    badge.className = "privacy-badge";
    $("#providerCapabilities").innerHTML = "";
    $("#providerNote").textContent = "请检查配置。";
  }

  const list = $("#providerList");
  list.innerHTML = "";
  state.providers.forEach((provider) => {
    const row = document.createElement("div");
    row.className = `provider-row${provider.active ? " is-active" : ""}`;
    const copy = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = provider.name;
    const kind = document.createElement("span");
    kind.className = `provider-tag${provider.privacy === "remote" ? " remote" : ""}`;
    kind.textContent = provider.privacy === "remote" ? "云端" : "本地";
    name.appendChild(kind);
    if (!provider.available) {
      const unavailable = document.createElement("span");
      unavailable.className = "provider-tag unavailable";
      unavailable.textContent = "未配置";
      name.appendChild(unavailable);
    }
    const description = document.createElement("p");
    description.textContent = provider.available
      ? provider.description
      : provider.unavailable_reason || "当前不可用";
    copy.append(name, description);
    row.appendChild(copy);
    const button = document.createElement("button");
    button.className = "provider-action";
    button.type = "button";
    if (provider.active) {
      button.textContent = "使用中";
      button.disabled = true;
    } else {
      button.textContent = "切换";
      button.disabled = !provider.available;
      button.addEventListener("click", () => switchProvider(provider.id));
    }
    row.appendChild(button);
    list.appendChild(row);
  });
}

async function loadProviders() {
  try {
    const data = await api("/v1/providers");
    state.providers = data.providers || [];
    state.providerStatus = data.status || null;
    renderProviders();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function switchProvider(providerId) {
  const provider = state.providers.find((item) => item.id === providerId);
  if (!provider) return;
  if (provider.privacy === "remote") {
    const confirmed = window.confirm(
      `切换到「${provider.name}」后，语音会被发送到该第三方厂商。\n\n声纹模板始终留在本机。是否继续？`
    );
    if (!confirmed) return;
  }
  try {
    await api("/v1/providers/active", { method: "PUT", body: { provider_id: providerId } });
    toast(`已切换到 ${provider.name}`, "success");
    await loadProviders();
    await checkHealth();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadModels() {
  try {
    const data = await api("/v1/models");
    state.models = data.models || [];
    const list = $("#localModelList");
    list.innerHTML = "";
    const installed = state.models.filter((model) => model.installed).length;
    $("#localModelCount").textContent = `${installed} / ${state.models.length} 已安装`;
    state.models.forEach((model) => {
      const row = document.createElement("div");
      row.className = "local-model";
      const copy = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = model.name;
      const description = document.createElement("small");
      description.textContent = `${model.approximate_size} · ${model.description}`;
      copy.append(name, description);
      const status = document.createElement("span");
      status.className = `model-state${model.installed ? "" : " not-installed"}`;
      status.textContent = model.installed ? "已安装" : "未安装";
      row.append(copy, status);
      list.appendChild(row);
    });
  } catch (error) {
    $("#localModelList").textContent = error.message;
  }
}

async function loadPrivacy() {
  try {
    const status = await api("/v1/privacy/status");
    state.privacy = status;
    $("#voiceprintSwitch").checked = Boolean(status.enabled);
    $("#policyVersion").textContent = status.policy_version || "1.0";
    $("#retentionNote").textContent = `媒体与事件默认保留 ${status.retention_hours ?? 72} 小时后自动删除。`;
    const consent = Boolean(status.consent_granted);
    const consentState = $("#consentState");
    consentState.textContent = consent
      ? `已授权 · ${new Date(status.consent_recorded_at).toLocaleString("zh-CN")}`
      : "尚未授权";
    consentState.className = `consent-state${consent ? " is-granted" : ""}`;
    $("#consentCheck").checked = consent ? $("#consentCheck").checked : false;
    $("#grantConsentButton").disabled = !$("#consentCheck").checked || consent;
    $("#withdrawConsentButton").disabled = !consent;
    $("#enrollButton").disabled = !(consent && status.enabled && state.voiceSamples.length >= 3);

    const chips = $("#privacyStatus");
    chips.innerHTML = "";
    addChip(chips, status.enabled ? "功能已开启" : "功能已关闭", status.enabled ? "is-local" : "");
    addChip(chips, consent ? "已授权" : "未授权");
    addChip(chips, status.enrolled ? "声纹模板已录入" : "未录入模板", status.enrolled ? "is-local" : "");
    addChip(chips, `事件 ${status.data?.events ?? 0}`, "");
    addChip(chips, "仅本机计算", "is-local");
  } catch (error) {
    toast(error.message, "error");
  }
}

function addChip(container, text, className = "") {
  const chip = document.createElement("span");
  chip.className = `status-chip ${className}`.trim();
  chip.textContent = text;
  container.appendChild(chip);
}

async function loadPolicy(force = false) {
  if (state.policy && !force) return state.policy;
  state.policy = await api("/v1/privacy/policy");
  $("#policyText").textContent = state.policy.text;
  $("#policyVersion").textContent = state.policy.version;
  return state.policy;
}

function openDialog(dialog) {
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeDialog(dialog) {
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function openTokenDialog() {
  const dialog = $("#tokenDialog");
  if (!dialog.open) {
    $("#tokenInput").value = state.token;
    openDialog(dialog);
  }
}

function setTab(tab) {
  state.tab = tab;
  $$(".tab").forEach((button) => {
    const active = button.dataset.tab === tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
  ["text", "image", "audio"].forEach((name) => {
    const panel = $(`#panel-${name}`);
    panel.hidden = name !== tab;
    panel.classList.toggle("is-active", name === tab);
  });
}

function setImageFile(file) {
  if (!file) return;
  if (!/^image\/(jpeg|png|webp|gif)$/.test(file.type)) {
    toast("请选择 JPEG、PNG、WebP 或 GIF 图像。", "error");
    return;
  }
  if (file.size > 15 * 1024 * 1024) {
    toast("图像超过 15 MB 限制。", "error");
    return;
  }
  if (state.imageUrl) URL.revokeObjectURL(state.imageUrl);
  state.imageFile = file;
  state.imageUrl = URL.createObjectURL(file);
  $("#imagePreview").src = state.imageUrl;
  $("#imageMeta").textContent = `${file.name} · ${formatBytes(file.size)}`;
  $("#imagePreviewCard").hidden = false;
}

function clearImage() {
  if (state.imageUrl) URL.revokeObjectURL(state.imageUrl);
  state.imageUrl = null;
  state.imageFile = null;
  $("#imageInput").value = "";
  $("#imagePreviewCard").hidden = true;
  $("#imagePreview").removeAttribute("src");
}

function setAudioFile(file, label) {
  if (!file) return;
  if (file.size > 25 * 1024 * 1024) {
    toast("音频超过 25 MB 限制。", "error");
    return;
  }
  if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
  state.audioFile = file;
  state.audioUrl = URL.createObjectURL(file);
  $("#audioPreview").src = state.audioUrl;
  $("#audioName").textContent = label || file.name || "录音.wav";
  $("#audioMeta").textContent = `${formatBytes(file.size)} · ${file.type || "audio"}`;
  $("#audioPreviewCard").hidden = false;
}

function clearAudio() {
  if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
  state.audioUrl = null;
  state.audioFile = null;
  $("#audioInput").value = "";
  $("#audioPreviewCard").hidden = true;
  $("#audioPreview").removeAttribute("src");
}

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function setLoading(loading) {
  const button = $("#sendButton");
  button.classList.toggle("is-loading", loading);
  button.disabled = loading;
  $("#sendLabel").textContent = loading ? "处理中…" : "送入输入层";
}

async function sendCurrent() {
  try {
    setLoading(true);
    let response;
    if (state.tab === "text") {
      const text = $("#textInput").value;
      if (!text.trim()) {
        toast("先写点什么再发送。", "error");
        return;
      }
      response = await api("/v1/inputs/text", {
        method: "POST",
        body: { text, session_id: state.sessionId, source: "web-console" },
        raw: true,
      });
    } else if (state.tab === "image") {
      if (!state.imageFile) {
        toast("请先选择一张图像。", "error");
        return;
      }
      const form = new FormData();
      form.append("file", state.imageFile, state.imageFile.name);
      form.append("session_id", state.sessionId);
      form.append("source", "web-console");
      response = await api("/v1/inputs/image", { method: "POST", body: form, raw: true });
    } else {
      if (!state.audioFile) {
        toast("请先录音或选择音频。", "error");
        return;
      }
      const form = new FormData();
      form.append("file", state.audioFile, state.audioFile.name || "voice.wav");
      form.append("session_id", state.sessionId);
      form.append("source", "web-console");
      form.append("language", "zh");
      response = await api("/v1/inputs/audio", { method: "POST", body: form, raw: true });
    }
    const session = response.headers.get("X-Dounai-Session");
    if (session) {
      state.sessionId = session;
      sessionStorage.setItem("dounai.session", session);
      renderSession();
    }
    const diagnostics = {
      provider: response.headers.get("X-Dounai-Provider"),
      model: response.headers.get("X-Dounai-Model"),
      degraded: response.headers.get("X-Dounai-Degraded") === "true",
      asrStatus: response.headers.get("X-Dounai-ASR-Status"),
      voiceprintChecked: response.headers.get("X-Dounai-Voiceprint-Checked") === "true",
    };
    // The JSON body is fetched separately only when needed; this response is a plain body.
    const event = await response.json();
    showEvent(event, diagnostics);
    if (state.tab === "text") $("#textInput").value = "";
    if (state.tab === "image") clearImage();
    if (state.tab === "audio") clearAudio();
    updateCounter();
    await refreshPrivacyQuiet();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setLoading(false);
  }
}

function showEvent(event, diagnostics = null) {
  state.event = event;
  state.diagnostics = diagnostics;
  $("#eventEmpty").hidden = true;
  $("#eventJson").hidden = false;
  $("#eventJson").textContent = JSON.stringify(event, null, 2);
  $("#copyEventButton").disabled = false;
  renderEventStatus();
  addHistory(event, diagnostics);
}

function renderEventStatus() {
  const event = state.event;
  const diagnostics = state.diagnostics;
  if (!event) return;
  const container = $("#eventStatus");
  container.innerHTML = "";
  addChip(container, MODALITY_LABELS[event.modality] || event.modality, "");
  const asrStatus = event.metadata?.asr_status || diagnostics?.asrStatus;
  if (asrStatus === "completed") addChip(container, "已转写", "is-local");
  if (asrStatus === "empty") addChip(container, "未检测到语音", "");
  if (asrStatus === "unavailable") {
    addChip(container, "转写不可用 · 请勿当作文本", "is-warn");
  }
  $("#asrHint").hidden = !(event.modality === "audio" && asrStatus === "unavailable");
  if (event.metadata?.voiceprint_event === "wakeup") addChip(container, "声纹唤醒", "is-voice");
  if (diagnostics?.provider) addChip(container, diagnostics.provider, "");
  if (diagnostics?.model && diagnostics.model !== diagnostics.provider) {
    addChip(container, diagnostics.model, "");
  }
  if (diagnostics?.degraded && (asrStatus === "completed" || !event.metadata?.asr_status)) {
    // Completed after a fallback is a soft degradation; unavailable already has
    // its own explicit chip above.
    addChip(container, "已降级 · 事件仍可用", "is-warn");
  }
}

function addHistory(event, diagnostics) {
  state.history = state.history.filter((item) => item.event.event_id !== event.event_id);
  state.history.unshift({ event, diagnostics, at: new Date() });
  state.history = state.history.slice(0, 20);
  renderHistory();
}

function renderHistory() {
  const section = $("#history");
  const list = $("#historyList");
  if (!state.history.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  list.innerHTML = "";
  state.history.forEach((item) => {
    const button = document.createElement("button");
    button.className = "history-item";
    button.type = "button";
    const modality = document.createElement("span");
    modality.textContent = MODALITY_LABELS[item.event.modality] || item.event.modality;
    const summary = document.createElement("span");
    summary.textContent = summarizeEvent(item.event);
    const time = document.createElement("time");
    time.textContent = item.at.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    button.append(modality, summary, time);
    button.addEventListener("click", () => {
      state.event = item.event;
      state.diagnostics = item.diagnostics;
      $("#eventEmpty").hidden = true;
      $("#eventJson").hidden = false;
      $("#eventJson").textContent = JSON.stringify(item.event, null, 2);
      $("#copyEventButton").disabled = false;
      renderEventStatus();
    });
    list.appendChild(button);
  });
}

function summarizeEvent(event) {
  if (event.modality === "text") return event.content.slice(0, 80);
  if (event.modality === "audio") {
    if (event.metadata?.asr_status === "completed") return event.metadata.asr_text;
    if (event.metadata?.asr_status === "empty") return "语音（未检测到人声）";
    return "语音（未转写）";
  }
  return "图像引用";
}

function renderSession() {
  $("#sessionLabel").textContent = state.sessionId;
  $("#sessionLabel").title = state.sessionId;
}

function resetSession() {
  state.sessionId = newSessionId();
  sessionStorage.setItem("dounai.session", state.sessionId);
  state.history = [];
  state.event = null;
  state.diagnostics = null;
  $("#eventJson").hidden = true;
  $("#eventEmpty").hidden = false;
  $("#eventStatus").innerHTML = "";
  $("#copyEventButton").disabled = true;
  $("#history").hidden = true;
  $("#asrHint").hidden = true;
  renderSession();
  toast("已开始新会话。", "success");
}

function updateCounter() {
  const value = $("#textInput").value;
  $("#textCounter").textContent = `${value.length} / 20000`;
}

function openPolicy() {
  loadPolicy()
    .then(() => openDialog($("#policyDialog")))
    .catch((error) => toast(error.message, "error"));
}

async function grantConsent() {
  try {
    const policy = await loadPolicy();
    await api("/v1/privacy/consent", {
      method: "POST",
      body: { policy_version: policy.version, granted: true },
    });
    toast("已记录授权并开启声纹功能。", "success");
    await loadPrivacy();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function setVoiceprintEnabled(enabled) {
  try {
    await api("/v1/privacy/settings", {
      method: "PUT",
      body: { voiceprint_enabled: enabled },
    });
    if (enabled && !state.privacy?.consent_granted) {
      toast("功能已开启；授权并录入模板前不会进行声纹判断。");
    } else {
      toast(enabled ? "声纹功能已开启。" : "声纹功能已关闭，不再提取特征。", "success");
    }
    await loadPrivacy();
  } catch (error) {
    $("#voiceprintSwitch").checked = !enabled;
    toast(error.message, "error");
  }
}

async function withdrawConsent() {
  if (!window.confirm("撤回同意会立即删除声纹模板并关闭功能。是否继续？")) return;
  try {
    await api("/v1/privacy/consent", { method: "DELETE" });
    toast("已撤回同意，声纹模板已删除。", "success");
    await loadPrivacy();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function enrollVoiceprint() {
  if (!state.voiceSamples.length) return;
  try {
    const form = new FormData();
    state.voiceSamples.forEach((file) => form.append("files", file, file.name));
    form.append("subject", "default");
    $("#enrollButton").classList.add("is-loading");
    const result = await api("/v1/privacy/voiceprint/enroll", { method: "POST", body: form });
    toast(`声纹模板已保存（${result.sample_count} 段样本，仅本次提取）。`, "success");
    state.voiceSamples = [];
    $("#voiceSamples").value = "";
    $("#voiceSampleCount").textContent = "未选择";
    await loadPrivacy();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    $("#enrollButton").classList.remove("is-loading");
  }
}

async function deleteVoiceprint() {
  if (!window.confirm("删除已保存的声纹模板？录音不会被保留。")) return;
  try {
    await api("/v1/privacy/voiceprint", { method: "DELETE" });
    toast("声纹模板已删除。", "success");
    await loadPrivacy();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function exportData() {
  try {
    const response = await api("/v1/privacy/export", { raw: true });
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename="([^"]+)"/.exec(disposition);
    const name = match ? match[1] : `dounai-input-export-${Date.now()}.zip`;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast("导出已开始。压缩包包含声纹模板，请妥善保管。", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteAllData() {
  try {
    await api("/v1/privacy/data?confirm=true", { method: "DELETE" });
    closeDialog($("#deleteDialog"));
    state.history = [];
    state.event = null;
    state.diagnostics = null;
    $("#eventJson").hidden = true;
    $("#eventEmpty").hidden = false;
    $("#eventStatus").innerHTML = "";
    $("#history").hidden = true;
    $("#copyEventButton").disabled = true;
    $("#asrHint").hidden = true;
    toast("全部个人数据已删除。", "success");
    await loadPrivacy();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function refreshPrivacyQuiet() {
  try {
    const status = await api("/v1/privacy/status");
    state.privacy = status;
    await loadPrivacy();
  } catch (_) {
    /* A privacy refresh must not turn a successful send into an error toast. */
  }
}

async function startRecording() {
  if (state.recording) {
    stopRecording();
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    toast("当前浏览器不支持录音，请上传音频文件。", "error");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const preferred = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"].find((type) =>
      MediaRecorder.isTypeSupported(type)
    );
    const recorder = new MediaRecorder(stream, preferred ? { mimeType: preferred } : undefined);
    const chunks = [];
    state.recordStream = stream;
    state.recording = recorder;
    state.recordStartedAt = Date.now();
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size) chunks.push(event.data);
    });
    recorder.addEventListener("stop", async () => {
      const rawType = recorder.mimeType || "audio/webm";
      const rawBlob = new Blob(chunks, { type: rawType });
      stream.getTracks().forEach((track) => track.stop());
      state.recordStream = null;
      stopRecordingTicker();
      state.recording = null;
      resetRecordButton();
      if (!rawBlob.size) {
        toast("没有录到声音，请再试一次。", "error");
        return;
      }
      try {
        const wav = await convertToWav(rawBlob);
        setAudioFile(new File([wav], `recording-${timestampName()}.wav`, { type: "audio/wav" }), "录音.wav");
      } catch (_) {
        const extension = rawType.includes("ogg") ? "ogg" : "webm";
        setAudioFile(new File([rawBlob], `recording-${timestampName()}.${extension}`, { type: rawType }), "录音");
        toast("浏览器无法转换为 WAV，已按原始格式上传；声纹判断将跳过。");
      }
    });
    recorder.start(250);
    $("#recordButton").classList.add("is-recording");
    $("#recordButton").setAttribute("aria-label", "停止录音");
    $("#recordLabel").textContent = "正在聆听…";
    $("#recordHint").textContent = "再次点击停止 · 最长 60 秒";
    startRecordingTicker();
  } catch (error) {
    toast("无法访问麦克风，请检查浏览器权限。", "error");
  }
}

function stopRecording() {
  if (!state.recording) return;
  try {
    state.recording.stop();
  } catch (_) {
    resetRecordButton();
  }
}

function resetRecordButton() {
  $("#recordButton").classList.remove("is-recording");
  $("#recordButton").setAttribute("aria-label", "开始录音");
  $("#recordLabel").textContent = "按下录音，或上传一段语音";
  $("#recordHint").textContent = "WAV 可获得本地声纹唤醒判断 · 最长 60 秒";
  $("#recordTimer").textContent = "00:00";
}

function startRecordingTicker() {
  stopRecordingTicker();
  state.recordTimer = window.setInterval(() => {
    const seconds = Math.floor((Date.now() - state.recordStartedAt) / 1000);
    $("#recordTimer").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    if (seconds >= 60) stopRecording();
  }, 250);
}

function stopRecordingTicker() {
  if (state.recordTimer) window.clearInterval(state.recordTimer);
  state.recordTimer = null;
}

async function convertToWav(blob) {
  const arrayBuffer = await blob.arrayBuffer();
  const context = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await context.decodeAudioData(arrayBuffer.slice(0));
  const channels = Math.min(2, decoded.numberOfChannels);
  const length = decoded.length;
  const mono = new Float32Array(length);
  for (let channel = 0; channel < channels; channel += 1) {
    const data = decoded.getChannelData(channel);
    for (let index = 0; index < length; index += 1) mono[index] += data[index] / channels;
  }
  await context.close();
  const targetRate = 16000;
  const ratio = decoded.sampleRate / targetRate;
  const outputLength = Math.max(1, Math.floor(length / ratio));
  const resampled = new Float32Array(outputLength);
  for (let index = 0; index < outputLength; index += 1) {
    const source = index * ratio;
    const left = Math.floor(source);
    const right = Math.min(left + 1, length - 1);
    const fraction = source - left;
    resampled[index] = mono[left] * (1 - fraction) + mono[right] * fraction;
  }
  const buffer = new ArrayBuffer(44 + resampled.length * 2);
  const view = new DataView(buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + resampled.length * 2, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, targetRate, true);
  view.setUint32(28, targetRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, resampled.length * 2, true);
  let offset = 44;
  for (let index = 0; index < resampled.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, resampled[index]));
    view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buffer], { type: "audio/wav" });
}

function writeAscii(view, offset, text) {
  for (let index = 0; index < text.length; index += 1) {
    view.setUint8(offset + index, text.charCodeAt(index));
  }
}

function timestampName() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}


const API_CAP_LABELS = { text: "文本", image: "图像", audio: "语音" };
const API_STATUS_LABELS = {
  supported: "通过",
  unsupported: "不支持",
  error: "错误",
  not_tested: "未测试",
};

function currentApiForm() {
  return {
    base_url: $("#apiBaseUrlInput").value.trim(),
    api_key: $("#apiKeyInput").value.trim(),
    allow_private: $("#apiAllowPrivate").checked,
    allow_http: $("#apiAllowHttp").checked,
  };
}

function resetApiDialog() {
  state.apiDiscovered = [];
  state.apiSelectedModels = [];
  state.apiProbes = {};
  $("#apiNameInput").value = "";
  $("#apiKeyInput").value = "";
  $("#apiBaseUrlInput").value = "";
  $("#apiAllowPrivate").checked = false;
  $("#apiAllowHttp").checked = false;
  $("#apiModelPicker").hidden = true;
  $("#apiSelected").hidden = true;
  $("#apiModelList").innerHTML = "";
  $("#apiSelectedList").innerHTML = "";
  $("#apiTestResult").hidden = true;
  $("#apiTestResultBody").innerHTML = "";
  $("#discoverHint").textContent = "";
  $("#probeHint").textContent = "";
  $("#apiManualModelToggle").checked = false;
  $("#apiManualModelRow").hidden = true;
  $("#apiManualModelInput").value = "";
  $("#saveApiConnectionButton").disabled = true;
  $("#probeSelectedButton").disabled = true;
  renderApiPresets();
  applyPreset(0);
}

function renderApiPresets() {
  const select = $("#apiPresetSelect");
  select.innerHTML = "";
  const manual = document.createElement("option");
  manual.value = "-1";
  manual.textContent = "手动输入";
  select.appendChild(manual);
  state.apiPresets.forEach((preset, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = preset.recommended ? `${preset.name}（推荐）` : preset.name;
    select.appendChild(option);
  });
  select.value = state.apiPresets.length ? "0" : "-1";
}

function applyPreset(index) {
  const preset = state.apiPresets[index];
  if (!preset) {
    $("#apiPresetNote").textContent = "手动输入任意 OpenAI 兼容端点。";
    $("#apiAllowPrivate").checked = false;
    $("#apiAllowHttp").checked = false;
    return;
  }
  $("#apiBaseUrlInput").value = preset.base_url;
  $("#apiNameInput").value = preset.name;
  $("#apiAllowPrivate").checked = Boolean(preset.requires_private_permission);
  $("#apiAllowHttp").checked = Boolean(preset.requires_http_permission);
  const models = preset.recommended_models.length
    ? ` 推荐模型：${preset.recommended_models.join("、")}`
    : "";
  $("#apiPresetNote").textContent = `${preset.note}${models}`;
}

async function loadApiConnections() {
  try {
    const [connections, presets] = await Promise.all([
      api("/v1/api-connections"),
      api("/v1/api-presets"),
    ]);
    state.apiConnections = connections.connections || [];
    state.apiPresets = presets.presets || [];
    renderApiConnections();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderApiConnections() {
  const container = $("#apiConnectionList");
  container.innerHTML = "";
  if (!state.apiConnections.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "尚未添加 API 连接。推荐先使用阿里云百炼 Qwen。";
    container.appendChild(empty);
    return;
  }
  state.apiConnections.forEach((connection) => {
    const card = document.createElement("div");
    card.className = "api-connection";
    const head = document.createElement("div");
    head.className = "api-connection-head";
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = connection.name;
    const meta = document.createElement("small");
    meta.textContent = `${connection.base_url} · key ${connection.key_fingerprint}`;
    copy.append(title, meta);
    const actions = document.createElement("div");
    actions.className = "api-connection-actions";
    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "provider-action";
    refresh.textContent = "重新发现";
    refresh.addEventListener("click", () => refreshConnection(connection.id));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "provider-action";
    remove.textContent = "删除";
    remove.addEventListener("click", () => confirmDeleteApiConnection(connection.id));
    actions.append(refresh, remove);
    head.append(copy, actions);
    const tags = document.createElement("div");
    tags.className = "api-model-tags";
    if (!connection.models.length) {
      const none = document.createElement("span");
      none.className = "hint";
      none.textContent = "没有已保存的模型；请删除后重建。";
      tags.appendChild(none);
    }
    connection.models.forEach((model) => {
      const tag = document.createElement("span");
      tag.className = `api-model-tag${model.active ? " is-active" : ""}`;
      tag.textContent = model.model_id;
      const caps = document.createElement("span");
      caps.className = "hint";
      caps.textContent = (model.capabilities || []).map((cap) => API_CAP_LABELS[cap] || cap).join("·");
      tag.appendChild(caps);
      if (model.active) {
        const active = document.createElement("em");
        active.textContent = "使用中";
        active.className = "hint";
        tag.appendChild(active);
      } else {
        const use = document.createElement("button");
        use.type = "button";
        use.textContent = "切换";
        use.addEventListener("click", () => switchProvider(model.provider_id));
        tag.appendChild(use);
      }
      const del = document.createElement("button");
      del.type = "button";
      del.textContent = "移除模型";
      del.addEventListener("click", () => removeApiModel(connection.id, model.provider_id));
      tag.appendChild(del);
      tags.appendChild(tag);
    });
    card.append(head, tags);
    container.appendChild(card);
  });
}

async function refreshConnection(connectionId) {
  try {
    const data = await api(`/v1/api-connections/${connectionId}/refresh`, { method: "POST" });
    const count = (data.models || []).length;
    toast(`已重新发现 ${count} 个模型；已有模型不会自动更新能力，请重新测试。`, "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

function confirmDeleteApiConnection(connectionId) {
  state.apiDeleteId = connectionId;
  openDialog($("#apiDeleteDialog"));
}

async function deleteApiConnection() {
  if (!state.apiDeleteId) return;
  try {
    await api(`/v1/api-connections/${state.apiDeleteId}`, { method: "DELETE" });
    closeDialog($("#apiDeleteDialog"));
    state.apiDeleteId = null;
    toast("API 连接已删除，密钥同时清除。", "success");
    await loadProviders();
    await loadApiConnections();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function removeApiModel(connectionId, providerId) {
  if (!window.confirm("移除这个模型？如果它是当前模型会自动切回离线直通。")) return;
  try {
    await api(`/v1/api-connections/${connectionId}/models/${providerId}`, { method: "DELETE" });
    toast("模型已移除。", "success");
    await loadProviders();
    await loadApiConnections();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function discoverModels() {
  const form = currentApiForm();
  if (!form.base_url) return toast("请填写 Base URL。", "error");
  if (!form.api_key) return toast("请填写 API Key。", "error");
  $("#discoverHint").textContent = "正在调用厂商 /models…";
  try {
    const data = await api("/v1/api-connections/discover", { method: "POST", body: form });
    state.apiDiscovered = data.models || [];
    renderDiscoveredModels();
    $("#apiModelPicker").hidden = false;
    $("#discoverHint").textContent = `发现 ${state.apiDiscovered.length} 个模型；勾选后测试能力。`;
  } catch (error) {
    $("#discoverHint").textContent = "";
    toast(error.message, "error");
  }
}

function renderDiscoveredModels() {
  const container = $("#apiModelList");
  const query = $("#apiModelSearch").value.trim().toLowerCase();
  container.innerHTML = "";
  const models = state.apiDiscovered.filter((model) =>
    !query || model.id.toLowerCase().includes(query)
  );
  if (!models.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "没有匹配的模型；可手动输入 ID。";
    container.appendChild(empty);
    return;
  }
  models.slice(0, 200).forEach((model) => {
    const row = document.createElement("label");
    row.className = "api-model-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = model.id;
    checkbox.checked = state.apiSelectedModels.includes(model.id);
    checkbox.addEventListener("change", () => toggleSelectedModel(model.id, checkbox.checked));
    const name = document.createElement("span");
    name.textContent = model.owned_by ? `${model.id} · ${model.owned_by}` : model.id;
    row.append(checkbox, name);
    container.appendChild(row);
  });
}

function toggleSelectedModel(modelId, selected) {
  if (selected && !state.apiSelectedModels.includes(modelId)) {
    if (state.apiSelectedModels.length >= 20) {
      toast("一次最多选择 20 个模型。", "error");
      return;
    }
    state.apiSelectedModels.push(modelId);
  } else if (!selected) {
    state.apiSelectedModels = state.apiSelectedModels.filter((item) => item !== modelId);
  }
  renderSelectedModels();
}

function addManualModel() {
  const value = $("#apiManualModelInput").value.trim();
  if (!value) return toast("请输入模型 ID。", "error");
  toggleSelectedModel(value, true);
  $("#apiManualModelInput").value = "";
}

function renderSelectedModels() {
  const container = $("#apiSelectedList");
  container.innerHTML = "";
  $("#apiSelected").hidden = state.apiSelectedModels.length === 0;
  $("#apiSelectedCount").textContent = state.apiSelectedModels.length
    ? `${state.apiSelectedModels.length} 个`
    : "";
  state.apiSelectedModels.forEach((modelId) => {
    const item = document.createElement("div");
    item.className = "api-selected-item";
    const name = document.createElement("span");
    name.textContent = modelId;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "text-button";
    remove.textContent = "移除";
    remove.addEventListener("click", () => toggleSelectedModel(modelId, false));
    item.append(name, remove);
    container.appendChild(item);
  });
  const hasResults = state.apiSelectedModels.some((modelId) => state.apiProbes[modelId]);
  $("#saveApiConnectionButton").disabled = !hasResults || Object.values(state.apiProbes).length === 0;
  $("#probeSelectedButton").disabled = !state.apiSelectedModels.length;
}

async function probeSelectedModels() {
  const form = currentApiForm();
  if (!state.apiSelectedModels.length) return;
  const button = $("#probeSelectedButton");
  button.disabled = true;
  $("#probeHint").textContent = "正在逐个测试（会调用厂商，最多每个模型 3 次）…";
  const results = [];
  try {
    for (const modelId of state.apiSelectedModels) {
      try {
        const result = await api("/v1/api-connections/probe", {
          method: "POST",
          body: { ...form, model_id: modelId },
        });
        state.apiProbes[modelId] = result;
        results.push(result);
      } catch (error) {
        state.apiProbes[modelId] = {
          model_id: modelId,
          capabilities: [],
          transcription_protocol: null,
          probes: { text: { status: "error", detail: error.message, latency_ms: 0 } },
        };
        results.push(state.apiProbes[modelId]);
      }
    }
    renderProbeResults(results);
    $("#saveApiConnectionButton").disabled = false;
    $("#probeHint").textContent = "已完成。只有至少一项能力通过的模型才能保存。";
  } finally {
    button.disabled = false;
  }
}

function renderProbeResults(results) {
  const details = $("#apiTestResult");
  const body = $("#apiTestResultBody");
  body.innerHTML = "";
  details.hidden = false;
  details.open = true;
  results.forEach((result) => {
    const card = document.createElement("div");
    card.className = "probe-card";
    const title = document.createElement("strong");
    title.textContent = result.model_id;
    card.appendChild(title);
    const grid = document.createElement("div");
    grid.className = "probe-grid";
    ["text", "image", "audio"].forEach((capability) => {
      const outcome = result.probes?.[capability] || { status: "not_tested", detail: "" };
      const box = document.createElement("div");
      box.className = `probe-box is-${outcome.status}`;
      const label = document.createElement("strong");
      label.textContent = `${API_CAP_LABELS[capability]} · ${API_STATUS_LABELS[outcome.status] || outcome.status}`;
      const detail = document.createElement("span");
      detail.textContent = outcome.detail || "";
      box.append(label, detail);
      grid.appendChild(box);
    });
    card.appendChild(grid);
    if (result.transcription_protocol) {
      const protocol = document.createElement("p");
      protocol.className = "hint";
      protocol.textContent = `语音协议：${result.transcription_protocol}`;
      card.appendChild(protocol);
    }
    body.appendChild(card);
  });
  renderSelectedModels();
}

async function saveApiConnection() {
  const form = currentApiForm();
  const name = $("#apiNameInput").value.trim();
  if (!name) return toast("请填写连接名称。", "error");
  const models = state.apiSelectedModels
    .map((modelId) => state.apiProbes[modelId])
    .filter((result) => result && (result.capabilities || []).length)
    .map((result) => ({
      model_id: result.model_id,
      capabilities: result.capabilities,
      transcription_protocol: result.transcription_protocol,
      probes: result.probes,
    }));
  if (!models.length) {
    return toast("请先测试模型；没有通过任何能力的模型不能保存。", "error");
  }
  try {
    await api("/v1/api-connections", {
      method: "POST",
      body: { ...form, name, models },
    });
    closeDialog($("#apiDialog"));
    toast(`已保存 ${models.length} 个模型。可在上方切换使用。`, "success");
    await loadProviders();
    await loadApiConnections();
  } catch (error) {
    toast(error.message, "error");
  }
}

function bindEvents() {
  $$(".tab").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.tab));
  });
  $("#textInput").addEventListener("input", updateCounter);
  $("#textInput").addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      sendCurrent();
    }
  });
  $("#sendButton").addEventListener("click", sendCurrent);
  $("#newSessionButton").addEventListener("click", resetSession);
  $("#providerSelect").addEventListener("change", (event) => switchProvider(event.target.value));

  $("#imageInput").addEventListener("change", (event) => setImageFile(event.target.files?.[0]));
  $("#clearImage").addEventListener("click", clearImage);
  const drop = $("#imageDrop");
  ["dragenter", "dragover"].forEach((name) => {
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.remove("is-dragging");
    });
  });
  drop.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) setImageFile(file);
  });
  drop.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      $("#imageInput").click();
    }
  });
  document.addEventListener("paste", (event) => {
    const item = Array.from(event.clipboardData?.items || []).find((entry) => entry.type.startsWith("image/"));
    if (item) {
      const file = item.getAsFile();
      if (file) {
        setTab("image");
        setImageFile(file);
      }
    }
  });

  $("#audioInput").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) setAudioFile(file);
  });
  $("#clearAudio").addEventListener("click", clearAudio);
  $("#recordButton").addEventListener("click", startRecording);

  $("#copyEventButton").addEventListener("click", async () => {
    if (!state.event) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(state.event, null, 2));
      toast("事件 JSON 已复制。", "success");
    } catch (_) {
      toast("浏览器不允许复制，请手动选择文本。", "error");
    }
  });
  $("#clearHistory").addEventListener("click", () => {
    state.history = [];
    renderHistory();
  });

  $("#voiceprintSwitch").addEventListener("change", (event) => setVoiceprintEnabled(event.target.checked));
  $("#consentCheck").addEventListener("change", (event) => {
    $("#grantConsentButton").disabled = !event.target.checked || Boolean(state.privacy?.consent_granted);
  });
  $("#grantConsentButton").addEventListener("click", grantConsent);
  $("#withdrawConsentButton").addEventListener("click", withdrawConsent);
  $("#openPolicyButton").addEventListener("click", (event) => {
    event.preventDefault();
    openPolicy();
  });
  $("#policyAcceptButton").addEventListener("click", () => {
    closeDialog($("#policyDialog"));
    $("#consentCheck").focus();
  });
  $("#closePolicyButton").addEventListener("click", () => closeDialog($("#policyDialog")));

  $("#voiceSamples").addEventListener("change", (event) => {
    state.voiceSamples = Array.from(event.target.files || []).filter(
      (file) => file.type === "audio/wav" || file.type === "audio/x-wav" || file.name.toLowerCase().endsWith(".wav")
    );
    if (state.voiceSamples.length !== (event.target.files?.length || 0)) {
      toast("已忽略非 WAV 文件。声纹录入需要 PCM WAV。", "error");
    }
    if (state.voiceSamples.length > 8) {
      state.voiceSamples = state.voiceSamples.slice(0, 8);
      toast("最多使用 8 段样本，已保留前 8 段。");
    }
    $("#voiceSampleCount").textContent = state.voiceSamples.length
      ? `${state.voiceSamples.length} 段已选择`
      : "未选择";
    $("#enrollButton").disabled = !(
      state.voiceSamples.length >= 3 &&
      state.privacy?.consent_granted &&
      state.privacy?.enabled
    );
  });
  $("#enrollButton").addEventListener("click", enrollVoiceprint);
  $("#deleteVoiceprintButton").addEventListener("click", deleteVoiceprint);
  $("#exportButton").addEventListener("click", exportData);

  const deleteDialog = $("#deleteDialog");
  $("#deleteAllButton").addEventListener("click", () => {
    $("#deleteConfirmInput").value = "";
    $("#confirmDeleteButton").disabled = true;
    openDialog(deleteDialog);
  });
  $("#cancelDeleteButton").addEventListener("click", () => closeDialog(deleteDialog));
  $("#deleteConfirmInput").addEventListener("input", (event) => {
    $("#confirmDeleteButton").disabled = event.target.value.trim() !== "删除";
  });
  $("#confirmDeleteButton").addEventListener("click", deleteAllData);

  $("#tokenButton").addEventListener("click", () => {
    $("#tokenInput").value = state.token;
    openDialog($("#tokenDialog"));
  });
  $("#tokenCancel").addEventListener("click", () => closeDialog($("#tokenDialog")));
  $("#tokenForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    state.token = $("#tokenInput").value.trim();
    if (state.token) sessionStorage.setItem("dounai.token", state.token);
    else sessionStorage.removeItem("dounai.token");
    closeDialog($("#tokenDialog"));
    await refreshAll();
  });

  $("#openApiDialogButton").addEventListener("click", () => {
    resetApiDialog();
    openDialog($("#apiDialog"));
  });
  $("#closeApiDialogButton").addEventListener("click", () => closeDialog($("#apiDialog")));
  $("#apiPresetSelect").addEventListener("change", (event) => applyPreset(Number(event.target.value)));
  $("#discoverModelsButton").addEventListener("click", discoverModels);
  $("#apiModelSearch").addEventListener("input", renderDiscoveredModels);
  $("#apiManualModelToggle").addEventListener("change", (event) => {
    $("#apiManualModelRow").hidden = !event.target.checked;
  });
  $("#apiManualModelButton").addEventListener("click", addManualModel);
  $("#probeSelectedButton").addEventListener("click", probeSelectedModels);
  $("#saveApiConnectionButton").addEventListener("click", saveApiConnection);
  $("#cancelApiDeleteButton").addEventListener("click", () => closeDialog($("#apiDeleteDialog")));
  $("#confirmApiDeleteButton").addEventListener("click", deleteApiConnection);
  $("#copyInstallButton").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText("dounai-input models install small");
      toast("安装命令已复制。", "success");
    } catch (_) {
      toast("复制失败，请手动选择命令。", "error");
    }
  });

  $$("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog && dialog.id !== "tokenDialog") closeDialog(dialog);
    });
    if (dialog.id === "apiDialog") {
      dialog.addEventListener("close", () => {
        $("#apiKeyInput").value = "";
      });
    }
  });
}

async function refreshAll() {
  await Promise.allSettled([checkHealth(), loadProviders(), loadPrivacy(), loadModels(), loadApiConnections(), loadPolicy(true)]);
  $("#policyVersion").textContent = state.policy?.version || state.privacy?.policy_version || "1.0";
}

function init() {
  renderSession();
  updateCounter();
  bindEvents();
  refreshAll();
  window.setInterval(checkHealth, 30000);
}

document.addEventListener("DOMContentLoaded", init);
