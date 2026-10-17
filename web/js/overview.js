import { $, escape, toast, keyOf, typeLabels, unit } from "./dom.js";
import { state, connected, pending, selected, setPending, replaceSelection, fetchState, updateButtons } from "./store.js";
import { api } from "./api.js";
import { action } from "./actions.js";
import { begin } from "./settings.js";
const statusLabels = { ready: ["待开始", ""], queued: ["排队中", "amber"], running: ["运行中", "green"],
  done: ["已运行", "blue"], attention: ["需要查看", "red"], limited: ["达到时限", "amber"], stopped: ["已停止", ""] };
let courseFilter = "all";
let courseSignature = "";
let verificationIdentity = null;
export function resetOverview() { courseSignature = ""; verificationIdentity = null; }
export function renderOverviewButtons() {
  const busy = state?.status === "running";
  const unavailable = !connected || pending;
  ["login", "refresh", "empty-login", "add-course", "reset-settings"].forEach((id) => { $(id).disabled = unavailable || busy; });
  $("start").disabled = unavailable || busy || !state?.logged_in || !selected.size;
  $("start").title = !state?.logged_in ? "请先登录并同步课程" : !selected.size ? "请先勾选至少一门课程" : "开始所选课程的学习任务";
  $("stop").hidden = !busy;
  $("stop").disabled = unavailable || Boolean(state?.stop_requested);
  renderVerification();
  document.querySelectorAll("[data-setting], #settings-form button[type=submit], #quick-settings-form button[type=submit]").forEach((element) => { element.disabled = unavailable || busy; });
}

$("login").addEventListener("click", () => begin("login"));
$("empty-login").addEventListener("click", () => begin(state?.logged_in || state?.saved_session ? "refresh" : "login"));
$("refresh").addEventListener("click", () => begin("refresh"));
$("start").addEventListener("click", () => begin("start"));
$("stop").addEventListener("click", () => action("stop", {}));
$("open-verification").addEventListener("click", (event) => {
  if (!connected || pending || state?.stop_requested || (state?.status === "running" && !state.verification_probe?.awaiting_confirmation) || !state?.verification || state.demo) {
    event.preventDefault();
    if (state?.demo) toast("演示模式：这里只展示验证入口，不打开真实课程。");
  }
});
$("confirm-verification").addEventListener("click", () => {
  const probe = state?.verification_probe;
  if (!probe?.awaiting_confirmation) return;
  action("confirm_verification", { task_id: probe.task_id, probe_id: probe.probe_id },
    "确认已发送，正在尝试原地恢复，不会重新启动任务。");
});
$("restart-after-verification").addEventListener("click", () => {
  if (state?.verification?.action?.startsWith("assessment_")) {
    location.hash = "assessments";
    toast("请先重新同步测验清单，核对状态后再选择答题。");
    return;
  }
  const known = new Set((state?.courses || []).map(keyOf));
  const keys = (state?.verification?.course_keys || []).filter((key) => known.has(key));
  if (!keys.length) { toast("课程清单已变化，请先同步并重新选择课程。", true); return; }
  replaceSelection(keys);
  renderCourses(true);
  begin("start");
});

export function renderVerification() {
  const verification = state?.verification;
  $("verification-panel").hidden = !verification;
  const waiting = Boolean(state?.verify_probe_enabled && state.status === "running" && state.verification_probe?.awaiting_confirmation);
  $("confirm-verification").hidden = !waiting;
  $("confirm-verification").disabled = !waiting || !connected || pending || Boolean(state?.stop_requested || state?.verification_probe?.confirmation_sent);
  $("restart-after-verification").hidden = waiting;
  if (!verification) { verificationIdentity = null; return; }
  const identity = JSON.stringify([verification.task_id, verification.course_keys]);
  if (identity !== verificationIdentity) {
    verificationIdentity = identity;
    if (!selected.size) replaceSelection(verification.course_keys);
  }
  const disabled = !connected || pending || Boolean(state.stop_requested) || (state.status === "running" && !waiting);
  $("embed-verification").disabled = disabled;
  const known = new Set(state.courses.map(keyOf));
  const canRestart = verification.course_keys.some((key) => known.has(key));
  $("verification-course").textContent = verification.course_name ? "当前课程：" + verification.course_name : "本次任务已停止，请先在官方页面处理验证。";
  $("open-verification").href = verification.url;
  $("open-verification").setAttribute("aria-disabled", String(disabled));
  $("open-verification").textContent = verification.direct ? "打开当前课程验证 ↗" : "打开知到官方页面 ↗";
  $("restart-after-verification").disabled = disabled || state.status === "running" || !canRestart || !(state.logged_in || state.saved_session);
  $("restart-after-verification").textContent = verification.action?.startsWith("assessment_") ? "返回测验清单核对" : "验证后重新开始";
  $("restart-after-verification").title = canRestart ? "确认已在官方页面完成验证后，按当前设置重新开始本次未完成的课程。" : "课程清单已变化，请先同步并重新选择。";
  $("verification-title").textContent = waiting ? "等待验证（实验）" : "需要完成平台验证";
  $("verification-note").textContent = waiting
    ? "当前任务保持等待；确认后仅尝试原地恢复一次，不重新登录。等待期间时限仍在计时，可随时停止。"
    : "重新开始会按当前设置计时；已完成的视频由脚本自动跳过。工作台尚不能自动确认官方验证是否完成。";
}

function visibleCourses() {
  const query = $("search").value.trim().toLocaleLowerCase();
  return (state?.courses || []).filter((course) => (courseFilter === "all" || course.kind === courseFilter)
    && (!query || `${course.name} ${course.id}`.toLocaleLowerCase().includes(query)));
}

export function renderCourses(force = false) {
  if (!state) return;
  const known = new Set(state.courses.map(keyOf));
  replaceSelection([...selected].filter((key) => known.has(key)));
  const visible = visibleCourses();
  const signature = JSON.stringify([state.courses, [...selected], courseFilter, $("search").value, state.verification?.course_key]);
  if (force || courseSignature !== signature) {
    courseSignature = signature;
    $("course-list").innerHTML = visible.map((course, index) => {
      const key = keyOf(course);
      const [label, color] = state.verification?.course_key === key ? ["需要验证", "red"] : statusLabels[course.status] || statusLabels.ready;
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

$("course-list").addEventListener("change", (event) => {
  if (!event.target.matches(".course-check")) return;
  const key = event.target.dataset.key;
  if (event.target.checked) selected.add(key); else selected.delete(key);
  // Preserve keyboard focus instead of replacing the active row.
  event.target.closest(".course-row").classList.toggle("selected", event.target.checked);
  courseSignature = JSON.stringify([state.courses, [...selected], courseFilter, $("search").value, state.verification?.course_key]);
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
      const classId = params.get("classId") || hashParams.get("classId");
      if (classId) { kind = "ai"; $("course-class-id").value = classId; }
      else if (kind === "auto") kind = shared ? "zhidao" : "hike";
    }
    setPending(true);
    $("add-form").querySelector("[type=submit]").disabled = true;
    await api("add", { course: { id, name: $("course-name").value.trim(), kind, class_id: $("course-class-id").value.trim() } });
    $("course-dialog").close();
    $("add-form").reset();
    $("class-id-field").hidden = true;
    $("course-class-id").required = false;
    courseFilter = "all";
    document.querySelectorAll("[data-filter]").forEach((button) => { const all = button.dataset.filter === "all"; button.classList.toggle("selected", all); button.setAttribute("aria-pressed", String(all)); });
    $("search").value = "";
    await fetchState();
    toast("课程已添加。");
  } catch (error) { $("add-error").textContent = error.message; }
  finally { setPending(false); $("add-form").querySelector("[type=submit]").disabled = false; updateButtons(); }
});
