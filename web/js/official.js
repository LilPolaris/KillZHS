import { $, toast } from "./dom.js";
import { state } from "./store.js";
export function officialPage(url, title) {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:" || !["studyvideoh5.zhihuishu.com", "onlineexamh5new.zhihuishu.com", "www.zhihuishu.com"].includes(parsed.hostname) || parsed.username || parsed.password || parsed.port) {
    toast("官方入口不正确，请重新同步清单。", true); return;
  }
  if (state?.demo) {
    toast("演示模式：展示内嵌区域，未连接官网。");
    $("official-frame").removeAttribute("src");
  } else {
    $("official-frame").src = parsed.href;
  }
  $("official-frame").hidden = false;
  $("official-title").textContent = title;
  $("official-description").textContent = "请在下方官网操作。加载完成不代表登录或验证已成功。";
  $("official-external").href = parsed.href;
  $("official-external").hidden = Boolean(state?.demo);
  $("official-close").hidden = false;
  location.hash = "official";
}

$("embed-verification").addEventListener("click", () => {
  if (state?.verification) officialPage(state.verification.url, "当前课程验证");
});
export function closeOfficial() {
  $("official-frame").removeAttribute("src");
  $("official-frame").hidden = true;
  $("official-close").hidden = true;
  $("official-external").hidden = true;
}
$("official-close").addEventListener("click", closeOfficial);
$("official-return").addEventListener("click", () => {
  closeOfficial();
  location.hash = state?.verification ? "overview" : "assessments";
});
