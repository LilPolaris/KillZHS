import { $, escape } from "./dom.js";
import { state } from "./store.js";
let logsSignature = "";
let hiddenThrough = 0;
export function resetLogs() { logsSignature = ""; hiddenThrough = 0; }
function clock(value) {
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function logMarkup(row) {
  return `<div class="log-line ${row.level === "error" ? "error" : row.level === "warning" ? "warning" : ""}"><time datetime="${escape(row.time)}">${clock(row.time)}</time><span>${escape(row.message)}</span></div>`;
}
export function renderLogs(force = false) {
  if (!state) return;
  const signature = JSON.stringify([state.logs.at(-1)?.id, hiddenThrough, $("log-filter").value]);
  if (!force && logsSignature === signature) return;
  logsSignature = signature;
  const logs = state.logs.filter((row) => row.id > hiddenThrough);
  const filter = $("log-filter").value;
  const filtered = logs.filter((row) => filter === "all" || row.level === "error" || (filter === "warning" && row.level === "warning"));
  const container = $("full-logs");
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 60;
  container.innerHTML = filtered.map(logMarkup).join("") || '<span class="no-logs">这里暂时没有日志。</span>';
  if (nearBottom) container.scrollTop = container.scrollHeight;
  $("mini-logs").innerHTML = state.logs.slice(-3).map(logMarkup).join("");
  $("log-count").textContent = state.logs.length > 99 ? "99+" : state.logs.length;
}
$("log-filter").addEventListener("change", () => renderLogs(true));
$("clear-logs").addEventListener("click", () => { hiddenThrough = state?.logs.at(-1)?.id || 0; renderLogs(true); });
$("export-logs").addEventListener("click", () => {
  if (!state) return;
  const content = "KillZHS · 本地工作台运行日志" + (state.demo ? "（演示）" : "") + "\n\n"
    + state.logs.map((row) => `${new Date(row.time).toLocaleString("zh-CN")} [${row.level}] ${row.message}`).join("\n");
  const url = URL.createObjectURL(new Blob(["\ufeff", content], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `学习日志-${new Date().toLocaleDateString("sv-SE")}.txt`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
