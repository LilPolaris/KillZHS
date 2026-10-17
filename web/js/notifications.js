import { $, toast } from "./dom.js";
import { state, pending, setPending, fetchState } from "./store.js";
import { api } from "./api.js";
import { action } from "./actions.js";
import { createFormState } from "./forms.js";
const notificationForm = createFormState();
$("notification-form").addEventListener("input", () => { notificationForm.dirty = true; updateNotificationFields(); });
function updateNotificationFields() {
  $("pushplus-fields").hidden = $("notification-provider").value !== "pushplus";
  $("bark-fields").hidden = $("notification-provider").value !== "bark";
  $("bark-server").disabled = $("notification-provider").value !== "bark";
}

function readNotifications() {
  const value = {
    enabled: $("notification-enabled").checked, provider: $("notification-provider").value,
    events: Object.fromEntries([...document.querySelectorAll("[data-notify-event]")].map((input) => [input.dataset.notifyEvent, input.checked]))
  };
  if (value.provider === "bark") value.bark_server = $("bark-server").value.trim();
  [["pushplus-token", "pushplus-clear", "pushplus_token"], ["bark-key", "bark-clear", "bark_key"]].forEach(([field, clear, key]) => {
    if ($(field).value.trim() || $(clear).checked) value[key] = $(clear).checked ? "" : $(field).value.trim();
  });
  return value;
}

async function saveNotifications(showToast = true) {
  if (pending) return false;
  setPending(true);
  renderNotifications();
  try {
    await api("notification_settings", { notifications: readNotifications() });
    notificationForm.dirty = false;
    ["pushplus-token", "bark-key"].forEach((id) => { $(id).value = ""; });
    ["pushplus-clear", "bark-clear"].forEach((id) => { $(id).checked = false; });
    await fetchState();
    if (showToast) toast("通知设置已保存，未发送消息。");
    return true;
  } catch (error) { toast(error.message, true); return false; }
  finally { setPending(false); renderNotifications(); }
}

export function renderNotifications() {
  if (!state?.notifications) return;
  if (notificationForm.accepts(state.notifications)) {
    const value = state.notifications;
    $("notification-enabled").checked = value.enabled;
    $("notification-provider").value = value.provider;
    $("bark-server").value = value.bark_server;
    $("pushplus-key-state").textContent = value.has_pushplus_token ? "已保存在本机，留空保留。" : "尚未保存 Token。";
    $("bark-key-state").textContent = value.has_bark_key ? "已保存在本机，留空保留。" : "尚未保存设备密钥。";
    document.querySelectorAll("[data-notify-event]").forEach((input) => { input.checked = value.events[input.dataset.notifyEvent]; });
  }
  updateNotificationFields();
}

$("notification-form").addEventListener("submit", (event) => { event.preventDefault(); saveNotifications(); });
$("test-notification").addEventListener("click", async () => {
  if (!notificationForm.dirty || await saveNotifications(false)) {
    await action("test_notification", {}, state.demo ? "已模拟测试，未连接推送服务。" : "测试通知已排队，请查看接收端和运行日志。");
  }
});
