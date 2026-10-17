import { getState, setSession } from "./api.js";
export let state = null;
export let connected = false;
export let pending = false;
export const selected = new Set();
const subscribers = new Set();
let fetching = null;
let polling = false;
let timer;
export function subscribe(listener) { subscribers.add(listener); return () => subscribers.delete(listener); }
function notify(kind) { for (const listener of subscribers) listener(kind); }
export function updateButtons() { notify("controls"); }
export function setPending(value) { pending = value; updateButtons(); }
export function replaceSelection(keys) { selected.clear(); for (const key of keys) selected.add(key); }
export async function fetchState() {
  if (fetching) return fetching;
  fetching = (async () => {
    let next;
    try { next = await getState(state); }
    catch { connected = false; setSession(state?.token, false); notify("offline"); return; }
    const restarted = state && state.token !== next.token;
    if (state && !restarted) {
      if (!next.logs_reset) next.logs = [...state.logs, ...next.logs].slice(-600);
      if (!Object.hasOwn(next, "history")) next.history = state.history;
    }
    if (restarted) selected.clear();
    state = next;
    connected = true;
    setSession(next.token, true);
    if (restarted) notify("restart");
    notify("state");
  })();
  try { await fetching; } finally { fetching = null; }
}
export function startPolling() {
  if (polling) return;
  polling = true;
  async function poll() {
    await fetchState();
    if (polling) timer = setTimeout(poll, document.hidden ? 5000 : 1000);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) fetchState(); });
  poll();
}
export function stopPolling() { polling = false; clearTimeout(timer); }
