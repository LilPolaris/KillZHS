// Transport only: no action discriminator is ever sent in the JSON body.
let token = null;
let online = false;
export function setSession(value, connected) { token = value; online = connected; }
async function json(response, fallback) {
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || fallback);
  return result;
}
export async function api(name, params = {}) {
  if (!token || !online) throw new Error("本地服务尚未连接，请稍后再试。");
  return json(await fetch("/api/actions/" + encodeURIComponent(name), {
    method: "POST", headers: { "Content-Type": "application/json", "X-Local-Token": token },
    body: JSON.stringify(params), signal: AbortSignal.timeout(12000)
  }), "操作失败，请重试。");
}
export async function getState(previous) {
  const query = previous ? "?" + new URLSearchParams({ since: previous.log_cursor, history_revision: previous.history_revision }) : "";
  return json(await fetch("/api/state" + query, { cache: "no-store", signal: AbortSignal.timeout(5000) }), "服务不可用");
}
export async function taskLog(id) {
  return json(await fetch("/api/task-log/" + encodeURIComponent(id), {
    headers: { "X-Local-Token": token }, cache: "no-store", signal: AbortSignal.timeout(5000)
  }), "日志读取失败。");
}
