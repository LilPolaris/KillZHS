import { $, escape } from "./dom.js";
import { state } from "./store.js";
import { taskLog } from "./api.js";
let historySignature = "";
export function resetHistory() { historySignature = ""; }
export function renderHistory() {
  if (!state) return;
  const signature = JSON.stringify(state.history);
  if (historySignature === signature) return;
  historySignature = signature;
  const labels = { start: "学习任务", login: "扫码登录", refresh: "同步课程", test_ai: "模型列表检查", diagnose: "环境检查", assessment_scan: "同步测验与考试", assessment_run: "自动答题并交卷" };
  const statuses = { done: "已结束", error: "需要查看", stopped: "已停止", limited: "达到时长", skipped: "已跳过", interrupted: "意外中断" };
  $("task-history").innerHTML = state.history.map((row) => {
    const duration = Math.max(0, Math.round((row.ended_at - row.started_at) / 60));
    return `<article class="history-row"><div><strong>${labels[row.action] || "任务"}<span class="badge">${row.origin === "schedule" ? "定时" : "手动"}</span></strong><p>${escape(row.message)}</p><span>${new Date(row.started_at * 1000).toLocaleString("zh-CN", { hour12: false })} · ${duration} 分钟 · AI ${row.ai_requests || 0} 次</span></div><div class="history-actions"><span class="badge ${row.status === "error" ? "red" : ""}">${statuses[row.status] || row.status}</span>${row.id && row.has_log ? `<button class="button secondary small" type="button" data-task-log="${escape(row.id)}">查看日志</button>` : '<span>无详细日志</span>'}</div></article>`;
  }).join("") || '<p class="panel-footnote">还没有运行记录，完成任务后会显示在这里。</p>';
}

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
    const result = await taskLog(historyTask);
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
