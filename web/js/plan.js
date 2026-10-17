import { $, escape, toast, keyOf, typeLabels } from "./dom.js";
import { state, pending, selected, setPending, fetchState, updateButtons } from "./store.js";
import { api } from "./api.js";
import { action } from "./actions.js";
import { createFormState } from "./forms.js";
const planForm = createFormState();
const dayNames = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
$("plan-days").innerHTML = dayNames.map((name, day) => `<label><input type="checkbox" value="${day}" checked><span>${name}</span></label>`).join("");
$("plan-form").addEventListener("input", () => { planForm.dirty = true; });
function selectedPlanCourses() {
  return [...$("plan-courses").querySelectorAll("input:checked")].map((input) => input.value);
}
$("plan-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pending) return;
  setPending(true);
  updateButtons();
  try {
    await api("plan", { plan: {
      enabled: $("plan-enabled").checked, time: $("plan-time").value,
      weekdays: [...$("plan-days").querySelectorAll("input:checked")].map((input) => Number(input.value)),
      minutes: Number($("plan-minutes").value), catchup_minutes: Number($("plan-catchup").value), courses: selectedPlanCourses()
    } });
    planForm.dirty = false;
    await fetchState();
    toast(state.plan.enabled ? "定时计划已启用，请保持程序在后台运行。" : "计划已保存，当前为暂停状态。");
  } catch (error) { toast(error.message, true); }
  finally { setPending(false); updateButtons(); renderPlan(); }
});
$("plan-use-selection").addEventListener("click", () => {
  $("plan-courses").querySelectorAll("input").forEach((input) => { input.checked = selected.has(input.value); });
  planForm.dirty = true;
  toast(selected.size ? "已填入概览中勾选的课程，请保存计划。" : "概览中还没有选择课程。");
});
$("pause-plan").addEventListener("click", async () => {
  await action("pause_plan", {}, "定时计划已暂停。");
  planForm.dirty = false;
  planForm.signature = "";
  renderPlan();
});


export function renderPlan() {
  if (!state?.plan) return;
  const planSig = JSON.stringify([state.plan, state.courses.map((c) => [keyOf(c), c.name])]);
  if (planSig !== planForm.signature) {
    const chosen = planForm.dirty ? selectedPlanCourses() : state.plan.courses;
    planForm.signature = planSig;
    if (!planForm.dirty) {
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
}
