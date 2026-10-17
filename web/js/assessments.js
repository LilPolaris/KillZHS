import { $, escape, keyOf } from "./dom.js";
import { state, connected, pending } from "./store.js";
import { action } from "./actions.js";
import { officialPage, closeOfficial } from "./official.js";
let assessmentSignature = "";
let assessmentToken = null;
const assessmentSelection = new Set();
const assessmentKey = (row) => `${row.course_key}:${row.kind}:${row.id}:${row.exam_id}`;

$("assessment-course").addEventListener("change", () => {
  assessmentSelection.clear(); assessmentSignature = ""; renderAssessments();
});
$("assessment-scan").addEventListener("click", () => action("assessment_scan", { courses: [$("assessment-course").value] }));
$("assessment-run").addEventListener("click", () => action("assessment_run", { courses: [$("assessment-course").value], assessments: [...assessmentSelection] }));
$("assessment-list").addEventListener("change", (event) => {
  const input = event.target.closest("[data-assessment]");
  if (!input) return;
  input.checked ? assessmentSelection.add(input.value) : assessmentSelection.delete(input.value);
  renderAssessmentButtons();
});
$("assessment-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-open-assessment]");
  if (!button) return;
  const row = (state.assessments || []).find((entry) => assessmentKey(entry) === button.dataset.openAssessment);
  if (row) officialPage(row.url, row.name);
});

function renderAssessmentButtons() {
  const busy = !connected || pending || state?.status === "running";
  $("assessment-course").disabled = busy;
  $("assessment-scan").disabled = busy || !$("assessment-course").value;
  $("assessment-run").disabled = busy || !assessmentSelection.size || !state?.ai.enabled;
  $("assessment-count").textContent = `已选择 ${assessmentSelection.size} 份`;
  $("assessment-list").querySelectorAll("[data-assessment]").forEach((input) => { input.disabled = busy || input.dataset.unavailable === "true"; });
}

export function renderAssessments() {
  if (!state) return;
  if (assessmentToken !== state.token) {
    assessmentToken = state.token; assessmentSelection.clear(); assessmentSignature = ""; closeOfficial();
  }
  const select = $("assessment-course");
  const courses = state.courses.filter((course) => course.kind === "zhidao");
  const wanted = select.value;
  const choices = courses.map((course) => `<option value="${escape(keyOf(course))}">${escape(course.name)}</option>`).join("");
  if (select.innerHTML !== choices) {
    select.innerHTML = choices;
    if (courses.some((course) => keyOf(course) === wanted)) select.value = wanted;
  }
  const rows = (state.assessments || []).filter((row) => row.course_key === select.value);
  const results = state.assessment_results || {};
  const signature = JSON.stringify([rows, results, select.value]);
  if (assessmentSignature !== signature) {
    assessmentSignature = signature;
    const available = new Set(rows.filter((row) => [1, 2, 5].includes(row.state) && results[assessmentKey(row)]?.state !== "submitted").map(assessmentKey));
    for (const key of assessmentSelection) if (!available.has(key)) assessmentSelection.delete(key);
    $("assessment-list").innerHTML = rows.map((row) => {
      const key = assessmentKey(row);
      const result = results[key];
      const disabled = !available.has(key);
      return `<article class="assessment-row"><label><input type="checkbox" data-assessment data-unavailable="${disabled}" value="${escape(key)}" ${assessmentSelection.has(key) ? "checked" : ""}><span><strong>${escape(row.name)}</strong><small>${row.kind === "chapter" ? "章节测验" : "在线考试"}${row.end ? " · 截止 " + escape(row.end) : ""}</small></span></label><div class="assessment-row-actions"><span class="badge ${result?.state === "submitted" ? "green" : ""}">${escape(result?.message || (disabled ? "已提交 / 不可作答" : "待作答"))}</span><button class="text-button" type="button" data-open-assessment="${escape(key)}">官方入口 ↗</button></div></article>`;
    }).join("") || '<p class="panel-footnote">尚无测验清单。先同步；若课程没有开放测验，清单会保持为空。</p>';
  }
  if ((state.action || "").startsWith("assessment_")) $("assessment-status").textContent = state.message;
  renderAssessmentButtons();
}
