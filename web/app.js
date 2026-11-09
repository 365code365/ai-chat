/* ============================================================
   TokenDesk — 前端逻辑
   ============================================================ */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  accounts: [],
  cards: [],
  status: null,
  view: "accounts",
  keyword: "",
  acc: null,
  plan: null,
  cardId: null,
};

/* ------------------------------- 工具 ------------------------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401) {
    showLogin();
    throw new Error("未登录");
  }
  let data = {};
  try { data = await res.json(); } catch (e) { data = { error: `HTTP ${res.status}` }; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s ease, transform .25s ease";
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    setTimeout(() => el.remove(), 260);
  }, 3600);
}

function busy(el, on) {
  if (!el) return;
  el.classList.toggle("loading", on);
  el.disabled = on;
}

function initials(a) {
  const s = (a.name || a.email || "?").trim();
  return /[\u4e00-\u9fa5]/.test(s) ? s.slice(0, 2) : s.slice(0, 2).toUpperCase();
}

function remain(exp) {
  if (!exp) return "—";
  const ms = exp * 1000 - Date.now();
  if (ms <= 0) return "已过期";
  const d = Math.floor(ms / 86400000);
  const h = Math.floor((ms % 86400000) / 3600000);
  return d > 0 ? `${d} 天 ${h} 小时` : `${h} 小时`;
}

/* ------------------------------- 登录 ------------------------------- */
const captchaState = { id: null, type: null, solved: false, payload: null };

async function loadCaptcha() {
  const box = $("#captchaBox");
  if (!box) return;
  captchaState.id = null;
  captchaState.solved = false;
  captchaState.payload = null;
  box.className = "captcha";
  box.innerHTML = `<div class="cap-loading">加载校验…</div>`;
  try {
    const d = await api("/api/captcha");
    captchaState.id = d.id;
    captchaState.type = d.type;
    if (d.type === "text") renderTextCaptcha(d);
    else renderSliderCaptcha(d);
  } catch (e) {
    box.innerHTML = `<div class="cap-loading" id="capRetry">校验加载失败，点此重试</div>`;
    $("#capRetry").addEventListener("click", loadCaptcha);
  }
}

const REFRESH_ICON = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
  stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.7"/>
  <path d="M20 4v7h-7"/></svg>`;
const ARROW_ICON = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
  stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`;
const CHECK_ICON = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
  stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`;

function renderTextCaptcha(d) {
  const box = $("#captchaBox");
  box.className = "captcha is-text";
  box.innerHTML = `
    <div class="cap-img">${d.image}
      <button type="button" class="cap-refresh" id="capRefresh" title="换一张" aria-label="换一张">${REFRESH_ICON}</button>
    </div>
    <input id="capInput" class="cap-input" placeholder="输入图中 4 位字符"
           autocomplete="off" maxlength="4" spellcheck="false" />`;
  const inp = $("#capInput");
  inp.addEventListener("input", () => {
    inp.value = inp.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 4);
    captchaState.solved = inp.value.length >= 4;
    captchaState.payload = { text: inp.value };
    box.classList.toggle("done", captchaState.solved);
  });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  $("#capRefresh").addEventListener("click", loadCaptcha);
}

function renderSliderCaptcha(d) {
  const box = $("#captchaBox");
  box.className = "captcha is-slider";
  const W = d.trackWidth, H = d.trackHeight, P = d.piece;
  const pieceW = (P / W) * 100;          // 方块宽度占图片宽度的百分比
  box.innerHTML = `
    <div class="cap-stage" style="aspect-ratio:${W} / ${H}">
      <div class="cap-bg">${d.image}</div>
      <div class="cap-piece" id="capPiece" style="width:${pieceW}%;top:${(d.pieceY / H) * 100}%;left:0"></div>
      <button type="button" class="cap-refresh" id="capRefresh" title="换一张" aria-label="换一张">${REFRESH_ICON}</button>
    </div>
    <div class="cap-track" id="capTrack">
      <div class="cap-fill" id="capFill"></div>
      <span class="cap-track-text" id="capTip">按住滑块，拖动完成拼图</span>
      <div class="cap-handle" id="capHandle">${ARROW_ICON}</div>
    </div>`;

  const track = $("#capTrack"), handle = $("#capHandle");
  const piece = $("#capPiece"), fill = $("#capFill");
  // 进度 r ∈ [0,1]：手柄走完轨道 = 方块走完图片。提交给服务端的是图片坐标系里的 x。
  const setProgress = (r) => {
    const travel = track.clientWidth - handle.offsetWidth;
    handle.style.left = r * travel + "px";
    fill.style.width = r * travel + handle.offsetWidth / 2 + "px";
    piece.style.left = r * (100 - pieceW) + "%";
    captchaState.payload = { x: Math.round(r * (W - P) * 10) / 10 };
  };

  handle.addEventListener("pointerdown", (e) => {
    if (captchaState.solved) return;
    handle.setPointerCapture(e.pointerId);
    track.classList.add("dragging");
    const startX = e.clientX;
    let r = 0;
    const onMove = (ev) => {
      const travel = track.clientWidth - handle.offsetWidth;
      r = Math.max(0, Math.min(1, (ev.clientX - startX) / travel));
      setProgress(r);
    };
    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      track.classList.remove("dragging");
      if (r < 0.02) { setProgress(0); return; }     // 没拖动，不算一次作答
      captchaState.solved = true;
      track.classList.add("done");
      handle.innerHTML = CHECK_ICON;
      if ($("#loginPass").value) {
        $("#capTip").textContent = "验证中…";
        doLogin();
      } else {
        $("#capTip").textContent = "已完成，输入密码后点登录";
      }
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });
  $("#capRefresh").addEventListener("click", loadCaptcha);
}

function showLogin() {
  $("#loginWrap").classList.remove("hidden");
  $("#appRoot").classList.add("hidden");
  loadCaptcha();
  setTimeout(() => $("#loginPass").focus(), 80);
}

function hideLogin(user) {
  $("#loginWrap").classList.add("hidden");
  $("#appRoot").classList.remove("hidden");
  if (user) $("#userName").textContent = user;
}

async function doLogin() {
  const user = $("#loginUser").value.trim();
  const password = $("#loginPass").value;
  $("#loginErr").textContent = "";
  if (!captchaState.id || !captchaState.solved) {
    $("#loginErr").textContent = "请先完成人机校验";
    return;
  }
  if (!password) {
    $("#loginErr").textContent = "请输入密码";
    return;
  }
  const btn = $("#loginBtn");
  busy(btn, true);
  try {
    const d = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ user, password, captchaId: captchaState.id, captcha: captchaState.payload }),
    });
    applyBootstrap(await api("/api/bootstrap"));
    toast("已登录", "ok");
  } catch (e) {
    $("#loginErr").textContent = e.message;
    loadCaptcha();            // 任何失败都换一张，防止重放
  } finally {
    busy(btn, false);
  }
}

$("#loginBtn").addEventListener("click", doLogin);
$("#loginPass").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
$("#logoutBtn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (e) { /* ignore */ }
  showLogin();
});

/* ------------------------------- 导航 ------------------------------- */
function setView(view) {
  state.view = view;
  if (view === "proxy" && typeof loadProxy === "function") loadProxy();
  if (view === "logs") loadJobs();
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
}

$$(".nav-item").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
$$("[data-goto]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.goto)));

/* ------------------------------- 渲染 ------------------------------- */
function renderStatus() {
  const b = state.status?.browser || {};
  const on = !!b.running;
  $("#engineDot").classList.toggle("on", on);
  $("#engineTitle").textContent = on ? "浏览器运行中" : "浏览器未启动";
  $("#engineSub").textContent = on ? (b.currentEmail || "已连接 · 未登录") : "点下方按钮启动";
}

function accountCard(a) {
  const plan = (a.plan || "free").toLowerCase();
  const cls = ["free", "go", "plus", "pro", "prolite", "team", "business", "enterprise"].includes(plan) ? plan : "free";
  const ok = a.status === "active";
  const soon = a.accessExp && a.accessExp * 1000 - Date.now() < 2 * 86400000;
  const plus = a.shots?.plus?.price;
  const pro = a.shots?.pro?.price;
  const card = (a.cards || [])[0];

  return `
  <article class="account ${a.isCurrent ? "is-current" : ""}" data-id="${esc(a.id)}">
    <div class="acc-top">
      <div class="avatar">${esc(initials(a))}</div>
      <div class="acc-id">
        <div class="acc-email" title="${esc(a.email)}">${esc(a.email || "未知邮箱")}</div>
        <div class="acc-alias">${esc(a.alias || a.name || "—")}</div>
      </div>
      <span class="badge ${cls}">${esc(a.planLabel || plan)}</span>
    </div>

    <div class="acc-meta">
      <span class="dot-status ${ok ? "ok" : a.status === "invalid" ? "bad" : ""}"></span>
      <span class="${soon ? "warn" : ""}">${esc(remain(a.accessExp))}</span>
      <span class="sep">/</span>
      <span>到期 ${esc(a.accessExpText || "—")}</span>
    </div>

    <div class="bound-card ${card ? "has" : ""}">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7">
        <rect x="3" y="6" width="18" height="12" rx="2.4"/><path d="M3 10.5h18"/>
      </svg>
      ${card
        ? `<span>${esc(card.brand)} ···· ${esc(card.last4)}</span><span class="spacer"></span>
           <button class="link" data-act="manage-cards">管理</button>`
        : `<span>未绑定银行卡</span><span class="spacer"></span>
           <button class="link" data-act="bind-card">去绑定</button>`}
    </div>

    <div class="acc-actions">
      <div class="acc-row-1">
        <button class="btn" data-act="login" ${a.hasSession ? "" : "disabled"}>一键登录</button>
        <span class="spacer"></span>
        <button class="btn ghost tiny" data-act="check" title="重新校验 token">校验</button>
        <button class="icon-btn danger" data-act="del" title="删除账号">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M6 7h12M9.5 7V5.5h5V7M8 7l.7 12h6.6L16 7" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
      </div>
      <div class="acc-row-2">
        <button class="btn plus" data-act="plan" data-plan="plus" title="一键进入 Plus 付款页" ${a.hasSession ? "" : "disabled"}>
          Plus${plus ? `<span class="price">${esc(plus)}</span>` : ""}
        </button>
        <button class="btn pro" data-act="plan" data-plan="pro" title="一键进入 Pro 付款页" ${a.hasSession ? "" : "disabled"}>
          Pro${pro ? `<span class="price">${esc(pro)}</span>` : ""}
        </button>
      </div>
    </div>
  </article>`;
}

function renderAccounts() {
  const kw = state.keyword.trim().toLowerCase();
  const list = state.accounts.filter((a) =>
    !kw || `${a.email} ${a.alias} ${a.name} ${a.plan}`.toLowerCase().includes(kw));

  $("#accountGrid").innerHTML = list.map(accountCard).join("");
  $("#navAccountCount").textContent = state.accounts.length;
  $("#accountEmpty").classList.toggle("hidden", state.accounts.length > 0);
  renderStatus();
}

// 卡组织标识（与客户页一致）
const BRAND_LOGO = {
  visa: '<span class="logo-visa">VISA</span>',
  mastercard: '<svg viewBox="0 0 26 16" width="24" height="15" aria-label="Mastercard">'
    + '<circle cx="9" cy="8" r="7" fill="#EB001B"/><circle cx="17" cy="8" r="7" fill="#F79E1B"/>'
    + '<path d="M13 2.26A7 7 0 0 1 13 13.74A7 7 0 0 1 13 2.26Z" fill="#FF5F00"/></svg>',
  amex: '<span class="logo-amex">AMEX</span>',
  jcb: '<span class="logo-text" style="color:#0b6b3a">JCB</span>',
  unionpay: '<span class="logo-text" style="color:#c81f26">银联</span>',
  discover: '<span class="logo-text" style="color:#e35205">DISC</span>',
  diners: '<span class="logo-text" style="color:#0b3d91">DINERS</span>',
  card: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#a1a1aa" stroke-width="1.7">'
    + '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h18"/></svg>',
};
const brandLogo = (key) => BRAND_LOGO[key] || BRAND_LOGO.card;

const TRASH_ICON = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7">
  <path d="M5 7h14M10 7V5h4v2M7.5 7l.8 12h7.4l.8-12" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

function cardRow(c) {
  const boundIds = c.accountIds || [];
  const chips = (c.accounts || []).map((a) =>
    `<span class="bind-chip">${esc(a.email)}
       <button data-act="unbind" data-id="${esc(c.id)}" data-acc="${esc(a.id)}" title="解绑">×</button>
     </span>`).join("");
  const menu = state.accounts.map((a) =>
    `<label><input type="checkbox" data-bindacc="${esc(c.id)}" value="${esc(a.id)}"
       ${boundIds.includes(a.id) ? "checked" : ""}> ${esc(a.email)}</label>`).join("");
  const cs = c.codeState || "active";
  const status = {
    active: '<span class="status ok">可用</span>',
    used: '<span class="status warn">已兑换</span>',
    revoked: '<span class="status bad">已作废</span>',
  }[cs] || "";
  const sub = [c.exp, c.holder, c.label].filter(Boolean).map(esc).join(" · ");

  return `
  <div class="card-row ${cs !== "active" ? "is-dim" : ""}" data-id="${esc(c.id)}">
    <div class="cr-card">
      <span class="brand-chip">${brandLogo(c.brand)}</span>
      <div class="cr-card-text">
        <div class="card-num">•••• ${esc(c.last4)}</div>
        <div class="card-sub">${sub || "—"}</div>
      </div>
    </div>
    <div class="cr-plans">
      ${["plus", "pro"].map((pl) => `
        <label class="plan-toggle ${(c.plans || []).includes(pl) ? "on" : ""}">
          <input type="checkbox" data-cardplan="${esc(c.id)}" value="${pl}"
            ${(c.plans || []).includes(pl) ? "checked" : ""} />${pl === "plus" ? "Plus" : "Pro"}
        </label>`).join("")}
    </div>
    <div class="cr-code">
      <div class="code-line"><code class="code-${esc(cs)}">${esc(c.code || "—")}</code>${status}</div>
      <div class="code-actions">
        <button class="mini" data-act="copy-code" data-id="${esc(c.id)}" data-code="${esc(c.code || "")}">复制</button>
        <button class="mini" data-act="rotate-code" data-id="${esc(c.id)}">换新码</button>
        ${cs !== "used"
          ? `<button class="mini" data-act="toggle-code" data-id="${esc(c.id)}" data-revoked="${cs === "revoked" ? 1 : ""}">${cs === "revoked" ? "启用" : "作废"}</button>`
          : ""}
      </div>
    </div>
    <div class="cr-bind bind-area">
      ${chips}
      <details class="bind-add">
        <summary>${chips ? "修改" : "绑定账号"}</summary>
        <div class="bind-menu">${menu || "<div class='muted small' style='padding:8px'>还没有账号</div>"}</div>
      </details>
    </div>
    <div class="cr-actions card-actions">
      <button class="mini" data-act="edit-card" data-id="${esc(c.id)}">编辑</button>
      <button class="mini" data-act="copy-card" data-id="${esc(c.id)}" title="复制卡号、有效期和 CVC">复制卡号</button>
      <button class="icon-btn danger" data-act="del-card" data-id="${esc(c.id)}" title="删除">${TRASH_ICON}</button>
    </div>
  </div>`;
}

const CARD_HEAD = `<div class="card-head"><span>卡片</span><span>可用套餐</span><span>兑换码</span><span>绑定账号</span><span></span></div>`;

/* ------------------------------- 兑换记录 ------------------------------- */
const JOB_STEPS = ["login", "plan", "checkout", "card", "address", "submit"];
const JOB_STEP_LABEL = {
  login: "登录 ChatGPT", plan: "打开订阅页", checkout: "进入付款页",
  card: "填写支付信息", address: "填写账单地址", submit: "提交订阅",
};

function jobTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function jobDuration(ms) {
  if (!ms && ms !== 0) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`;
}

function jobStatusTag(j) {
  if (!j.done) return '<span class="status warn">进行中</span>';
  if (j.ok) return '<span class="status ok">成功</span>';
  return '<span class="status bad">失败</span>';
}

function jobRow(j) {
  const stepTimeline = JOB_STEPS.map((k) => {
    const rec = (j.steps || []).find((s) => s.key === k);
    const st = rec ? rec.status : (j.done ? "skip" : "skip");
    const dur = rec && rec.durationMs != null ? ` <em>${jobDuration(rec.durationMs)}</em>` : "";
    return `<li class="jl-step ${st}"><i></i><span>${JOB_STEP_LABEL[k]}</span>${dur}</li>`;
  }).join("");
  const logs = (j.log || []).map((l) =>
    `<div class="jl-logline"><span>${jobTime(l.t)}</span>${esc(l.text)}</div>`).join("");
  const failLine = (j.done && !j.ok)
    ? `<div class="jl-error"><b>卡在「${esc(j.failedStepLabel || j.stepLabel || "—")}」</b>${esc(j.message || "")}</div>`
    : "";
  const px = j.proxy;
  const proxyLine = px
    ? `<div class="jl-proxy">出口：${esc(px.channel || "直连")}${px.session ? " · session " + esc(px.session) : ""}${px.ip ? " · " + esc([px.ip, px.country, px.city, px.org].filter(Boolean).join(" ")) : (px.error ? " · 检测失败 " + esc(px.error) : "")}</div>`
    : "";
  const shot = j.shot
    ? `<a class="jl-shot" href="${esc(j.shot)}" target="_blank" rel="noopener"><img src="${esc(j.shot)}" alt="页面截图" loading="lazy" /></a>`
    : "";
  return `
  <details class="job-item ${j.done ? (j.ok ? "ok" : "bad") : "running"}">
    <summary>
      <span class="jl-status">${jobStatusTag(j)}</span>
      <code class="jl-code">${esc(j.code || "—")}</code>
      <span class="jl-plan">${(j.plan || "").toUpperCase()}</span>
      <span class="jl-acc" title="${esc(j.account || "")}">${esc(j.account || "未知账号")}</span>
      <span class="jl-where">${j.done && !j.ok ? "卡在 " + esc(j.failedStepLabel || "—") : esc(j.stepLabel || "")}</span>
      <span class="jl-time">${jobTime(j.createdAt)}${j.durationMs ? " · " + jobDuration(j.durationMs) : ""}</span>
    </summary>
    <div class="jl-body">
      <div class="jl-main">
        <ul class="jl-timeline">${stepTimeline}</ul>
        ${failLine}
        ${proxyLine}
        <div class="jl-logs">${logs || '<div class="muted small">无日志</div>'}</div>
      </div>
      ${shot ? `<div class="jl-side">${shot}<span class="muted tiny">当时的页面</span></div>` : ""}
    </div>
  </details>`;
}

async function loadJobs() {
  try {
    const d = await api("/api/jobs");
    state.jobs = d.jobs || [];
  } catch (e) {
    toast(e.message, "bad");
    return;
  }
  $("#jobList").innerHTML = state.jobs.map(jobRow).join("");
  $("#jobEmpty").classList.toggle("hidden", state.jobs.length > 0);
  $("#navJobCount").textContent = state.jobs.length || "";
}

$("#jobsRefreshBtn")?.addEventListener("click", () => loadJobs());

function renderCards() {
  $("#cardList").innerHTML = state.cards.length ? CARD_HEAD + state.cards.map(cardRow).join("") : "";
  $("#navCardCount").textContent = state.cards.length;
  $("#cardEmpty").classList.toggle("hidden", state.cards.length > 0);
}

/* ------------------------------- 数据 ------------------------------- */
function applyData(acc, cards) {
  state.accounts = acc.accounts || [];
  state.status = acc.status;
  state.cards = cards.cards || [];
  renderAccounts();
  renderCards();
  renderStatus();
}

async function loadAll() {
  const [acc, cards] = await Promise.all([api("/api/accounts"), api("/api/cards")]);
  applyData(acc, cards);
}

/* 首屏 / 登录后：一次请求拿齐账号、卡库、代理（原先要串行 3 轮） */
function applyBootstrap(d) {
  hideLogin(d.user);
  applyData(d.accounts, d.cards);
  if (typeof renderProxy === "function" && d.proxy) renderProxy(d.proxy);
}

async function refreshStatus() {
  if (document.hidden) return;          // 标签页在后台时不轮询
  try {
    state.status = await api("/api/status");
    renderStatus();
  } catch (e) { /* silent */ }
}

/* ------------------------------- 账号操作 ------------------------------- */
async function loginAccount(a, btn) {
  busy(btn, true);
  toast("正在注入会话并打开窗口…");
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(a.id)}/login`, { method: "POST" });
    toast(`已登录 ${d.email}`, "ok");
    await loadAll();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

async function checkAccount(a, btn) {
  busy(btn, true);
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(a.id)}/check`, { method: "POST" });
    toast(d.account.status === "active" ? "token 有效" : `失效：${d.account.lastError}`,
      d.account.status === "active" ? "ok" : "bad");
    await loadAll();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

async function deleteAccount(a) {
  if (!confirm(`删除 ${a.email}？只删本地记录，不影响账号本身。`)) return;
  try {
    await api(`/api/accounts/${encodeURIComponent(a.id)}`, { method: "DELETE" });
    toast("已删除", "ok");
    await loadAll();
  } catch (e) { toast(e.message, "bad"); }
}

$("#accountGrid").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const a = state.accounts.find((x) => x.id === e.target.closest(".account")?.dataset.id);
  if (!a) return;
  switch (btn.dataset.act) {
    case "login": return loginAccount(a, btn);
    case "check": return checkAccount(a, btn);
    case "del": return deleteAccount(a);
    case "plan": return openPlan(a, btn.dataset.plan, btn);
    case "bind-card": openCardModal(a.id); return;
    case "manage-cards": setView("cards"); return;
    default: return;
  }
});

$("#accountSearch").addEventListener("input", (e) => {
  state.keyword = e.target.value;
  renderAccounts();
});
$("#refreshBtn").addEventListener("click", async () => {
  await loadAll();
  toast("已刷新", "ok");
});
$("#startBrowserBtn").addEventListener("click", async () => {
  try {
    toast("正在启动浏览器…");
    state.status = (await api("/api/browser/start", { method: "POST" })).status;
    renderStatus();
    toast("浏览器已就绪", "ok");
  } catch (e) { toast(e.message, "bad"); }
});

/* ------------------------------- 客户兑换页链接 ------------------------------- */
// 本地：管理端 8899 → 兑换页 8900；线上：管理端 :9443 → 兑换页是同域名的 443（根路径）
const CUSTOMER_URL = location.port === "8899"
  ? `${location.protocol}//${location.hostname}:8900/`
  : `${location.protocol}//${location.hostname}/`;
$("#customerLink").href = CUSTOMER_URL;

// 「查看浏览器画面」：线上管理端（9443）才有 noVNC（在 9444），本地开发没有就隐藏
if (location.port === "9443") {
  const vnc = $("#vncLink");
  vnc.classList.remove("hidden");
  vnc.addEventListener("click", async (e) => {
    e.preventDefault();
    // 先同步开一个空标签页，避免异步后被浏览器拦截弹窗
    const w = window.open("about:blank", "_blank");
    try {
      const d = await api("/api/vnc");   // 后端生成临时密码并应用
      const url = `https://${location.hostname}:${d.port || 9444}/vnc.html`
        + `?autoconnect=1&reconnect=1&resize=scale&path=websockify`
        + `&password=${encodeURIComponent(d.password)}`;
      if (w) w.location.href = url; else window.open(url, "_blank", "noopener");
    } catch (err) {
      if (w) w.close();
      toast(err.message, "bad");
    }
  });
}

/* ------------------------------- 导入 ------------------------------- */
const ADMIN_URL = location.origin;
const BOOKMARKLET =
  "javascript:(async()=>{try{const d=await(await fetch('/api/auth/session',{cache:'no-store'})).json();" +
  "if(!d||!d.accessToken){alert('当前页面没有登录态');return}" +
  "const s=btoa(unescape(encodeURIComponent(JSON.stringify(d))));" +
  "location.href='" + ADMIN_URL + "/#import='+encodeURIComponent(s)}" +
  "catch(e){alert('导入失败：'+e)}})()";

$("#bookmarklet").setAttribute("href", BOOKMARKLET);
$("#copyBookmarklet").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(BOOKMARKLET);
    toast("书签脚本已复制", "ok");
  } catch (e) {
    prompt("手动复制：", BOOKMARKLET);
  }
});

async function importAccounts() {
  const raw = $("#rawInput").value.trim();
  if (!raw) return toast("先粘贴 token", "bad");
  const alias = $("#aliasInput").value.trim();
  const btn = $("#importBtn");
  busy(btn, true);
  $("#importLog").innerHTML = "";
  try {
    const d = await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({ raw, alias }),
    });
    const results = d.results || [];
    const ok = results.filter((r) => r.ok).length;
    $("#importLog").innerHTML = results.map((r) => r.ok
      ? `<div class="ok">✓ 已导入 ${esc(r.account.email)}（${esc(r.account.planLabel)}）</div>`
      : `<div class="bad">✗ ${esc(r.error || "未知错误")}</div>`).join("");
    if (ok) {
      $("#rawInput").value = "";
      $("#aliasInput").value = "";
      toast(`导入 ${ok} 个账号`, "ok");
      await loadAll();
      setView("accounts");
    } else {
      toast("导入失败，检查 token 是否完整", "bad");
    }
  } catch (e) {
    toast(e.message, "bad");
    $("#importLog").innerHTML = `<div class="bad">✗ ${esc(e.message)}</div>`;
  } finally {
    busy(btn, false);
  }
}

$("#importBtn").addEventListener("click", importAccounts);
$("#rawInput").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") importAccounts();
});


/* ------------------------------- 国家/地区 ------------------------------- */
const COMMON_COUNTRIES = ["CN", "HK", "TW", "JP", "SG", "US", "GB", "PH", "MY", "TH",
  "VN", "ID", "KR", "AU", "CA", "AE", "DE", "FR", "NL", "TR"];
const ALL_COUNTRIES = ['AF', 'AX', 'AL', 'DZ', 'AS', 'AD', 'AO', 'AI', 'AQ', 'AG', 'AR', 'AM', 'AW', 'AU', 'AT', 'AZ', 'BS', 'BH', 'BD', 'BB', 'BY', 'BE', 'BZ', 'BJ', 'BM', 'BT', 'BO', 'BQ', 'BA', 'BW', 'BR', 'BN', 'BG', 'BF', 'BI', 'CV', 'KH', 'CM', 'CA', 'KY', 'CF', 'TD', 'CL', 'CN', 'CX', 'CC', 'CO', 'KM', 'CG', 'CD', 'CK', 'CR', 'CI', 'HR', 'CU', 'CW', 'CY', 'CZ', 'DK', 'DJ', 'DM', 'DO', 'EC', 'EG', 'SV', 'GQ', 'ER', 'EE', 'SZ', 'ET', 'FK', 'FO', 'FJ', 'FI', 'FR', 'GF', 'PF', 'GA', 'GM', 'GE', 'DE', 'GH', 'GI', 'GR', 'GL', 'GD', 'GP', 'GU', 'GT', 'GG', 'GN', 'GW', 'GY', 'HT', 'HN', 'HK', 'HU', 'IS', 'IN', 'ID', 'IR', 'IQ', 'IE', 'IM', 'IL', 'IT', 'JM', 'JP', 'JE', 'JO', 'KZ', 'KE', 'KI', 'KP', 'KR', 'KW', 'KG', 'LA', 'LV', 'LB', 'LS', 'LR', 'LY', 'LI', 'LT', 'LU', 'MO', 'MG', 'MW', 'MY', 'MV', 'ML', 'MT', 'MH', 'MQ', 'MR', 'MU', 'YT', 'MX', 'FM', 'MD', 'MC', 'MN', 'ME', 'MS', 'MA', 'MZ', 'MM', 'NA', 'NR', 'NP', 'NL', 'NC', 'NZ', 'NI', 'NE', 'NG', 'NU', 'NF', 'MK', 'MP', 'NO', 'OM', 'PK', 'PW', 'PS', 'PA', 'PG', 'PY', 'PE', 'PH', 'PN', 'PL', 'PT', 'PR', 'QA', 'RE', 'RO', 'RU', 'RW', 'BL', 'SH', 'KN', 'LC', 'MF', 'PM', 'VC', 'WS', 'SM', 'ST', 'SA', 'SN', 'RS', 'SC', 'SL', 'SG', 'SX', 'SK', 'SI', 'SB', 'SO', 'ZA', 'GS', 'SS', 'ES', 'LK', 'SD', 'SR', 'SJ', 'SE', 'CH', 'SY', 'TW', 'TJ', 'TZ', 'TH', 'TL', 'TG', 'TK', 'TO', 'TT', 'TN', 'TR', 'TM', 'TC', 'TV', 'UG', 'UA', 'AE', 'GB', 'US', 'UM', 'UY', 'UZ', 'VU', 'VA', 'VE', 'VN', 'VG', 'VI', 'WF', 'EH', 'YE', 'ZM', 'ZW'];

function countryLabel(code) {
  try {
    const dn = new Intl.DisplayNames(["zh-CN"], { type: "region" });
    return dn.of(code) || code;
  } catch (e) { return code; }
}

function fillCountrySelect(sel, selected) {
  if (!sel) return;
  const current = selected || sel.value || "JP";
  const common = COMMON_COUNTRIES.filter((c) => ALL_COUNTRIES.includes(c));
  const rest = ALL_COUNTRIES.filter((c) => !common.includes(c)).sort();
  const opt = (c) => `<option value="${c}" ${c === current ? "selected" : ""}>${countryLabel(c)} ${c}</option>`;
  sel.innerHTML =
    `<optgroup label="常用">${common.map(opt).join("")}</optgroup>` +
    `<optgroup label="全部">${rest.map(opt).join("")}</optgroup>`;
  if (!sel.value) sel.value = current;
}

/* ------------------------------- 卡库 ------------------------------- */
const CARD_TEXT_INPUTS = ["#newExp", "#newCvc", "#newHolder", "#newPostal",
  "#newState", "#newCity", "#newLine1", "#newLabel"];

function fillPlans(selected) {
  const sel = selected && selected.length ? selected : ["plus", "pro"];
  $("#newPlans").innerHTML = ["plus", "pro"].map((pl) =>
    `<label><input type="checkbox" value="${pl}" ${sel.includes(pl) ? "checked" : ""}> ${pl === "plus" ? "Plus" : "Pro"}</label>`
  ).join("");
}

function fillAccountsChecklist(checkedIds) {
  const set = new Set(checkedIds || []);
  $("#newAccounts").innerHTML = state.accounts.length
    ? state.accounts.map((a) =>
        `<label><input type="checkbox" value="${esc(a.id)}" ${set.has(a.id) ? "checked" : ""}> ${esc(a.email)}</label>`).join("")
    : `<div class="muted small" style="padding:8px">还没有账号</div>`;
}

function showCardModal() {
  $("#cardModal").classList.remove("hidden");
  $("#scrim").classList.add("on");
}

// 新增卡
function openCardModal(accountId) {
  state.editId = null;
  state.editLast4 = "";
  state.editBrand = "card";
  $("#cardModalTitle").textContent = "添加卡";
  $("#cardSave").textContent = "保存到卡库";
  $("#newNumber").value = "";
  $("#newNumber").placeholder = "1234 1234 1234 1234";
  $("#newCvc").placeholder = "CVC";
  CARD_TEXT_INPUTS.forEach((s) => ($(s).value = ""));
  fillCountrySelect($("#newCountry"), "JP");
  fillPlans(["plus", "pro"]);
  fillAccountsChecklist(accountId ? [accountId] : []);
  updatePreview();
  showCardModal();
  setTimeout(() => $("#newNumber").focus(), 60);
}

// 编辑卡：卡号/安全码留空＝不改，其余回显
function openCardEdit(c) {
  state.editId = c.id;
  state.editLast4 = c.last4 || "";
  state.editBrand = c.brand || "card";
  $("#cardModalTitle").textContent = "编辑卡";
  $("#cardSave").textContent = "保存修改";
  $("#newNumber").value = "";
  $("#newNumber").placeholder = `留空＝不改（当前 •••• ${c.last4 || "••••"}）`;
  $("#newCvc").value = "";
  $("#newCvc").placeholder = "留空＝不改";
  $("#newExp").value = c.exp || "";
  $("#newHolder").value = c.holder || "";
  $("#newPostal").value = c.postal || "";
  $("#newState").value = c.state || "";
  $("#newCity").value = c.city || "";
  $("#newLine1").value = c.line1 || "";
  $("#newLabel").value = c.label || "";
  fillCountrySelect($("#newCountry"), c.country || "JP");
  fillPlans(c.plans);
  fillAccountsChecklist(c.accountIds);
  updatePreview();
  showCardModal();
  setTimeout(() => $("#newHolder").focus(), 60);
}

function closeCardModal() {
  $("#cardModal").classList.add("hidden");
  $("#scrim").classList.remove("on");
}

$("#addCardBtn").addEventListener("click", () => openCardModal());
$("#addCardBtn2").addEventListener("click", () => openCardModal());
$("#cardModalClose").addEventListener("click", closeCardModal);
$("#cardCancel").addEventListener("click", closeCardModal);
$("#scrim").addEventListener("click", closeCardModal);

function luhnOk(num) {
  const d = [...num].map(Number);
  if (d.length < 12) return false;
  let sum = 0;
  d.reverse().forEach((v, i) => {
    if (i % 2) { v *= 2; if (v > 9) v -= 9; }
    sum += v;
  });
  return sum % 10 === 0;
}

// 按卡 BIN（前缀）识别发卡组织，与后端 store.card_brand 保持一致
function detectBrand(num) {
  const n = (num || "").replace(/\D/g, "");
  if (!n) return { key: "card", label: "CARD" };
  const two = +n.slice(0, 2);
  const three = +n.slice(0, 3);
  const four = +n.slice(0, 4);
  if (n[0] === "4") return { key: "visa", label: "VISA" };
  if ((two >= 51 && two <= 55) || (four >= 2221 && four <= 2720)) return { key: "mastercard", label: "MASTERCARD" };
  if (two === 34 || two === 37) return { key: "amex", label: "AMEX" };
  if (four >= 3528 && four <= 3589) return { key: "jcb", label: "JCB" };
  if (two === 62 || two === 81) return { key: "unionpay", label: "UNIONPAY" };
  if (/^6011/.test(n) || two === 65 || (three >= 644 && three <= 649)) return { key: "discover", label: "DISCOVER" };
  if (two === 36 || two === 38 || (three >= 300 && three <= 305)) return { key: "diners", label: "DINERS" };
  return { key: "card", label: "CARD" };
}

function updatePreview() {
  const num = $("#newNumber").value.replace(/\D/g, "");
  const brand = detectBrand(num);
  // 编辑模式下卡号留空时，用现有卡的后四位与品牌占位
  const editMasked = !num && state.editId && state.editLast4;
  const key = num ? brand.key : (editMasked ? (state.editBrand || "card") : "card");
  $("#cpNum").textContent = num
    ? num.replace(/(.{4})/g, "$1 ").trim().padEnd(19, "•")
    : (editMasked ? "•••• •••• •••• " + state.editLast4 : "•••• •••• •••• ••••");
  $("#cpBrand").innerHTML = (num || editMasked) ? brandLogo(key) : "";
  $("#cpBrand").classList.toggle("on", !!(num || editMasked));
  $("#cpBrandName").textContent = num && brand.key !== "card" ? brand.label : "";
  $("#cpExp").textContent = $("#newExp").value || "MM/YY";
  $("#cpHolder").textContent = $("#newHolder").value || "持卡人";

  const hint = $("#luhnHint");
  if (num.length >= 12) {
    const ok = luhnOk(num);
    hint.textContent = ok ? "✓ 卡号校验通过" : "✗ 卡号校验失败，检查是否输错";
    hint.className = `hint ${ok ? "ok" : "bad"}`;
  } else {
    hint.textContent = "";
    hint.className = "hint";
  }
}

$$("#newNumber, #newExp, #newHolder").forEach((el) => el.addEventListener("input", updatePreview));
$("#newNumber").addEventListener("input", (e) => {
  const d = e.target.value.replace(/\D/g, "").slice(0, 19);
  e.target.value = d.replace(/(.{4})/g, "$1 ").trim();
  updatePreview();
});
$("#newExp").addEventListener("input", (e) => {
  const d = e.target.value.replace(/\D/g, "").slice(0, 4);
  e.target.value = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
  updatePreview();
});
$("#newCvc").addEventListener("input", (e) => {
  e.target.value = e.target.value.replace(/\D/g, "").slice(0, 4);
});

$("#cardSave").addEventListener("click", async () => {
  const numRaw = $("#newNumber").value.replace(/\D/g, "");
  const cvcRaw = $("#newCvc").value.replace(/\D/g, "");
  const plans = [...document.querySelectorAll("#newPlans input:checked")].map((x) => x.value);
  if (!plans.length) return toast("至少勾选一个可用套餐", "bad");
  const payload = {
    exp: $("#newExp").value.trim(),
    holder: $("#newHolder").value.trim(),
    country: $("#newCountry").value,
    postal: $("#newPostal").value.trim(),
    state: $("#newState").value.trim(),
    city: $("#newCity").value.trim(),
    line1: $("#newLine1").value.trim(),
    label: $("#newLabel").value.trim(),
    accountIds: [...document.querySelectorAll("#newAccounts input:checked")].map((x) => x.value),
    plans,
  };
  const editing = !!state.editId;
  if (editing) {
    if (numRaw) payload.number = numRaw;   // 留空＝不改
    if (cvcRaw) payload.cvc = cvcRaw;
  } else {
    if (!numRaw || !payload.exp || !cvcRaw) {
      return toast("卡号 / 有效期 / 安全码必填", "bad");
    }
    payload.number = numRaw;
    payload.cvc = cvcRaw;
  }
  const btn = $("#cardSave");
  busy(btn, true);
  try {
    if (editing) {
      await api(`/api/cards/${encodeURIComponent(state.editId)}`, {
        method: "PATCH", body: JSON.stringify(payload),
      });
      toast("已保存修改", "ok");
    } else {
      await api("/api/cards", { method: "POST", body: JSON.stringify(payload) });
      toast("已添加到卡库", "ok");
    }
    closeCardModal();
    await loadAll();
    setView("cards");
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
});

$("#cardList").addEventListener("change", async (e) => {
  const planBox = e.target.closest("[data-cardplan]");
  if (planBox) {
    const cardId = planBox.dataset.cardplan;
    const plans = [...document.querySelectorAll(`[data-cardplan="${cardId}"]`)]
      .filter((x) => x.checked).map((x) => x.value);
    if (!plans.length) {
      planBox.checked = true;
      return toast("至少保留一个可用套餐", "bad");
    }
    try {
      await api(`/api/cards/${encodeURIComponent(cardId)}`, {
        method: "PATCH", body: JSON.stringify({ plans }),
      });
      toast("可用套餐已更新：" + plans.map((p) => p.toUpperCase()).join(" / "), "ok");
      await loadAll();
    } catch (err) { toast(err.message, "bad"); }
    return;
  }
  const box = e.target.closest("[data-bindacc]");
  if (!box) return;
  const cardId = box.dataset.bindacc;
  const ids = [...document.querySelectorAll(`[data-bindacc="${cardId}"]`)]
    .filter((x) => x.checked).map((x) => x.value);
  try {
    await api(`/api/cards/${encodeURIComponent(cardId)}`, {
      method: "PATCH",
      body: JSON.stringify({ accountIds: ids }),
    });
    toast(box.checked ? "已绑定账号" : "已解绑", "ok");
    await loadAll();
  } catch (err) {
    toast(err.message, "bad");
  }
});

$("#cardList").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const id = btn.dataset.id;
  if (btn.dataset.act === "edit-card") {
    const c = state.cards.find((x) => x.id === id);
    if (c) openCardEdit(c);
    return;
  }
  if (btn.dataset.act === "del-card") {
    if (!confirm("从卡库删除这张卡？")) return;
    try {
      await api(`/api/cards/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("已删除", "ok");
      await loadAll();
    } catch (err) { toast(err.message, "bad"); }
  }
  if (btn.dataset.act === "unbind") {
    try {
      await api(`/api/cards/${encodeURIComponent(id)}`, {
        method: "PATCH", body: JSON.stringify({ unbindAccountId: btn.dataset.acc }),
      });
      toast("已解绑", "ok");
      await loadAll();
    } catch (err) { toast(err.message, "bad"); }
    return;
  }
  if (btn.dataset.act === "copy-code") {
    try {
      await navigator.clipboard.writeText(btn.dataset.code || "");
      toast("兑换码已复制", "ok");
    } catch (err) { toast("复制失败", "bad"); }
    return;
  }
  if (btn.dataset.act === "rotate-code") {
    if (!confirm("换新码后旧兑换码立即失效（含已兑换/已作废的），继续？")) return;
    try {
      await api(`/api/cards/${encodeURIComponent(id)}/rotate-code`, { method: "POST" });
      toast("已生成新兑换码", "ok");
      await loadAll();
    } catch (err) { toast(err.message, "bad"); }
    return;
  }
  if (btn.dataset.act === "toggle-code") {
    const revoke = !btn.dataset.revoked;   // 当前已作废则启用，否则作废
    try {
      await api(`/api/cards/${encodeURIComponent(id)}/revoke-code`, {
        method: "POST", body: JSON.stringify({ revoked: revoke }),
      });
      toast(revoke ? "兑换码已作废" : "兑换码已启用", "ok");
      await loadAll();
    } catch (err) { toast(err.message, "bad"); }
    return;
  }
  if (btn.dataset.act === "copy-card") {
    try {
      const d = await api(`/api/cards/${encodeURIComponent(id)}/reveal`, { method: "POST" });
      await navigator.clipboard.writeText(`${d.number}\t${d.exp}\t${d.cvc}`);
      toast("已复制：卡号 · 有效期 · CVC", "ok");
    } catch (err) { toast(err.message, "bad"); }
  }
});

/* ------------------------------- 订阅抽屉 ------------------------------- */
function steps(active) {
  $$(".step").forEach((el) => {
    const n = Number(el.dataset.step);
    el.classList.toggle("active", n === active);
    el.classList.toggle("done", n < active);
  });
}

function openDrawer() {
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
}

$("#drawerClose").addEventListener("click", closeDrawer);

function vaultOptions(accountId) {
  const own = state.cards.filter((c) => !c.accountId || c.accountId === accountId);
  if (!own.length) return `<option value="">卡库为空，先去添加</option>`;
  return `<option value="">手动输入</option>` + own.map((c) =>
    `<option value="${esc(c.id)}" data-addr='${esc(JSON.stringify({
      holder: c.holder, country: c.country, postal: c.postal,
      state: c.state, city: c.city, line1: c.line1,
    }))}'>${esc(c.brand)} ···· ${esc(c.last4)}${c.label ? " · " + esc(c.label) : ""}</option>`
  ).join("");
}

/* 一键进结账页：点 Plus / Pro 直接打开付款页（失败可在抽屉里重试） */
async function openPlan(a, plan, btn) {
  state.acc = a;
  state.plan = plan;
  state.cardId = null;
  const name = plan === "plus" ? "Plus" : "Pro";

  openDrawer();
  $("#drawerKicker").textContent = plan.toUpperCase();
  $("#drawerTitle").textContent = `${name} 订阅`;
  $("#priceRow").innerHTML = "";
  $("#drawerShot").removeAttribute("src");
  $("#vaultCard").innerHTML = vaultOptions(a.id);
  fillCountrySelect($("#addrCountry"), $("#addrCountry").value || "JP");
  await goCheckout(btn);
}

async function goCheckout(btn) {
  const { acc, plan } = state;
  if (!acc) return;
  const name = plan === "plus" ? "Plus" : "Pro";
  steps(1);
  $("#drawerSub").textContent = `${acc.email} · 正在进入 ${name} 付款页`;
  $("#shotLoading").classList.remove("hidden");
  $("#shotLoading").textContent = `正在进入 ${name} 付款页…`;
  $("#checkoutBox").classList.add("hidden");
  $("#stagePlan").classList.add("hidden");
  $("#coAmount").textContent = "—";
  $("#coState").className = "pill-state";
  $("#coState").textContent = "等待填写";
  $("#submitBtn").disabled = true;

  const btns = [btn, $("#goCheckoutBtn")].filter(Boolean);
  btns.forEach((b) => busy(b, true));
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(acc.id)}/checkout`, {
      method: "POST",
      body: JSON.stringify({ plan }),
    });
    if (d.screenshotUrl) {
      const img = new Image();
      img.onload = () => $("#shotLoading").classList.add("hidden");
      img.src = d.screenshotUrl;
      $("#drawerShot").src = d.screenshotUrl;
    }
    const secs = d.elapsedMs ? ` · 用时 ${(d.elapsedMs / 1000).toFixed(1)}s` : "";

    if (!d.ok || !d.url) {
      $("#shotLoading").textContent = d.error || "没进入付款页";
      $("#stagePlan").classList.remove("hidden");
      $("#goCheckoutBtn").textContent = "重试 · 进入付款页";
      $("#drawerSub").textContent = `${d.email || acc.email} · 没进入付款页${secs}`;
      toast(d.error || "没进入付款页：可能已经是当前套餐", "bad");
      return;
    }

    $("#coAmount").textContent = d.amount || "—";
    $("#priceRow").innerHTML = `<span class="price-tag">已选 <b>${name}</b></span>`
      + (d.amount ? `<span class="price-tag">今日应付 <b>${esc(d.amount)}</b></span>` : "");
    $("#checkoutBox").classList.remove("hidden");
    steps(2);
    $("#coState").className = "pill-state " + (d.ready ? "ok" : "");
    $("#coState").textContent = d.ready ? "卡信息字段就绪" : "页面仍在加载";
    $("#drawerSub").textContent = `${d.email || acc.email} · 已进入 ${name} 付款页${secs}`;
    toast(d.ready ? `${name} 付款页就绪` : "付款页还在加载", d.ready ? "ok" : "bad");
    await loadAll();
  } catch (e) {
    $("#shotLoading").textContent = e.message;
    $("#stagePlan").classList.remove("hidden");
    $("#goCheckoutBtn").textContent = "重试 · 进入付款页";
    toast(e.message, "bad");
  } finally {
    btns.forEach((b) => busy(b, false));
  }
}

$("#goCheckoutBtn").addEventListener("click", () => goCheckout($("#goCheckoutBtn")));

$("#vaultCard").addEventListener("change", (e) => {
  const opt = e.target.selectedOptions[0];
  state.cardId = e.target.value || null;
  $("#manualCard").open = !state.cardId;
  if (!state.cardId) return;
  try {
    const addr = JSON.parse(opt.dataset.addr || "{}");
    if (addr.holder) $("#addrName").value = addr.holder;
    if (addr.country) $("#addrCountry").value = addr.country;
    if (addr.postal) $("#addrPostal").value = addr.postal;
    if (addr.state) $("#addrState").value = addr.state;
    if (addr.city) $("#addrCity").value = addr.city;
    if (addr.line1) $("#addrLine1").value = addr.line1;
  } catch (err) { /* ignore */ }
  $("#vaultHint").textContent = "已选卡，账单地址会自动带出。";
});

$("#fillCardBtn").addEventListener("click", async () => {
  const acc = state.acc;
  if (!acc) return;
  let payload;
  if (state.cardId) {
    payload = { cardId: state.cardId };
  } else {
    const number = $("#cardNumber").value.replace(/\D/g, "");
    const exp = $("#cardExp").value.replace(/\D/g, "");
    const cvc = $("#cardCvc").value.replace(/\D/g, "");
    if (number.length < 13) return toast("卡号不完整", "bad");
    if (exp.length !== 4) return toast("有效期填 MM/YY", "bad");
    if (cvc.length < 3) return toast("安全码不完整", "bad");
    payload = { number, exp, cvc };
  }
  const btn = $("#fillCardBtn");
  busy(btn, true);
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(acc.id)}/card`, {
      method: "POST", body: JSON.stringify(payload),
    });
    $("#coState").className = "pill-state ok";
    $("#coState").textContent = `已填入 ${d.count} 项 · ${d.fields.number}`;
    $("#submitBtn").disabled = d.count !== 3;
    toast("卡信息已填入浏览器", "ok");
  } catch (e) {
    $("#coState").className = "pill-state bad";
    $("#coState").textContent = "填充失败";
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
});

$("#revealBtn").addEventListener("click", async () => {
  const acc = state.acc;
  if (!acc) return;
  const btn = $("#revealBtn");
  busy(btn, true);
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(acc.id)}/reveal`, { method: "POST" });
    if (d.screenshotUrl) $("#drawerShot").src = d.screenshotUrl;
    $("#coState").className = "pill-state " + (d.revealed ? "ok" : "bad");
    $("#coState").textContent = d.revealed ? "账单地址已展开" : "未展开";
    toast(d.revealed ? "地址区块已展开" : "没检测到地址区块", d.revealed ? "ok" : "bad");
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
});

$("#fillAddrBtn").addEventListener("click", async () => {
  const acc = state.acc;
  if (!acc) return;
  const body = state.cardId
    ? { cardId: state.cardId }
    : {
        name: $("#addrName").value.trim(),
        country: $("#addrCountry").value,
        postal: $("#addrPostal").value.trim(),
        state: $("#addrState").value.trim(),
        city: $("#addrCity").value.trim(),
        line1: $("#addrLine1").value.trim(),
      };
  const btn = $("#fillAddrBtn");
  busy(btn, true);
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(acc.id)}/address`, {
      method: "POST", body: JSON.stringify(body),
    });
    if (d.screenshotUrl) $("#drawerShot").src = d.screenshotUrl;
    const n = Object.values(d.values || {}).filter(Boolean).length;
    $("#coState").className = "pill-state ok";
    $("#coState").textContent = `地址已填 ${n} 项`;
    toast("账单地址已填入", "ok");
  } catch (e) {
    $("#coState").className = "pill-state bad";
    $("#coState").textContent = "地址填写失败";
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
});

$("#submitBtn").addEventListener("click", async () => {
  const acc = state.acc;
  if (!acc) return;
  const amount = $("#coAmount").textContent.trim();
  if (!confirm(`确认提交订阅？\n\n账号：${acc.email}\n金额：${amount}\n\n点击确定会真实扣款，无法撤回。`)) return;
  const btn = $("#submitBtn");
  busy(btn, true);
  steps(3);
  $("#coState").className = "pill-state";
  $("#coState").textContent = "提交中…";
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(acc.id)}/subscribe`, {
      method: "POST", body: JSON.stringify({ confirm: true }),
    });
    const done = !d.pending;
    $("#coState").className = "pill-state " + (done ? "ok" : "");
    $("#coState").textContent = done ? "已提交" : "等待验证";
    const firstLine = (d.text || "").split("\n").filter(Boolean)[0] || "";
    toast(done ? `已提交：${firstLine.slice(0, 40)}` : "已提交，可能需要 3DS", done ? "ok" : "");
    await loadAll();
  } catch (e) {
    $("#coState").className = "pill-state bad";
    $("#coState").textContent = "提交失败";
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
});

$("#cardNumber").addEventListener("input", (e) => {
  const d = e.target.value.replace(/\D/g, "").slice(0, 19);
  e.target.value = d.replace(/(.{4})/g, "$1 ").trim();
});
$("#cardExp").addEventListener("input", (e) => {
  const d = e.target.value.replace(/\D/g, "").slice(0, 4);
  e.target.value = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
});
$("#cardCvc").addEventListener("input", (e) => {
  e.target.value = e.target.value.replace(/\D/g, "").slice(0, 4);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("#channelModal").classList.contains("hidden")) closeChannelModal();
    else if (!$("#cardModal").classList.contains("hidden")) closeCardModal();
    else closeDrawer();
  }
});

/* ------------------------------- 代理 IP ------------------------------- */
const proxyState = { data: null, editId: null, results: {} };

const ROTATION_LABEL = { task: "每任务一个 IP", connection: "每连接换 IP" };

function exitText(x) {
  if (!x) return "出口未检测";
  if (!x.ok) return "检测失败：" + (x.error || "");
  return [x.ip, [x.country, x.city].filter(Boolean).join(" "), x.org, x.ms != null ? x.ms + "ms" : ""]
    .filter(Boolean).join(" · ");
}

function channelRow(c) {
  const res = proxyState.results[c.id];
  const resHtml = res
    ? `<span class="ch-result ${res.ok ? "ok" : "bad"}">${esc(res.text)}</span>` : "";
  return `
  <div class="ch-row ${c.isDefault ? "is-default" : ""}" data-id="${esc(c.id)}">
    <input type="radio" name="defaultChannel" value="${esc(c.id)}" ${c.isDefault ? "checked" : ""} title="设为默认渠道" />
    <div class="ch-main">
      <div class="ch-name">${esc(c.name)}
        <span class="badge">${c.type === "kookeey" ? "kookeey" : "自定义"}</span>
        <span class="badge">${ROTATION_LABEL[c.rotation] || c.rotation}</span>
        ${c.isDefault ? '<span class="badge pro">默认</span>' : ""}
      </div>
      <div class="ch-sub" title="${esc(c.summary)}">${esc(c.summary)}${c.type === "kookeey" && !c.hasToken ? " · 未填 token（换 IP 用新 session）" : ""}</div>
      ${resHtml}
    </div>
    <div class="ch-actions">
      <button class="mini" data-ch="test">测试</button>
      ${c.type === "kookeey" ? '<button class="mini" data-ch="traffic">流量</button>' : ""}
      <button class="mini" data-ch="edit">编辑</button>
      <button class="mini" data-ch="delete">删除</button>
    </div>
  </div>`;
}

function leaseRow(l) {
  const when = l.rotatedAt || l.startedAt;
  return `
  <div class="ch-row" data-slot="${esc(l.slot)}">
    <div class="ch-main">
      <div class="ch-name">${esc(l.label)}
        <span class="status ${l.running ? "ok" : ""}">${l.running ? "运行中" : "未运行"}</span>
      </div>
      <div class="ch-sub">${esc(l.channelName)}${l.session ? " · session " + esc(l.session) : ""}${l.jobId ? " · 任务 " + esc(l.jobId) : ""}${when ? " · " + jobTime(when) : ""}</div>
      <span class="ch-result ${l.exit ? (l.exit.ok ? "ok" : "bad") : ""}">${esc(exitText(l.exit))}</span>
    </div>
    <div class="ch-actions">
      <button class="mini" data-lease="test">检测</button>
      <button class="mini" data-lease="rotate">切换 IP</button>
    </div>
  </div>`;
}

function renderProxy(d) {
  proxyState.data = d;
  const r = d.relay || {};
  $("#relayState").textContent = r.alive
    ? (r.slotsAlive ? `中继运行中 · ${r.port}` : "中继是旧版，请重启 proxy_relay.py")
    : "中继未运行（浏览器直连）";
  $("#relayState").className = "pill-state " + (r.alive && r.slotsAlive ? "ok" : "bad");

  const direct = `
  <div class="ch-row ${d.defaultId ? "" : "is-default"}">
    <input type="radio" name="defaultChannel" value="" ${d.defaultId ? "" : "checked"} title="不走代理" />
    <div class="ch-main"><div class="ch-name">直连 ${d.defaultId ? "" : '<span class="badge pro">默认</span>'}</div>
      <div class="ch-sub">不走代理，用服务器本机 IP</div></div>
  </div>`;
  $("#channelList").innerHTML = (d.channels || []).map(channelRow).join("") + direct;

  const leases = d.leases || [];
  $("#leaseList").innerHTML = leases.length
    ? leases.map(leaseRow).join("")
    : '<div class="ch-empty">暂无。浏览器启动时会按默认渠道分配出口。</div>';

  const main = leases.find((l) => l.slot === "main");
  $("#mainLease").textContent = main
    ? `${main.channelName}${main.session ? " · session " + main.session : ""}`
    : "浏览器未启动";
  if (main && main.exit) $("#proxyExit").textContent = exitText(main.exit);
}

async function loadProxy() {
  try {
    renderProxy(await api("/api/proxy"));
  } catch (e) {
    $("#relayState").textContent = "读取失败";
    $("#relayState").className = "pill-state bad";
  }
}

async function proxyPost(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body || {}) });
}

/* ---- 默认渠道 ---- */
$("#channelList").addEventListener("change", async (e) => {
  if (e.target.name !== "defaultChannel") return;
  try {
    renderProxy(await proxyPost("/api/proxy/default", { id: e.target.value }));
    toast("默认渠道已切换；新开的浏览器 / 下一笔开通生效，管理端点「切换 IP」立即生效", "ok");
  } catch (err) {
    toast(err.message, "bad");
    loadProxy();
  }
});

/* ---- 渠道操作 ---- */
$("#channelList").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-ch]");
  if (!btn) return;
  const id = btn.closest(".ch-row").dataset.id;
  const ch = (proxyState.data?.channels || []).find((c) => c.id === id);
  if (!ch) return;
  const act = btn.dataset.ch;
  if (act === "edit") return openChannelModal(ch);
  if (act === "delete") {
    if (!confirm(`删除渠道「${ch.name}」？`)) return;
    try {
      renderProxy(await api(`/api/proxy/channels/${encodeURIComponent(id)}`, { method: "DELETE" }));
      toast("已删除", "ok");
    } catch (err) { toast(err.message, "bad"); }
    return;
  }
  busy(btn, true);
  try {
    const d = await proxyPost(`/api/proxy/channels/${encodeURIComponent(id)}/${act}`);
    if (act === "test") {
      proxyState.results[id] = { ok: d.ok, text: (d.session ? `session ${d.session} → ` : "") + exitText(d) };
    } else {
      proxyState.results[id] = d.ok
        ? { ok: true, text: `剩余 ${d.leftGb ?? "?"} GB / 共 ${d.totalGb ?? "?"} GB${d.package ? " · " + d.package : ""}${d.expireAt ? " · 到期 " + new Date(d.expireAt * 1000).toLocaleDateString() : ""}` }
        : { ok: false, text: d.error || "查询失败" };
    }
    renderProxy(proxyState.data);
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

/* ---- 在用出口：检测 / 切换 ---- */
async function slotAction(slot, act, btn) {
  busy(btn, true);
  try {
    if (act === "rotate") {
      const d = await proxyPost("/api/proxy/rotate", { slot });
      toast(`${d.note}${d.exit?.ok ? " → " + d.exit.ip : ""}`, d.exit?.ok === false ? "bad" : "ok");
    } else {
      const d = await proxyPost("/api/proxy/test", { slot });
      if (!d.ok) toast("检测失败：" + (d.error || ""), "bad");
      if (slot === "main") $("#proxyExit").textContent = exitText(d);
    }
    await loadProxy();
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
}

$("#leaseList").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-lease]");
  if (btn) slotAction(btn.closest(".ch-row").dataset.slot, btn.dataset.lease, btn);
});
$("#testIpBtn").addEventListener("click", (e) => {
  $("#proxyExit").textContent = "检测中…";
  slotAction("main", "test", e.currentTarget);
});
$("#rotateIpBtn").addEventListener("click", (e) => slotAction("main", "rotate", e.currentTarget));

$("#proxyRestartBtn").addEventListener("click", async () => {
  const btn = $("#proxyRestartBtn");
  busy(btn, true);
  try {
    const d = await proxyPost("/api/proxy/restart");
    toast("浏览器已重启，已分配新的出口", "ok");
    if (d.proxy) renderProxy(d.proxy);
    await refreshStatus();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
});

/* ---- 渠道弹窗 ---- */
function chType() { return $("#chType").value; }

function syncChannelType() {
  $("#chKkFields").classList.toggle("hidden", chType() !== "kookeey");
  $("#chCustomFields").classList.toggle("hidden", chType() !== "custom");
}

function openChannelModal(ch) {
  const c = ch || {};
  proxyState.editId = ch ? ch.id : null;
  $("#channelModalTitle").textContent = ch ? `编辑渠道 · ${c.name}` : "添加渠道";
  $("#chType").value = c.type || "kookeey";
  $("#chType").disabled = !!ch;
  $("#chName").value = c.name || "";
  $("#chRotation").value = c.rotation || "task";
  $("#chUid").value = c.uid || "";
  $("#chAccessid").value = c.accessid || "";
  $("#chToken").value = "";
  $("#chToken").placeholder = c.hasToken ? "已保存（留空不改）" : "账户中心 → 开发者 token";
  $("#chAuthName").value = c.authName || "";
  $("#chAuthPwd").value = "";
  $("#chAuthPwd").placeholder = c.hasAuthPwd ? "已保存（留空不改）" : "安全策略密码";
  $("#chGeo").value = c.geo || "US";
  $("#chProtocol").value = c.protocol || "http";
  $("#chGateway").innerHTML = (proxyState.data?.gateways || [])
    .map((g) => `<option value="${esc(g.host)}">${esc(g.host)} · ${esc(g.label)}</option>`).join("");
  $("#chGateway").value = c.gateway || "gate.kookeey.info";
  $("#chPort").value = c.port || 1000;
  $("#chUpstream").value = "";
  $("#chUpstream").placeholder = c.upstreamMasked
    ? `已保存：${c.upstreamMasked}（留空不改）` : "http://user-{session}:pass@host:port   或   host:port:user:pass";
  $("#chChangeUrl").value = "";
  $("#chChangeUrl").placeholder = c.hasChangeIpUrl ? "已保存（留空 = 清除）" : "https://provider.example/api/change?key=…";
  $("#chDefault").checked = ch ? !!c.isDefault : !(proxyState.data?.channels || []).length;
  $("#chAuthHint").textContent = "填好用户 ID 和开发者 token 后可自动获取提取模式账密";
  $("#chAuthHint").className = "hint";
  syncChannelType();
  $("#channelModal").classList.remove("hidden");
}

function closeChannelModal() {
  $("#channelModal").classList.add("hidden");
}

$("#addChannelBtn").addEventListener("click", () => openChannelModal(null));
$("#channelModalClose").addEventListener("click", closeChannelModal);
$("#channelCancel").addEventListener("click", closeChannelModal);
$("#channelModal").addEventListener("click", (e) => { if (e.target.id === "channelModal") closeChannelModal(); });
$("#chType").addEventListener("change", syncChannelType);

$("#chFetchAuth").addEventListener("click", async () => {
  const btn = $("#chFetchAuth");
  busy(btn, true);
  try {
    const d = await proxyPost("/api/proxy/kookeey/auth", {
      uid: $("#chUid").value.trim(), accessid: $("#chAccessid").value.trim(),
      token: $("#chToken").value.trim(), channelId: proxyState.editId,
    });
    if (!d.ok) throw new Error(d.error || "拉取失败");
    $("#chAuthName").value = d.authName;
    $("#chAuthPwd").value = d.authPwd;
    $("#chAuthHint").textContent = `已获取：${d.authName}`;
    $("#chAuthHint").className = "hint ok";
  } catch (e) {
    $("#chAuthHint").textContent = e.message;
    $("#chAuthHint").className = "hint bad";
  } finally {
    busy(btn, false);
  }
});

$("#channelSave").addEventListener("click", async () => {
  const btn = $("#channelSave");
  const body = {
    type: chType(), name: $("#chName").value.trim(), rotation: $("#chRotation").value,
    makeDefault: $("#chDefault").checked,
  };
  if (body.type === "kookeey") {
    Object.assign(body, {
      uid: $("#chUid").value.trim(), accessid: $("#chAccessid").value.trim(),
      token: $("#chToken").value.trim(), authName: $("#chAuthName").value.trim(),
      authPwd: $("#chAuthPwd").value.trim(), geo: $("#chGeo").value.trim(),
      protocol: $("#chProtocol").value, gateway: $("#chGateway").value, port: $("#chPort").value.trim(),
    });
  } else {
    Object.assign(body, { upstream: $("#chUpstream").value.trim(), changeIpUrl: $("#chChangeUrl").value.trim() });
  }
  busy(btn, true);
  try {
    const id = proxyState.editId;
    const d = id
      ? await api(`/api/proxy/channels/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) })
      : await proxyPost("/api/proxy/channels", body);
    renderProxy(d);
    closeChannelModal();
    toast("渠道已保存", "ok");
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
});

/* ------------------------------- 书签回传 ------------------------------- */
async function consumeHashImport() {
  const h = location.hash || "";
  if (!h.startsWith("#import=")) return false;
  history.replaceState(null, "", location.pathname);
  try {
    const b64 = decodeURIComponent(h.slice(8));
    const raw = decodeURIComponent(escape(atob(b64)));
    const d = await api("/api/import-session", {
      method: "POST",
      body: JSON.stringify({ raw }),
    });
    if (d.ok && d.imported?.length) {
      toast(`书签导入成功：${d.imported[0].email}`, "ok");
      await loadAll();
      setView("accounts");
    } else {
      toast(d.error || "导入失败", "bad");
    }
  } catch (e) {
    toast(`导入失败：${e.message}`, "bad");
  }
  return true;
}

/* ------------------------------- 启动 ------------------------------- */
(async function boot() {
  // 直接 fetch：未登录时 401 只走一次 showLogin（api() 自己也会调一次）
  let d = null;
  try {
    const res = await fetch("/api/bootstrap");
    if (res.ok) d = await res.json();
  } catch (e) { /* 网络错误按未登录处理 */ }
  if (!d) return showLogin();
  if ((location.hash || "").startsWith("#import=")) {
    hideLogin(d.user);
    await consumeHashImport().catch(() => {});
    d = await api("/api/bootstrap");     // 书签回传后重新拉一次，显示刚导入的账号
  }
  applyBootstrap(d);
})();
window.addEventListener("hashchange", () => consumeHashImport().catch(() => {}));
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
setInterval(refreshStatus, 15000);
setInterval(() => { renderAccounts(); }, 60000);
