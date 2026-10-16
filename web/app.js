"use strict";

const icons = {
  book: '<path d="M3 4c4-1 6 0 9 2 3-2 5-3 9-2v16c-4-1-6 0-9 2-3-2-5-3-9-2Z"/><path d="M12 6v16"/>',
  dashboard: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  sliders: '<path d="M4 7h7m5 0h4M4 17h3m5 0h8"/><circle cx="13" cy="7" r="2.5"/><circle cx="9" cy="17" r="2.5"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2c-1 .5-1.5 1-1.5 2m0 3h.01"/>',
  "arrow-up-right": '<path d="M6 18 18 6M6 6h12v12"/>',
  "arrow-right": '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18m-13 4h2m4 0h2"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  refresh: '<path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5m-4 8a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  play: '<path d="m8 4 13 8-13 8Z"/>',
  square: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  qr: '<rect x="3" y="3" width="6" height="6" rx="1"/><rect x="15" y="3" width="6" height="6" rx="1"/><rect x="3" y="15" width="6" height="6" rx="1"/><path d="M15 15h3v3h3v3m0-6v1m-6 2v3h3M3 12h3m6-9v3m0 3v3h6m3 0h-1M9 12h1m2 3v3m0 3h.01"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  x: '<path d="m6 6 12 12M6 18 18 6"/>'
};

const $ = (id) => document.getElementById(id);
document.querySelectorAll("[data-icon]").forEach((element) => {
  element.innerHTML = `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[element.dataset.icon] || ""}</svg>`;
});

let state = null;
let connected = false;
let pending = false;
let selected = new Set();
let courseFilter = "all";
let courseSignature = "";
let logsSignature = "";
let hiddenThrough = 0;
let toastTimeout;
let settingsDirty = false;
let savedSettingsSignature = "";
let finishedElapsed = 0;
let previousStarted = null;

const statusLabels = { ready: ["待开始", ""], queued: ["排队中", "amber"], running: ["运行中", "green"],
  done: ["已运行", "blue"], attention: ["需要查看", "red"], limited: ["达到时限", "amber"], stopped: ["已停止", ""] };
const typeLabels = { auto: "自动识别", zhidao: "共享课", hike: "校内课" };
const keyOf = (course) => `${course.kind}:${course.id}`;
const escape = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);

function toast(message, error = false) {
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => { $("toast").hidden = true; }, error ? 6000 : 3200);
}

function route() {
  const view = ["overview", "logs", "settings", "guide"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  document.querySelectorAll(".view").forEach((element) => { element.hidden = element.id !== `view-${view}`; });
  document.querySelectorAll(".nav-link").forEach((element) => {
    element.classList.toggle("active", element.dataset.view === view);
    if (element.dataset.view === view) element.setAttribute("aria-current", "page");
    else element.removeAttribute("aria-current");
    // Icon-only navigation remains labelled on narrow screens.
    element.setAttribute("aria-label", { overview: "学习概览", logs: "运行日志", settings: "偏好设置", guide: "使用指南" }[element.dataset.view]);
  });
  $("breadcrumb-title").textContent = { overview: "学习概览", logs: "运行日志", settings: "偏好设置", guide: "使用指南" }[view];
  document.title = `${$("breadcrumb-title").textContent} · 知序`;
  if (view === "logs") renderLogs(true);
}
window.addEventListener("hashchange", route);
route();
$("guide-button").addEventListener("click", () => { location.hash = "guide"; });

function renderSettings(force = false) {
  if (!state) return;
  const signature = JSON.stringify(state.settings);
  if (!force && (settingsDirty || savedSettingsSignature === signature)) return;
  document.querySelectorAll("[data-setting]").forEach((element) => {
    const value = state.settings[element.dataset.setting];
    if (element.type === "checkbox") element.checked = value;
    else {
      const text = value === null ? "" : String(value);
      if (element.tagName === "SELECT" && ![...element.options].some((option) => option.value === text)) {
        const option = document.createElement("option");
        option.value = text;
        option.textContent = `${text} ×`;
        element.add(option);
      }
      element.value = text;
    }
  });
  savedSettingsSignature = signature;
}

document.querySelectorAll("[data-setting]").forEach((input) => {
  input.addEventListener("input", () => {
    settingsDirty = true;
    // Keep the two settings views consistent without re-rendering the form.
    document.querySelectorAll(`[data-setting="${input.dataset.setting}"]`).forEach((other) => {
      if (other === input) return;
      if (other.type === "checkbox") other.checked = input.checked;
      else {
        if (other.tagName === "SELECT" && ![...other.options].some((option) => option.value === input.value)) {
          const option = document.createElement("option");
          option.value = input.value;
          option.textContent = input.value ? `${input.value} ×` : "平台默认";
          other.add(option);
        }
        other.value = input.value;
      }
    });
  });
});

function readSettings() {
  return {
    speed: $("setting-speed").value.trim() === "" ? null : Number($("setting-speed").value),
    limit: Number($("setting-limit").value),
    threshold: Number($("setting-threshold").value),
    remember: $("setting-remember").checked
  };
}

async function api(data) {
  if (!state || !connected) throw new Error("本地服务尚未连接，请稍后再试。");
  const response = await fetch("/api/action", {
    method: "POST", headers: { "Content-Type": "application/json", "X-Local-Token": state.token },
    body: JSON.stringify(data), signal: AbortSignal.timeout(12000)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失败，请重试。");
  return result;
}

async function action(data, success) {
  if (pending) return;
  pending = true;
  updateButtons();
  try {
    await api(data);
    if (success) toast(success);
    await fetchState();
  } catch (error) {
    toast(error.message || "操作失败，请检查本地服务。", true);
  } finally {
    pending = false;
    updateButtons();
  }
}

async function saveSettings(event) {
  event?.preventDefault();
  if (pending) return;
  pending = true;
  updateButtons();
  try {
    await api({ action: "settings", settings: readSettings() });
    settingsDirty = false;
    await fetchState();
    renderSettings(true);
    toast("偏好已保存，将用于下一次任务。");
  } catch (error) { toast(error.message, true); }
  finally { pending = false; updateButtons(); }
}
$("quick-settings-form").addEventListener("submit", saveSettings);
$("settings-form").addEventListener("submit", saveSettings);
$("reset-settings").addEventListener("click", () => {
  if (!state) return;
  const previous = state.settings;
  state.settings = { speed: null, limit: 0, threshold: 0.91, remember: true };
  renderSettings(true);
  state.settings = previous;
  settingsDirty = true;
  toast("已填入默认值，点击保存后生效。");
});

async function begin(actionName) {
  if (pending) return;
  // Save visible edits first so a changed speed is never silently ignored.
  if (settingsDirty) {
    try {
      await api({ action: "settings", settings: readSettings() });
      settingsDirty = false;
    } catch (error) { toast(error.message, true); return; }
  }
  await action({ action: actionName, ...(actionName === "start" ? { courses: [...selected] } : {}) });
}
$("login").addEventListener("click", () => begin("login"));
$("empty-login").addEventListener("click", () => begin(state?.logged_in || state?.saved_session ? "refresh" : "login"));
$("refresh").addEventListener("click", () => begin("refresh"));
$("start").addEventListener("click", () => begin("start"));
$("stop").addEventListener("click", () => action({ action: "stop" }));

function updateButtons() {
  const busy = state?.status === "running";
  const unavailable = !connected || pending;
  ["login", "refresh", "empty-login", "add-course", "reset-settings"].forEach((id) => { $(id).disabled = unavailable || busy; });
  $("start").disabled = unavailable || busy || !state?.logged_in || !selected.size;
  $("start").title = !state?.logged_in ? "请先登录并同步课程" : !selected.size ? "请先勾选至少一门课程" : "开始所选课程的学习任务";
  $("stop").hidden = !busy;
  $("stop").disabled = unavailable;
  document.querySelectorAll("[data-setting], #settings-form button[type=submit], #quick-settings-form button[type=submit]").forEach((element) => { element.disabled = unavailable || busy; });
}

function visibleCourses() {
  const query = $("search").value.trim().toLocaleLowerCase();
  return (state?.courses || []).filter((course) => (courseFilter === "all" || course.kind === courseFilter)
    && (!query || `${course.name} ${course.id}`.toLocaleLowerCase().includes(query)));
}

function renderCourses(force = false) {
  if (!state) return;
  const known = new Set(state.courses.map(keyOf));
  selected = new Set([...selected].filter((key) => known.has(key)));
  const visible = visibleCourses();
  const signature = JSON.stringify([state.courses, [...selected], courseFilter, $("search").value]);
  if (force || courseSignature !== signature) {
    courseSignature = signature;
    $("course-list").innerHTML = visible.map((course, index) => {
      const key = keyOf(course);
      const [label, color] = statusLabels[course.status] || statusLabels.ready;
      return `<div class="course-row ${selected.has(key) ? "selected" : ""} ${course.status === "running" ? "running" : ""}">
        <label class="checkbox-wrap"><input class="course-check" type="checkbox" data-key="${escape(key)}" id="course-${index}" aria-label="选择${escape(course.name)}" ${selected.has(key) ? "checked" : ""}></label>
        <label class="course-info" for="course-${index}"><span class="course-cover cover-${index % 4}" aria-hidden="true">${escape(course.name[0] || "课")}</span><span class="course-text"><span class="course-title" title="${escape(course.name)}">${escape(course.name)}</span><span class="course-meta">${state.demo ? "演示课程" : "ID"} · ${escape(course.id)}</span></span></label>
        <span class="course-type">${typeLabels[course.kind] || "自动识别"}</span><span class="badge course-status ${color}">${label}</span></div>`;
    }).join("");
    $("course-empty").hidden = Boolean(visible.length);
    if (!visible.length) {
      const filtered = Boolean(state.courses.length);
      $("course-empty").classList.toggle("filtered", filtered);
      $("course-empty").querySelector("h3").textContent = filtered ? "没有找到这门课程" : "从第一门课程开始";
      $("course-empty").querySelector("p").innerHTML = filtered ? "换个关键词，或切换课程类型试试。" : "扫码登录后点击「同步课程」，<br>就能在这里选择你的课程。";
      $("empty-login").hidden = filtered;
    }
  }
  $("select-all").checked = visible.length > 0 && visible.every((course) => selected.has(keyOf(course)));
  $("select-all").indeterminate = !$("select-all").checked && visible.some((course) => selected.has(keyOf(course)));
  $("select-all").disabled = !visible.length;
  $("stat-total").replaceChildren(document.createTextNode(String(state.courses.length)), unit("门"));
  $("stat-selected").replaceChildren(document.createTextNode(String(selected.size)), unit("门"));
  $("selected-count").textContent = selected.size;
  $("course-count").textContent = state.courses.length;
  $("clear-selection").disabled = !selected.size;
  updateButtons();
}

function unit(text) { const span = document.createElement("span"); span.textContent = text; return span; }
$("course-list").addEventListener("change", (event) => {
  if (!event.target.matches(".course-check")) return;
  const key = event.target.dataset.key;
  if (event.target.checked) selected.add(key); else selected.delete(key);
  // Preserve keyboard focus instead of replacing the active row.
  event.target.closest(".course-row").classList.toggle("selected", event.target.checked);
  courseSignature = JSON.stringify([state.courses, [...selected], courseFilter, $("search").value]);
  renderCourses();
});
$("select-all").addEventListener("change", (event) => {
  visibleCourses().forEach((course) => { if (event.target.checked) selected.add(keyOf(course)); else selected.delete(keyOf(course)); });
  renderCourses();
});
$("clear-selection").addEventListener("click", () => { selected.clear(); renderCourses(); });
$("search").addEventListener("input", () => renderCourses());
document.querySelectorAll("[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    courseFilter = button.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((item) => {
      item.classList.toggle("selected", item === button);
      item.setAttribute("aria-pressed", String(item === button));
    });
    renderCourses();
  });
});

function clock(value) {
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function logMarkup(row) {
  return `<div class="log-line ${row.level === "error" ? "error" : row.level === "warning" ? "warning" : ""}"><time datetime="${escape(row.time)}">${clock(row.time)}</time><span>${escape(row.message)}</span></div>`;
}
function renderLogs(force = false) {
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
  const content = "知序 · 本地工作台运行日志" + (state.demo ? "（演示）" : "") + "\n\n"
    + state.logs.map((row) => `${new Date(row.time).toLocaleString("zh-CN")} [${row.level}] ${row.message}`).join("\n");
  const url = URL.createObjectURL(new Blob(["\ufeff", content], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `学习日志-${new Date().toLocaleDateString("sv-SE")}.txt`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

$("add-course").addEventListener("click", () => { $("add-error").textContent = ""; $("course-dialog").showModal(); });
$("close-dialog").addEventListener("click", () => $("course-dialog").close());
$("course-dialog").addEventListener("click", (event) => { if (event.target === $("course-dialog")) { const bounds = event.target.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.target.close(); } });
$("add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pending) return;
  let id = $("course-url").value.trim();
  let kind = $("course-kind").value;
  try {
    if (/^https?:\/\//i.test(id)) {
      const url = new URL(id);
      // Some course pages keep their parameters in a hash route.
      const params = new URLSearchParams(url.search);
      const hashParams = new URLSearchParams(url.hash.split("?")[1] || "");
      const shared = params.get("recruitAndCourseId") || hashParams.get("recruitAndCourseId");
      id = shared || params.get("courseId") || hashParams.get("courseId") || "";
      if (!id) throw new Error("地址中没有找到 courseId 或 recruitAndCourseId。");
      if (kind === "auto") kind = shared ? "zhidao" : "hike";
    }
    pending = true;
    $("add-form").querySelector("[type=submit]").disabled = true;
    await api({ action: "add", course: { id, name: $("course-name").value.trim(), kind } });
    $("course-dialog").close();
    $("add-form").reset();
    courseFilter = "all";
    document.querySelectorAll("[data-filter]").forEach((button) => { const all = button.dataset.filter === "all"; button.classList.toggle("selected", all); button.setAttribute("aria-pressed", String(all)); });
    $("search").value = "";
    await fetchState();
    toast("课程已添加。");
  } catch (error) { $("add-error").textContent = error.message; }
  finally { pending = false; $("add-form").querySelector("[type=submit]").disabled = false; updateButtons(); }
});

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
  $("dependency-banner").textContent = `当前 Python 环境缺少 ${state.missing_dependencies.join("、")}。请使用项目内的「启动学习工作台.bat」启动。`;
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
  const taskLabel = { idle: "等待开始", running: state.action === "start" ? "正在学习" : state.action === "login" ? "正在登录" : "正在同步", done: "任务已结束", error: "需要查看", stopped: "已停止" }[state.status] || "等待开始";
  $("task-status").textContent = taskLabel;
  $("task-status").className = "badge" + (busy ? " green" : state.status === "error" ? " red" : "");
  $("task-message").textContent = state.message;
  $("stat-task-label").textContent = state.started_at ? taskLabel : "尚未开始任务";
  $("live-indicator").classList.toggle("active", busy);
  $("stat-done").replaceChildren(document.createTextNode(String(state.courses.filter((course) => course.status === "done").length)), unit("门"));
  $("progress-section").hidden = !state.progress || !busy || state.action !== "start";
  if (state.progress) {
    $("progress-title").textContent = state.progress.title;
    $("progress-number").textContent = `${state.progress.value}%`;
    $("video-progress").value = state.progress.value;
  }
  renderSettings();
  renderCourses();
  renderLogs();
  renderTimer();
}

let fetching = null;
async function fetchState() {
  if (fetching) return fetching;
  fetching = (async () => {
    try {
      const response = await fetch("/api/state", { cache: "no-store", signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error("服务不可用");
      const next = await response.json();
      if (state && state.token !== next.token) {
        selected.clear();
        settingsDirty = false;
        savedSettingsSignature = "";
        hiddenThrough = 0;
        courseSignature = "";
        logsSignature = "";
        toast("本地服务已重新启动。");
      }
      state = next;
      connected = true;
      render();
    } catch {
      connected = false;
      $("connection-label").textContent = "等待本地服务连接";
      $("connection-dot").classList.add("offline");
      $("offline-banner").hidden = false;
      updateButtons();
    }
  })();
  try { await fetching; } finally { fetching = null; }
}
async function poll() { await fetchState(); setTimeout(poll, 1000); }
$("today").textContent = new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" });
setInterval(renderTimer, 1000);
updateButtons();
poll();
