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
  const id = crypto.randomUUID ? crypto.randomUUID().replaceAll("-", "") : `${Date.now()}${Math.random()}`;
  return `sess_${id.replaceAll(".", "")}`;
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
      ? "语音会发送到该厂商；密钥只从环境变量读取，不写入浏览器。"
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
  if (event.metadata?.asr_text) addChip(container, "已转写", "is-local");
  if (event.metadata?.voiceprint_event === "wakeup") addChip(container, "声纹唤醒", "is-voice");
  if (diagnostics?.provider) addChip(container, diagnostics.provider, "");
  if (diagnostics?.model && diagnostics.model !== diagnostics.provider) {
    addChip(container, diagnostics.model, "");
  }
  if (diagnostics?.degraded) addChip(container, "已降级 · 事件仍可用", "is-warn");
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
  if (event.modality === "audio") return event.metadata?.asr_text || "语音片段";
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
  });
}

async function refreshAll() {
  await Promise.allSettled([checkHealth(), loadProviders(), loadPrivacy(), loadModels(), loadPolicy(true)]);
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
