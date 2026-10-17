import { $, toast } from "./dom.js";
import { state, pending, selected, setPending, fetchState, updateButtons } from "./store.js";
import { api } from "./api.js";
import { action } from "./actions.js";
import { createFormState } from "./forms.js";
const settingsForm = createFormState();
export function renderSettings(force = false) {
  if (!state) return;
  if (!settingsForm.accepts(state.settings, force)) return;
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
}

document.querySelectorAll("[data-setting]").forEach((input) => {
  input.addEventListener("input", () => {
    settingsForm.dirty = true;
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
    remember: $("setting-remember").checked, budget_minutes: Number($("setting-budget").value)
  };
}

async function saveSettings(event) {
  event?.preventDefault();
  if (pending) return;
  setPending(true);
  updateButtons();
  try {
    await api("settings", { settings: readSettings() });
    settingsForm.dirty = false;
    await fetchState();
    renderSettings(true);
    toast("偏好已保存，将用于下一次任务。");
  } catch (error) { toast(error.message, true); }
  finally { setPending(false); updateButtons(); }
}
$("quick-settings-form").addEventListener("submit", saveSettings);
$("settings-form").addEventListener("submit", saveSettings);
$("reset-settings").addEventListener("click", () => {
  if (!state) return;
  const previous = state.settings;
  state.settings = structuredClone(state.defaults);
  renderSettings(true);
  state.settings = previous;
  settingsForm.dirty = true;
  toast("已填入默认值，点击保存后生效。");
});

export async function begin(actionName) {
  if (pending) return;
  // Save visible edits first so a changed speed is never silently ignored.
  if (settingsForm.dirty) {
    try {
      await api("settings", { settings: readSettings() });
      settingsForm.dirty = false;
    } catch (error) { toast(error.message, true); return; }
  }
  await action(actionName, actionName === "start" ? { courses: [...selected] } : {});
}
