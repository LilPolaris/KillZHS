import { $, toast } from "./dom.js";
import { state, connected, pending, setPending, fetchState, updateButtons } from "./store.js";
import { api } from "./api.js";
import { begin } from "./settings.js";
import { createFormState } from "./forms.js";
const aiForm = createFormState();
$("ai-form").addEventListener("input", () => { aiForm.dirty = true; updateAIFields(); });
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
  setPending(true);
  updateButtons();
  try {
    await api("ai_settings", { ai: readAI() });
    aiForm.dirty = false;
    ["ai-key", "ppt-key"].forEach((id) => { $(id).value = ""; });
    ["ai-clear-key", "ppt-clear-key"].forEach((id) => { $(id).checked = false; });
    await fetchState();
    if (showToast) toast(state.status === "running" ? "AI 配置已保存，下次任务生效，当前任务继续运行。" : "AI 配置已保存，没有发送模型请求。");
    return true;
  } catch (error) { toast(error.message, true); return false; }
  finally { setPending(false); updateButtons(); renderAI(); }
}
$("ai-form").addEventListener("submit", (event) => { event.preventDefault(); saveAI(); });
$("test-ai").addEventListener("click", async () => {
  if (!aiForm.dirty || await saveAI(false)) await begin("test_ai");
});


export function renderAI() {
  if (!state?.ai) return;
  if (aiForm.accepts(state.ai)) {
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
  const activeLimit = state.status === "running" && ["start", "assessment_run"].includes(state.action) ? (state.active_ai_max_requests ?? state.ai.max_requests) : state.ai.max_requests;
  $("ai-usage").textContent = `本次生成请求 ${state.ai_requests} / ${activeLimit} 次`;
  $("ai-run-note").hidden = state.status !== "running";
  updateAIFields();
}
