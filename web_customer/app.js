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

const ADMIN_PORT = 8900;

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
}

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
    $("#cardBrandIcon").textContent = (d.card.brand || "CARD").toUpperCase().slice(0, 4);
    $("#cardLast4").textContent = d.card.last4 || "••••";
    $("#cardExp").textContent = d.card.exp || "--/--";
    $("#cardLabel").textContent = `${d.card.brand || "银行卡"} 已匹配`;
    showHint("");
    setStep(2);
    if (d.used) {
      $("#sessionHint").textContent = "提示：该兑换码已被使用过，如需重复订阅请联系客服";
      $("#sessionHint").className = "muted";
    }
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
    ? `该兑换码仅支持 ${allowed[0].toUpperCase()} 套餐`
    : "";
}

$$(".plan").forEach((b) => {
  b.addEventListener("click", () => {
    state.plan = b.dataset.plan;
    $$(".plan").forEach((x) => x.classList.toggle("selected", x === b));
  });
});
$$(".plan")[0].classList.add("selected");

/* 从浏览器自动获取登录信息 */

async function grabSession() {
  const btn = $("#grabBtn");
  btn.classList.add("loading");
  btn.disabled = true;
  $("#grabHint").textContent = "正在打开 chatgpt.com/api/auth/session …";
  try {
    const d = await api("/api/grab-session", { method: "POST" });
    if (!d.ok) throw new Error(d.error || "获取失败");
    $("#sessionInput").value = d.session;
    $("#sessionHint").textContent = "已获取登录信息 ✓";
    $("#sessionHint").className = "muted";
    $("#grabHint").textContent = "已自动填入，检查无误后点「开始订阅」";
    $("#grabBtn").textContent = "重新获取";
    const ta = $("#sessionInput");
    ta.scrollTop = 0;
    ta.setSelectionRange(0, 0);
  } catch (e) {
    $("#sessionHint").textContent = "";
    $("#grabHint").textContent = `自动获取失败：${e.message}。可点右侧「手动打开该页面」复制内容。`;
  } finally {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
}

$("#grabBtn").addEventListener("click", grabSession);

/* ------------------------------- 第三步 ------------------------------- */
function updateTimeline(job) {
  const order = ["login", "plan", "checkout", "card", "address", "submit"];
  const idx = order.indexOf(job.step);
  $$("#timeline li").forEach((li) => {
    const k = li.dataset.k;
    const i = order.indexOf(k);
    li.className = "";
    if (job.done) {
      if (job.ok) li.className = "done";
      else if (i < idx) li.className = "done";
      else if (i === idx) li.className = "error";
    } else {
      if (i < idx) li.className = "done";
      if (i === idx) li.className = "running";
    }
    li.querySelector("em").textContent = "";
  });
  if (job.step === "done") {
    $$("#timeline li").forEach((li) => (li.className = "done"));
  }
}

async function pollJob() {
  try {
    const job = await api(`/api/job/${state.jobId}`);
    updateTimeline(job);
    $("#progressMsg").textContent = job.message || "处理中…";

    if (job.done) {
      clearInterval(state.timer);
      const box = $("#result");
      box.classList.remove("hidden", "ok", "bad");
      box.classList.add(job.ok ? "ok" : "bad");
      box.innerHTML = job.ok
        ? `<b>订阅已提交</b><br>${esc(job.message || "")}<br>请回到 ChatGPT 页面确认订阅状态；若出现 3D Secure 验证，请在浏览器窗口内完成。`
        : `<b>订阅未完成</b><br>${esc(job.message || "未知错误")}<br>可检查登录信息是否过期后重试。`;
      $("#progressTitle").textContent = job.ok ? "完成" : "失败";
    }
  } catch (e) {
    clearInterval(state.timer);
    $("#progressMsg").textContent = e.message;
  }
}

async function subscribe() {
  const session = $("#sessionInput").value.trim();
  if (!session) {
    $("#sessionHint").textContent = "请先粘贴登录信息，或用上面的按钮一键获取";
    $("#sessionHint").className = "muted";
    return;
  }
  const btn = $("#subscribeBtn");
  btn.classList.add("loading");
  btn.disabled = true;
  try {
    const d = await api("/api/subscribe", {
      method: "POST",
      body: JSON.stringify({ code: state.code, plan: state.plan, session }),
    });
    state.jobId = d.jobId;
    setStep(3);
    $("#progressTitle").textContent = "正在订阅…";
    $("#progressMsg").textContent = "排队中，请保持页面打开";
    state.timer = setInterval(pollJob, 1500);
    pollJob();
  } catch (e) {
    $("#sessionHint").textContent = e.message;
    $("#sessionHint").className = "muted";
    btn.classList.remove("loading");
    btn.disabled = false;
  }
}

$("#subscribeBtn").addEventListener("click", subscribe);

/* ------------------------------- 启动 ------------------------------- */
consumeHash();
