"use strict";

let aiDirty = false;
let planDirty = false;
let aiSignature = "";
let planSignature = "";
let historySignature = "";
let featureToken = null;
let notificationDirty = false;
let notificationSignature = "";
const dayNames = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
$("plan-days").innerHTML = dayNames.map((name, day) => `<label><input type="checkbox" value="${day}" checked><span>${name}</span></label>`).join("");
$("ai-form").addEventListener("input", () => { aiDirty = true; updateAIFields(); });
$("plan-form").addEventListener("input", () => { planDirty = true; });
$("notification-form").addEventListener("input", () => { notificationDirty = true; updateNotificationFields(); });

function updateAIFields() {
  $("custom-ai-fields").hidden = $("ai-source").value !== "custom";
  $("test-ai").disabled = !connected || pending || state?.status === "running" || $("ai-source").value !== "custom";
  $("test-ai").title = state?.status === "running" ? "可先保存配置；模型列表检查请在当前任务结束后进行。" : "仅请求模型列表，不发送答题内容。";
}

function readAI() {
  const result = {
    enabled: $("ai-enabled").checked,
    no_exam: $("ai-no-exam").checked,
    use_zhidao_ai: $("ai-source").value === "platform",
    use_stream: $("ai-stream").checked,
    max_requests: Number($("ai-max").value),
    openai: { api_base: $("ai-base").value.trim(), model_name: $("ai-model").value.trim() },
    ppt_processing: {
      provide_to_ai: $("ppt-enabled").checked,
      moonShot: { base_url: $("ppt-base").value.trim(), delete_after_convert: $("ppt-delete").checked }
    }
  };
  if ($("ai-key").value.trim() || $("ai-clear-key").checked) result.openai.api_key = $("ai-clear-key").checked ? "" : $("ai-key").value.trim();
  if ($("ppt-key").value.trim() || $("ppt-clear-key").checked) result.ppt_processing.moonShot.api_key = $("ppt-clear-key").checked ? "" : $("ppt-key").value.trim();
  return result;
}

async function saveAI(showToast = true) {
  if (pending) return false;
  pending = true;
  updateButtons();
  try {
    await api({ action: "ai_settings", ai: readAI() });
    aiDirty = false;
    ["ai-key", "ppt-key"].forEach((id) => { $(id).value = ""; });
    ["ai-clear-key", "ppt-clear-key"].forEach((id) => { $(id).checked = false; });
    await fetchState();
    if (showToast) toast(state.status === "running" ? "AI 配置已保存，下次任务生效，当前任务继续运行。" : "AI 配置已保存，没有发送模型请求。");
    return true;
  } catch (error) { toast(error.message, true); return false; }
  finally { pending = false; updateButtons(); renderFeatures(); }
}
$("ai-form").addEventListener("submit", (event) => { event.preventDefault(); saveAI(); });
$("test-ai").addEventListener("click", async () => {
  if (!aiDirty || await saveAI(false)) await begin("test_ai");
});

function selectedPlanCourses() {
  return [...$("plan-courses").querySelectorAll("input:checked")].map((input) => input.value);
}
$("plan-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pending) return;
  pending = true;
  updateButtons();
  try {
    await api({ action: "plan", plan: {
      enabled: $("plan-enabled").checked, time: $("plan-time").value,
      weekdays: [...$("plan-days").querySelectorAll("input:checked")].map((input) => Number(input.value)),
      minutes: Number($("plan-minutes").value), catchup_minutes: Number($("plan-catchup").value), courses: selectedPlanCourses()
    } });
    planDirty = false;
    await fetchState();
    toast(state.plan.enabled ? "定时计划已启用，请保持程序在后台运行。" : "计划已保存，当前为暂停状态。");
  } catch (error) { toast(error.message, true); }
  finally { pending = false; updateButtons(); renderFeatures(); }
});
$("plan-use-selection").addEventListener("click", () => {
  $("plan-courses").querySelectorAll("input").forEach((input) => { input.checked = selected.has(input.value); });
  planDirty = true;
  toast(selected.size ? "已填入概览中勾选的课程，请保存计划。" : "概览中还没有选择课程。");
});
$("pause-plan").addEventListener("click", async () => {
  await action({ action: "pause_plan" }, "定时计划已暂停。");
  planDirty = false;
  planSignature = "";
  renderFeatures();
});

function renderFeatures() {
  if (!state?.ai) return;
  if (featureToken !== state.token) {
    featureToken = state.token;
    aiDirty = planDirty = notificationDirty = false;
    aiSignature = planSignature = historySignature = "";
    notificationSignature = "";
  }
  const signature = JSON.stringify(state.ai);
  if (!aiDirty && signature !== aiSignature) {
    aiSignature = signature;
    const ai = state.ai;
    $("ai-enabled").checked = ai.enabled;
    $("ai-no-exam").checked = ai.no_exam;
    $("ai-source").value = ai.use_zhidao_ai ? "platform" : "custom";
    $("ai-stream").checked = ai.use_stream;
    $("ai-max").value = ai.max_requests;
    $("ai-base").value = ai.openai.api_base;
    $("ai-model").value = ai.openai.model_name;
    $("ai-key-state").textContent = ai.openai.has_api_key ? "密钥已保存在本机，留空保留；需要替换时填写新密钥。" : "尚未保存密钥，留空不会覆盖已有密钥。";
    $("ai-key").placeholder = ai.openai.has_api_key ? "已保存，留空保留" : "填写密钥";
    $("ppt-enabled").checked = ai.ppt_processing.provide_to_ai;
    $("ppt-base").value = ai.ppt_processing.moonShot.base_url;
    $("ppt-delete").checked = ai.ppt_processing.moonShot.delete_after_convert;
    $("ppt-key-state").textContent = ai.ppt_processing.moonShot.has_api_key ? "已保存，留空保留。" : "尚未保存文件处理密钥。";
  }
  const activeLimit = state.status === "running" && state.action === "start" ? (state.active_ai_max_requests ?? state.ai.max_requests) : state.ai.max_requests;
  $("ai-usage").textContent = `本次生成请求 ${state.ai_requests} / ${activeLimit} 次`;
  $("ai-run-note").hidden = state.status !== "running";
  const planSig = JSON.stringify([state.plan, state.courses.map((c) => [keyOf(c), c.name])]);
  if (planSig !== planSignature) {
    const chosen = planDirty ? selectedPlanCourses() : state.plan.courses;
    planSignature = planSig;
    if (!planDirty) {
      $("plan-enabled").checked = state.plan.enabled;
      $("plan-time").value = state.plan.time;
      $("plan-minutes").value = state.plan.minutes;
      $("plan-catchup").value = state.plan.catchup_minutes || 0;
      $("plan-days").querySelectorAll("input").forEach((input) => { input.checked = state.plan.weekdays.includes(Number(input.value)); });
    }
    $("plan-courses").innerHTML = state.courses.map((course) => `<label class="plan-course"><input type="checkbox" value="${escape(keyOf(course))}" ${chosen.includes(keyOf(course)) ? "checked" : ""}><span>${escape(course.name)}</span><span class="badge">${typeLabels[course.kind]}</span></label>`).join("") || '<p class="panel-footnote">先登录并同步课程，也可以在概览中手动添加。</p>';
  }
  $("plan-status").textContent = state.plan.enabled ? "计划已启用" : "计划未启用";
  $("plan-status").className = "badge" + (state.plan.enabled ? " green" : "");
  $("next-run").textContent = state.next_run ? new Date(state.next_run).toLocaleString("zh-CN", { hour12: false }) : "保存并启用计划后显示";
  $("pause-plan").hidden = !state.plan.enabled;
  document.querySelectorAll("[data-feature-button]").forEach((button) => {
    button.disabled = !connected || pending || (state.status === "running" && !button.hasAttribute("data-allow-while-running"));
  });
  updateAIFields();
  renderNotifications();
  renderHistory();
}

function renderHistory() {
  const signature = JSON.stringify(state.history);
  if (historySignature === signature) return;
  historySignature = signature;
  const labels = { start: "学习任务", login: "扫码登录", refresh: "同步课程", test_ai: "模型列表检查", diagnose: "环境检查" };
  const statuses = { done: "已结束", error: "需要查看", stopped: "已停止", limited: "达到时长", skipped: "已跳过", interrupted: "意外中断" };
  $("task-history").innerHTML = state.history.map((row) => {
    const duration = Math.max(0, Math.round((row.ended_at - row.started_at) / 60));
    return `<article class="history-row"><div><strong>${labels[row.action] || "任务"}<span class="badge">${row.origin === "schedule" ? "定时" : "手动"}</span></strong><p>${escape(row.message)}</p><span>${new Date(row.started_at * 1000).toLocaleString("zh-CN", { hour12: false })} · ${duration} 分钟 · AI ${row.ai_requests || 0} 次</span></div><div class="history-actions"><span class="badge ${row.status === "error" ? "red" : ""}">${statuses[row.status] || row.status}</span>${row.id && row.has_log ? `<button class="button secondary small" type="button" data-task-log="${escape(row.id)}">查看日志</button>` : '<span>无详细日志</span>'}</div></article>`;
  }).join("") || '<p class="panel-footnote">还没有运行记录，完成任务后会显示在这里。</p>';
}

$("diagnose").addEventListener("click", () => begin("diagnose"));
$("quit-app").addEventListener("click", async () => {
  try { await api({ action: "quit" }); toast("KillZHS 已退出，当前任务会停止。"); }
  catch (error) { toast(error.message, true); }
});

function updateNotificationFields() {
  $("pushplus-fields").hidden = $("notification-provider").value !== "pushplus";
  $("bark-fields").hidden = $("notification-provider").value !== "bark";
  $("bark-server").disabled = $("notification-provider").value !== "bark";
}

function readNotifications() {
  const value = {
    enabled: $("notification-enabled").checked, provider: $("notification-provider").value,
    events: Object.fromEntries([...document.querySelectorAll("[data-notify-event]")].map((input) => [input.dataset.notifyEvent, input.checked]))
  };
  if (value.provider === "bark") value.bark_server = $("bark-server").value.trim();
  [["pushplus-token", "pushplus-clear", "pushplus_token"], ["bark-key", "bark-clear", "bark_key"]].forEach(([field, clear, key]) => {
    if ($(field).value.trim() || $(clear).checked) value[key] = $(clear).checked ? "" : $(field).value.trim();
  });
  return value;
}

async function saveNotifications(showToast = true) {
  if (pending) return false;
  pending = true;
  renderFeatures();
  try {
    await api({ action: "notification_settings", notifications: readNotifications() });
    notificationDirty = false;
    ["pushplus-token", "bark-key"].forEach((id) => { $(id).value = ""; });
    ["pushplus-clear", "bark-clear"].forEach((id) => { $(id).checked = false; });
    await fetchState();
    if (showToast) toast("通知设置已保存，未发送消息。");
    return true;
  } catch (error) { toast(error.message, true); return false; }
  finally { pending = false; renderFeatures(); }
}

function renderNotifications() {
  if (!state.notifications) return;
  const signature = JSON.stringify(state.notifications);
  if (!notificationDirty && signature !== notificationSignature) {
    notificationSignature = signature;
    const value = state.notifications;
    $("notification-enabled").checked = value.enabled;
    $("notification-provider").value = value.provider;
    $("bark-server").value = value.bark_server;
    $("pushplus-key-state").textContent = value.has_pushplus_token ? "已保存在本机，留空保留。" : "尚未保存 Token。";
    $("bark-key-state").textContent = value.has_bark_key ? "已保存在本机，留空保留。" : "尚未保存设备密钥。";
    document.querySelectorAll("[data-notify-event]").forEach((input) => { input.checked = value.events[input.dataset.notifyEvent]; });
  }
  updateNotificationFields();
}

$("notification-form").addEventListener("submit", (event) => { event.preventDefault(); saveNotifications(); });
$("test-notification").addEventListener("click", async () => {
  if (!notificationDirty || await saveNotifications(false)) {
    await action({ action: "test_notification" }, state.demo ? "已模拟测试，未连接推送服务。" : "测试通知已排队，请查看接收端和运行日志。");
  }
});

let historyText = "";
let historyTask = "";
$("task-history").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-task-log]");
  if (!button) return;
  historyTask = button.dataset.taskLog;
  historyText = "";
  $("history-log-text").textContent = "正在读取…";
  $("download-history").disabled = true;
  $("history-dialog").showModal();
  try {
    const response = await fetch("/api/task-log/" + encodeURIComponent(historyTask), {
      headers: { "X-Local-Token": state.token }, cache: "no-store", signal: AbortSignal.timeout(5000)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "日志读取失败。");
    historyText = result.text;
    $("history-log-text").textContent = historyText || "本次没有详细日志。";
    $("download-history").disabled = !historyText;
  } catch (error) { $("history-log-text").textContent = error.message; }
});
$("close-history").addEventListener("click", () => $("history-dialog").close());
$("download-history").addEventListener("click", () => {
  if (!historyText) return;
  const url = URL.createObjectURL(new Blob(["\ufeff", historyText], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "KillZHS-" + historyTask + ".log";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

window.addEventListener("beforeunload", (event) => {
  if (aiDirty || planDirty || notificationDirty || settingsDirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});

$("course-kind").addEventListener("change", () => {
  const isAI = $("course-kind").value === "ai";
  $("class-id-field").hidden = !isAI;
  $("course-class-id").required = isAI;
});
