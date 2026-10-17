import { api } from "./api.js";
import { pending, setPending, updateButtons, fetchState } from "./store.js";
import { toast } from "./dom.js";
export async function action(name, params = {}, success) {
  if (pending) return;
  setPending(true);
  updateButtons();
  try {
    await api(name, params);
    if (success) toast(success);
    await fetchState();
  } catch (error) {
    toast(error.message || "操作失败，请检查本地服务。", true);
  } finally {
    setPending(false);
    updateButtons();
  }
}
