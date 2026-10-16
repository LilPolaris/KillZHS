"use strict";

let aiDirty = false;
let planDirty = false;
let aiSignature = "";
let planSignature = "";
let historySignature = "";
let featureToken = null;
const dayNames = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
$("plan-days").innerHTML = dayNames.map((name, day) => `<label><input type="checkbox" value="${day}" checked><span>${name}</span></label>`).join("");
$("ai-form").addEventListener("input", () => { aiDirty = true; updateAIFields(); });
$("plan-form").addEventListener("input", () => { planDirty = true; });

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
      minutes: Number($("plan-minutes").value), courses: selectedPlanCourses()
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
    aiDirty = planDirty = false;
    aiSignature = planSignature = historySignature = "";
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
  renderHistory();
}

function renderHistory() {
  const signature = JSON.stringify(state.history);
  if (historySignature === signature) return;
  historySignature = signature;
  const labels = { start: "学习任务", login: "扫码登录", refresh: "同步课程", test_ai: "模型列表检查", diagnose: "环境检查" };
  const statuses = { done: "已结束", error: "需要查看", stopped: "已停止", limited: "达到时长", skipped: "已跳过" };
  $("task-history").innerHTML = state.history.map((row) => {
    const duration = Math.max(0, Math.round((row.ended_at - row.started_at) / 60));
    return `<article class="history-row"><div><strong>${labels[row.action] || "任务"}<span class="badge">${row.origin === "schedule" ? "定时" : "手动"}</span></strong><p>${escape(row.message)}</p><span>${new Date(row.started_at * 1000).toLocaleString("zh-CN", { hour12: false })} · ${duration} 分钟 · AI ${row.ai_requests || 0} 次</span></div><span class="badge ${row.status === "error" ? "red" : ""}">${statuses[row.status] || row.status}</span></article>`;
  }).join("") || '<p class="panel-footnote">还没有运行记录，完成任务后会显示在这里。</p>';
}

$("diagnose").addEventListener("click", () => begin("diagnose"));
$("quit-app").addEventListener("click", async () => {
  try { await api({ action: "quit" }); toast("KillZHS 已退出，当前任务会停止。"); }
  catch (error) { toast(error.message, true); }
});

$("course-kind").addEventListener("change", () => {
  const isAI = $("course-kind").value === "ai";
  $("class-id-field").hidden = !isAI;
  $("course-class-id").required = isAI;
});
