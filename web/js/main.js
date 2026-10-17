import { $, toast, unit } from "./dom.js";
import { state, connected, pending } from "./store.js";
import { api } from "./api.js";
import { initializeIcons } from "./dom.js";
import { subscribe, startPolling } from "./store.js";
import { hasUnsavedForms, resetForms } from "./forms.js";
import { renderSettings, begin } from "./settings.js";
import { renderCourses, renderVerification, renderOverviewButtons, resetOverview } from "./overview.js";
import { renderLogs, resetLogs } from "./logs.js";
import { renderAI } from "./ai.js";
import { renderPlan } from "./plan.js";
import { renderNotifications } from "./notifications.js";
import { renderHistory, resetHistory } from "./history.js";
import { renderAssessments } from "./assessments.js";
initializeIcons();
let finishedElapsed = 0;
let previousStarted = null;
function renderFeatures() {
  document.querySelectorAll("[data-feature-button]").forEach((button) => {
    button.disabled = !connected || pending || (state?.status === "running" && !button.hasAttribute("data-allow-while-running"));
  });
  renderAI(); renderPlan(); renderNotifications(); renderHistory();
}
function renderControls() { renderOverviewButtons(); renderFeatures(); renderAssessments(); }
subscribe((kind) => {
  if (kind === "restart") { resetForms(); resetOverview(); resetLogs(); resetHistory(); toast("本地服务已重新启动。"); }
  else if (kind === "state") render();
  else if (kind === "controls") renderControls();
  else if (kind === "offline") {
    $("connection-label").textContent = "等待本地服务连接";
    $("connection-dot").classList.add("offline");
    $("offline-banner").hidden = false;
    renderControls();
  }
});
window.addEventListener("beforeunload", (event) => {
  if (hasUnsavedForms()) { event.preventDefault(); event.returnValue = ""; }
});
const viewTitles = { overview: "学习概览", logs: "运行日志", settings: "偏好设置", guide: "使用指南", ai: "AI 配置", plan: "定时计划", notifications: "消息通知", assessments: "测验与考试", official: "官网与验证" };
function route() {
  const view = Object.hasOwn(viewTitles, location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  document.querySelectorAll(".view").forEach((element) => { element.hidden = element.id !== `view-${view}`; });
  document.querySelectorAll(".nav-link").forEach((element) => {
    element.classList.toggle("active", element.dataset.view === view);
    if (element.dataset.view === view) element.setAttribute("aria-current", "page");
    else element.removeAttribute("aria-current");
    // Icon-only navigation remains labelled on narrow screens.
    element.setAttribute("aria-label", viewTitles[element.dataset.view]);
  });
  $("breadcrumb-title").textContent = viewTitles[view];
  document.title = `${$("breadcrumb-title").textContent} · KillZHS`;
  if (view === "logs") renderLogs(true);
}
window.addEventListener("hashchange", route);
route();
$("guide-button").addEventListener("click", () => { location.hash = "guide"; });

function renderTimer() {
  if (!state) return;
  if (previousStarted !== state.started_at) { previousStarted = state.started_at; finishedElapsed = 0; }
  if (state.started_at) finishedElapsed = Math.max(0, Math.floor((state.ended_at || Date.now() / 1000) - state.started_at));
  const hours = Math.floor(finishedElapsed / 3600);
  const minutes = Math.floor(finishedElapsed / 60) % 60;
  const seconds = finishedElapsed % 60;
  const label = hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  $("stat-time").replaceChildren(document.createTextNode(label), unit("时长"));
}

function render() {
  if (!state) return;
  const busy = state.status === "running";
  $("demo-badge").hidden = !state.demo;
  $("connection-label").textContent = state.demo ? "本地演示已连接" : "本地服务已连接";
  $("connection-dot").classList.remove("offline");
  $("offline-banner").hidden = true;
  $("dependency-banner").hidden = !state.missing_dependencies.length || state.demo;
  $("dependency-banner").textContent = `当前 Python 环境缺少 ${state.missing_dependencies.join("、")}。请使用项目内的「启动KillZHS.bat」启动。`;
  $("app-version").textContent = "KillZHS · " + state.version + (state.verify_probe_available || state.verify_probe_enabled ? " · 验证实验" : "");
  $("auth-badge").textContent = state.logged_in ? (state.demo ? "演示账号" : "已连接") : "未登录";
  $("auth-badge").className = "badge" + (state.logged_in ? " green" : "");
  const hasQR = Boolean(state.qr && busy && state.action === "login");
  $("qr-image").hidden = !hasQR;
  $("login-visual").hidden = hasQR;
  if (hasQR && $("qr-image").getAttribute("src") !== state.qr) $("qr-image").src = state.qr;
  if (!hasQR) $("qr-image").removeAttribute("src");
  $("login-title").textContent = hasQR ? "打开知到 App，扫一扫" : state.logged_in ? "账号已连接，准备就绪" : "连接你的知到账号";
  $("login-description").innerHTML = hasQR ? "扫码后请在手机上确认<br>二维码过期后会自动刷新" : state.demo ? "当前所有课程和运行均为本地模拟<br>不会连接真实账号" : state.logged_in ? "选择想学的课程，就可以开始了<br>需要换账号时可重新扫码" : "使用知到 App 扫码登录<br>已有登录状态可直接尝试同步";
  $("login-button-label").textContent = busy && state.action === "login" ? "等待扫码确认…" : state.logged_in ? "重新扫码登录" : "扫码登录";
  const taskLabel = state.stop_requested ? "正在收尾" : state.verify_probe_enabled && state.verification_probe?.awaiting_confirmation ? "等待验证（实验）" : state.verification && !busy ? "等待验证" : { idle: "等待开始", running: state.action === "start" ? "正在学习" : state.action === "login" ? "正在登录" : "正在同步", done: "任务已结束", error: "需要查看", stopped: "已停止", limited: "达到时长" }[state.status] || "等待开始";
  $("task-status").textContent = taskLabel;
  $("task-status").className = "badge" + (busy ? " green" : state.status === "error" ? " red" : "");
  $("task-message").textContent = state.message;
  $("stat-task-label").textContent = state.started_at || state.verification ? taskLabel : "尚未开始任务";
  $("live-indicator").classList.toggle("active", busy);
  $("stat-done").replaceChildren(document.createTextNode(String(state.courses.filter((course) => course.status === "done").length)), unit("门"));
  $("progress-section").hidden = !state.progress || !busy || state.action !== "start";
  if (state.progress) {
    $("progress-title").textContent = state.progress.title;
    $("progress-number").textContent = state.progress.phase === "waiting" ? `剩余 ${state.progress.remaining} 秒` : `${state.progress.value}%`;
    $("video-progress").hidden = state.progress.phase === "waiting";
    $("video-progress").value = state.progress.value;
  }
  renderVerification();
  if (state.verification && (!busy || state.verification_probe?.awaiting_confirmation)) {
    $("login-title").textContent = "账号需要完成验证";
    $("login-description").textContent = state.verification_probe?.awaiting_confirmation
      ? "请在官方页面完成验证，再点击“我已完成网页验证”。"
      : "请使用页面上方的验证入口，完成后返回工作台重新开始。";
  }
  renderSettings();
  renderCourses();
  renderLogs();
  renderTimer();
  renderFeatures();
  renderAssessments();
}

$("diagnose").addEventListener("click", () => begin("diagnose"));
$("quit-app").addEventListener("click", async () => {
  try { await api("quit", {}); toast("KillZHS 已退出，当前任务会停止。"); }
  catch (error) { toast(error.message, true); }
});

$("course-kind").addEventListener("change", () => {
  const isAI = $("course-kind").value === "ai";
  $("class-id-field").hidden = !isAI;
  $("course-class-id").required = isAI;
});
$("today").textContent = new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" });
setInterval(renderTimer, 1000);
renderControls();
startPolling();
