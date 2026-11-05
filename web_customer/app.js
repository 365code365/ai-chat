/* ============================================================
   客户兑换页逻辑
   ============================================================ */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  code: "",
  card: null,
  plan: "plus",
  jobId: null,
  timer: null,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { data = { error: `HTTP ${res.status}` }; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function setStep(n) {
  $$(".step").forEach((s) => s.classList.toggle("hidden", s.id !== `step${n}`));
  $$(".pstep").forEach((p) => {
    const i = Number(p.dataset.s);
    p.classList.toggle("active", i === n);
    p.classList.toggle("done", i < n);
  });
  $("#stepCount").textContent = `第 ${n} 步，共 3 步`;
  window.scrollTo({ top: 0 });
}

// 卡组织标识（与管理端一致）
const BRAND_LOGO = {
  visa: '<span class="logo-visa">VISA</span>',
  mastercard: '<svg viewBox="0 0 26 16" width="26" height="16" aria-label="Mastercard">'
    + '<circle cx="9" cy="8" r="7" fill="#EB001B"/><circle cx="17" cy="8" r="7" fill="#F79E1B"/>'
    + '<path d="M13 2.26A7 7 0 0 1 13 13.74A7 7 0 0 1 13 2.26Z" fill="#FF5F00"/></svg>',
  amex: '<span class="logo-amex">AMEX</span>',
  jcb: '<span class="logo-text" style="color:#0b6b3a">JCB</span>',
  unionpay: '<span class="logo-text" style="color:#c81f26">银联</span>',
  discover: '<span class="logo-text" style="color:#e35205">DISC</span>',
  diners: '<span class="logo-text" style="color:#0b3d91">DINERS</span>',
  card: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#8e8e98" stroke-width="1.7">'
    + '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h18"/></svg>',
};

/* ------------------------------- 第一步 ------------------------------- */
const codeInput = $("#codeInput");
codeInput.addEventListener("input", () => {
  const raw = codeInput.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 10);
  codeInput.value = raw.replace(/(.{4})(.{4})(.{2})/, "$1-$2-$3").replace(/-$/, "");
});

async function verify() {
  const code = codeInput.value.trim().toUpperCase();
  if (code.replace(/-/g, "").length < 6) {
    return showHint("兑换码不完整", "bad");
  }
  const btn = $("#verifyBtn");
  btn.classList.add("loading");
  btn.disabled = true;
  try {
    const d = await api("/api/verify", {
      method: "POST",
      body: JSON.stringify({ code }),
    });
    if (!d.ok) return showHint(d.error || "兑换码无效", "bad");
    state.code = code;
    state.card = d.card;
    applyPlans(d.plans || ["plus", "pro"]);
    const key = d.card.brandKey || "card";
    $("#cardBrandIcon").innerHTML = BRAND_LOGO[key] || BRAND_LOGO.card;
    $("#cardLast4").textContent = d.card.last4 || "••••";
    $("#cardExp").textContent = d.card.exp || "--/--";
    $("#cardLabel").textContent = d.card.brand || "银行卡";
    showHint("");
    setStep(2);
  } catch (e) {
    showHint(e.message, "bad");
  } finally {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
}

function showHint(msg, kind = "") {
  const el = $("#codeHint");
  el.textContent = msg;
  el.className = `hint ${kind}`;
}

$("#verifyBtn").addEventListener("click", verify);
codeInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") verify();
});

/* ------------------------------- 第二步 ------------------------------- */
function applyPlans(allowed) {
  const all = $$(".plan");
  all.forEach((b) => b.classList.toggle("hidden", !allowed.includes(b.dataset.plan)));
  const first = all.find((b) => !b.classList.contains("hidden"));
  state.plan = first ? first.dataset.plan : "plus";
  all.forEach((b) => b.classList.toggle("selected", b === first));
  $("#planNote").textContent = allowed.length === 1
    ? `这个兑换码只能开通 ${allowed[0] === "pro" ? "Pro" : "Plus"}。`
    : "";
}

$$(".plan").forEach((b) => {
  b.addEventListener("click", () => {
    state.plan = b.dataset.plan;
    $$(".plan").forEach((x) => x.classList.toggle("selected", x === b));
  });
});
$$(".plan")[0].classList.add("selected");

/* ---------------- 登录信息：客户在自己浏览器里打开页面，复制粘贴 ---------------- */
state.session = null;   // 校验通过后的 { raw, email, expires }

// 解析粘贴内容。复制整页时可能带上「美观输出」之类的页面文字，所以只取最外层的 {...}。
function parseSession(text) {
  const t = (text || "").trim();
  if (!t) return { empty: true };
  const start = t.indexOf("{");
  const end = t.lastIndexOf("}");
  if (start < 0 || end <= start) {
    return { error: "没识别到登录信息，请确认复制的是「登录信息页面」的全部内容" };
  }
  let obj;
  try {
    obj = JSON.parse(t.slice(start, end + 1));
  } catch (e) {
    return { error: "内容不完整，可能没复制全。请回到那个页面按 Ctrl/⌘+A 全选后重新复制" };
  }
  if (!obj || typeof obj !== "object" || !Object.keys(obj).length) {
    return { error: "粘贴的是 {}：这个浏览器还没登录 ChatGPT。请先完成第 1 步登录，再重新打开页面" };
  }
  if (!obj.accessToken) {
    return { error: "内容里没有登录凭证，请确认打开的是 chatgpt.com/api/auth/session" };
  }
  if (!obj.sessionToken) {
    return { error: "登录信息不完整（缺少 sessionToken），请复制该页面的全部内容" };
  }
  const exp = obj.expires ? Date.parse(obj.expires) : NaN;
  if (!Number.isNaN(exp) && exp < Date.now()) {
    return { error: "登录信息已过期，请刷新那个页面后重新复制" };
  }
  return {
    raw: JSON.stringify(obj),
    email: obj.user?.email || "",
    expires: Number.isNaN(exp) ? "" : new Date(exp).toLocaleDateString("zh-CN"),
  };
}

function markGuide(n, done) {
  $(`#gs${n}`)?.classList.toggle("done", !!done);
}

function validateSession() {
  const r = parseSession($("#sessionInput").value);
  const box = $("#sessionStatus");
  state.session = r.raw ? r : null;
  $("#subscribeBtn").disabled = !state.session;
  markGuide(3, !!state.session);
  if (r.empty) {
    box.className = "session-status";
    box.innerHTML = "";
  } else if (r.error) {
    box.className = "session-status bad";
    box.textContent = r.error;
  } else {
    markGuide(1, true);
    markGuide(2, true);
    box.className = "session-status ok";
    box.innerHTML = `<b>${esc(r.email || "已识别登录信息")}</b>`
      + `<span>请确认这是要开通的账号${r.expires ? `，登录有效期至 ${esc(r.expires)}` : ""}。</span>`;
  }
  $("#sessionHint").textContent = "";
}

$("#sessionInput").addEventListener("input", validateSession);
$("#openSessionBtn").addEventListener("click", () => markGuide(2, true));
$("#pasteBtn").addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    $("#sessionInput").value = text;
    validateSession();
  } catch (e) {
    // 部分浏览器不允许网页直接读剪贴板
    const box = $("#sessionStatus");
    box.className = "session-status bad";
    box.textContent = "浏览器不允许直接读取剪贴板，请点击下方输入框，按 Ctrl/⌘ + V 粘贴";
    $("#sessionInput").focus();
  }
});

/* ------------------------------- 第三步 ------------------------------- */
// 把内部的技术报错翻译成客户能看懂、且不泄露实现细节的话术。
function friendlyError(raw) {
  const m = String(raw || "").toLowerCase();
  if (/过期|expired|登录信息|session|sessiontoken|401|未登录/.test(m)) {
    return "登录信息可能已失效，请返回重新获取后再试。";
  }
  if (/当前套餐|已经是|already/.test(m)) {
    return "这个账号可能已经是该套餐了，请确认后再试。";
  }
  // 其余一律给通用话术，不暴露内部步骤 / 报错
  return "开通没有成功，可以返回重试，或联系客服处理。";
}

function finishJob(ok, raw) {
  clearInterval(state.timer);
  state.timer = null;
  $("#processing").classList.add("hidden");
  const box = $("#result");
  box.classList.remove("hidden", "ok", "bad");
  box.classList.add(ok ? "ok" : "bad");
  box.innerHTML = ok
    ? "<b>开通成功</b><br>订阅已提交，请回到 ChatGPT 查看。如果银行要求验证，请按提示完成。你的兑换码已使用。"
    : `<b>暂时没有开通</b><br>${esc(friendlyError(raw))}<br>兑换码没有被使用，不会浪费。`;
  $("#retryBtn").classList.toggle("hidden", ok);
}

async function pollJob() {
  try {
    const job = await api(`/api/job/${state.jobId}`);
    // 只显示一个「处理中」，不暴露内部步骤
    if (job.done) {
      finishJob(!!job.ok, job.message);
    }
  } catch (e) {
    // 轮询偶发失败不打断，继续等；真正结果以 job.done 为准
  }
}

$("#retryBtn").addEventListener("click", () => {
  $("#result").classList.add("hidden");
  $("#retryBtn").classList.add("hidden");
  $("#processing").classList.remove("hidden");
  setStep(2);
});

async function subscribe() {
  validateSession();
  if (!state.session) {
    $("#sessionHint").textContent = "请先按上面 3 步获取并粘贴登录信息";
    $("#sessionHint").className = "hint bad";
    return;
  }
  const session = state.session.raw;
  const btn = $("#subscribeBtn");
  btn.classList.add("loading");
  btn.disabled = true;
  try {
    const d = await api("/api/subscribe", {
      method: "POST",
      body: JSON.stringify({ code: state.code, plan: state.plan, session }),
    });
    state.jobId = d.jobId;
    $("#result").classList.add("hidden");
    $("#retryBtn").classList.add("hidden");
    $("#processing").classList.remove("hidden");
    setStep(3);
    state.timer = setInterval(pollJob, 1500);
    pollJob();
  } catch (e) {
    // 提交时的校验错误（兑换码无效 / 登录信息过期等）本身就是友好话术，直接显示
    $("#sessionHint").textContent = e.message;
    $("#sessionHint").className = "hint bad";
    btn.classList.remove("loading");
    btn.disabled = false;
  }
}

$("#subscribeBtn").addEventListener("click", subscribe);
