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
    hideLogin(d.user);
    await loadAll();
    if (typeof loadProxy === "function") loadProxy();
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
        <button class="btn plus" data-act="plan" data-plan="plus" ${a.hasSession ? "" : "disabled"}>
          Plus${plus ? `<span class="price">${esc(plus)}</span>` : ""}
        </button>
        <button class="btn pro" data-act="plan" data-plan="pro" ${a.hasSession ? "" : "disabled"}>
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
      <button class="mini" data-act="copy-card" data-id="${esc(c.id)}" title="复制卡号、有效期和 CVC">复制卡号</button>
      <button class="icon-btn danger" data-act="del-card" data-id="${esc(c.id)}" title="删除">${TRASH_ICON}</button>
    </div>
  </div>`;
}

const CARD_HEAD = `<div class="card-head"><span>卡片</span><span>可用套餐</span><span>兑换码</span><span>绑定账号</span><span></span></div>`;

function renderCards() {
  $("#cardList").innerHTML = state.cards.length ? CARD_HEAD + state.cards.map(cardRow).join("") : "";
  $("#navCardCount").textContent = state.cards.length;
  $("#cardEmpty").classList.toggle("hidden", state.cards.length > 0);
}

/* ------------------------------- 数据 ------------------------------- */
async function loadAll() {
  const [acc, cards] = await Promise.all([api("/api/accounts"), api("/api/cards")]);
  state.accounts = acc.accounts || [];
  state.status = acc.status;
  state.cards = cards.cards || [];
  renderAccounts();
  renderCards();
  renderStatus();
}

async function refreshStatus() {
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
function openCardModal(accountId) {
  fillCountrySelect($("#newCountry"), $("#newCountry").value || "JP");
  $("#cardModal").classList.remove("hidden");
  $("#scrim").classList.add("on");
  $("#newPlans").innerHTML = ["plus", "pro"].map((pl) =>
    `<label><input type="checkbox" value="${pl}" checked> ${pl === "plus" ? "Plus" : "Pro"}</label>`
  ).join("");
  $("#newAccounts").innerHTML = state.accounts.length
    ? state.accounts.map((a) =>
        `<label><input type="checkbox" value="${esc(a.id)}"
          ${accountId === a.id ? "checked" : ""}> ${esc(a.email)}</label>`).join("")
    : `<div class="muted small" style="padding:8px">还没有账号</div>`;
  setTimeout(() => $("#newNumber").focus(), 60);
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
  $("#cpNum").textContent = num
    ? num.replace(/(.{4})/g, "$1 ").trim().padEnd(19, "•")
    : "•••• •••• •••• ••••";
  $("#cpBrand").innerHTML = num ? brandLogo(brand.key) : "";
  $("#cpBrand").classList.toggle("on", !!num);
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
  const payload = {
    number: $("#newNumber").value,
    exp: $("#newExp").value,
    cvc: $("#newCvc").value,
    holder: $("#newHolder").value.trim(),
    country: $("#newCountry").value,
    postal: $("#newPostal").value.trim(),
    state: $("#newState").value.trim(),
    city: $("#newCity").value.trim(),
    line1: $("#newLine1").value.trim(),
    label: $("#newLabel").value.trim(),
    accountIds: [...document.querySelectorAll("#newAccounts input:checked")].map((x) => x.value),
    plans: [...document.querySelectorAll("#newPlans input:checked")].map((x) => x.value),
  };
  if (!payload.number.replace(/\D/g, "") || !payload.exp || !payload.cvc) {
    return toast("卡号 / 有效期 / 安全码必填", "bad");
  }
  const btn = $("#cardSave");
  busy(btn, true);
  try {
    await api("/api/cards", { method: "POST", body: JSON.stringify(payload) });
    toast("已保存到卡库", "ok");
    closeCardModal();
    $$("#newNumber, #newExp, #newCvc, #newHolder, #newPostal, #newState, #newCity, #newLine1, #newLabel")
      .forEach((el) => (el.value = ""));
    updatePreview();
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

async function openPlan(a, plan, btn) {
  state.acc = a;
  state.plan = plan;
  state.cardId = null;

  openDrawer();
  steps(1);
  $("#drawerKicker").textContent = plan.toUpperCase();
  $("#drawerTitle").textContent = plan === "plus" ? "Plus 订阅" : "Pro 订阅";
  $("#drawerSub").textContent = `${a.email} · 正在打开订阅页`;
  $("#priceRow").innerHTML = "";
  $("#drawerShot").removeAttribute("src");
  $("#shotLoading").classList.remove("hidden");
  $("#shotLoading").textContent = "正在打开订阅弹窗…";
  $("#checkoutBox").classList.add("hidden");
  $("#stagePlan").classList.remove("hidden");
  $("#coAmount").textContent = "—";
  $("#coState").className = "pill-state";
  $("#coState").textContent = "等待填写";
  $("#submitBtn").disabled = true;
  $("#vaultCard").innerHTML = vaultOptions(a.id);
  fillCountrySelect($("#addrCountry"), $("#addrCountry").value || "JP");

  busy(btn, true);
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(a.id)}/plan`, {
      method: "POST",
      body: JSON.stringify({ plan }),
    });
    const tags = [];
    if (d.price) tags.push(`<span class="price-tag">参考价 <b>${esc(d.price)}</b>/月</span>`);
    if (d.cta) tags.push(`<span class="price-tag">按钮 <b>${esc(d.cta)}</b></span>`);
    tags.push(`<span class="price-tag">已选中 <b>${plan === "plus" ? "Plus" : "Pro"}</b></span>`);
    $("#priceRow").innerHTML = tags.join("");

    const img = new Image();
    img.onload = () => $("#shotLoading").classList.add("hidden");
    img.src = d.screenshotUrl;
    $("#drawerShot").src = d.screenshotUrl;
    $("#drawerSub").textContent = `${d.email || a.email} · 订阅弹窗已打开`;
    toast(`${plan === "plus" ? "Plus" : "Pro"} 已选中`, "ok");
    await loadAll();
  } catch (e) {
    $("#shotLoading").textContent = e.message;
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

$("#goCheckoutBtn").addEventListener("click", async () => {
  const { acc, plan } = state;
  if (!acc) return;
  const btn = $("#goCheckoutBtn");
  busy(btn, true);
  $("#shotLoading").classList.remove("hidden");
  $("#shotLoading").textContent = "正在进入付款页…";
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(acc.id)}/checkout`, {
      method: "POST",
      body: JSON.stringify({ plan }),
    });
    if (d.screenshotUrl) $("#drawerShot").src = d.screenshotUrl;
    $("#shotLoading").classList.add("hidden");

    if (!d.ok || !d.url) {
      $("#coState").className = "pill-state bad";
      $("#coState").textContent = "没进入付款页";
      $("#coAmount").textContent = "—";
      toast("没进入付款页：可能已经是当前套餐，或按钮不是购买按钮", "bad");
      return;
    }

    $("#coAmount").textContent = d.amount || "—";
    $("#stagePlan").classList.add("hidden");
    $("#checkoutBox").classList.remove("hidden");
    steps(2);
    $("#coState").className = "pill-state " + (d.ready ? "ok" : "");
    $("#coState").textContent = d.ready ? "卡信息字段就绪" : "页面仍在加载";
    $("#drawerSub").textContent = `${d.email || acc.email} · 已进入付款页`;
    toast(d.ready ? "付款页就绪" : "付款页还在加载", d.ready ? "ok" : "bad");
    await loadAll();
  } catch (e) {
    $("#shotLoading").textContent = e.message;
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
});

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
    if (!$("#cardModal").classList.contains("hidden")) closeCardModal();
    else closeDrawer();
  }
});

/* ------------------------------- 代理 ------------------------------- */
async function loadProxy() {
  try {
    const d = await api("/api/proxy");
    $("#proxyInput").value = d.upstream || "";
    $("#relayState").textContent = d.relayAlive ? "中继运行中 · " + d.relayPort : "中继未运行";
    $("#relayState").className = "pill-state " + (d.relayAlive ? "ok" : "bad");
  } catch (e) {
    $("#relayState").textContent = "读取失败";
    $("#relayState").className = "pill-state bad";
  }
}

async function saveProxy(value) {
  try {
    const d = await api("/api/proxy", { method: "POST", body: JSON.stringify({ upstream: value }) });
    $("#proxyInput").value = d.upstream || "";
    $("#proxyHint").textContent = d.upstream ? "已保存，点右侧「重启浏览器并应用」生效" : "已切回直连";
    $("#proxyHint").className = "hint ok";
    toast(d.upstream ? "代理已保存" : "已切回直连", "ok");
    await loadProxy();
  } catch (e) {
    $("#proxyHint").textContent = e.message;
    $("#proxyHint").className = "hint bad";
    toast(e.message, "bad");
  }
}

$("#proxySaveBtn")?.addEventListener("click", () => saveProxy($("#proxyInput").value.trim()));
$("#proxyClearBtn")?.addEventListener("click", () => saveProxy(""));

$("#proxyRestartBtn")?.addEventListener("click", async () => {
  const btn = $("#proxyRestartBtn");
  busy(btn, true);
  try {
    await api("/api/proxy/restart", { method: "POST" });
    toast("浏览器已重启，代理生效", "ok");
    await loadProxy();
    await refreshStatus();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
});

$("#testIpBtn")?.addEventListener("click", async () => {
  const btn = $("#testIpBtn");
  busy(btn, true);
  $("#proxyExit").textContent = "检测中…";
  try {
    const d = await api("/api/proxy/test", { method: "POST" });
    if (!d.ok) throw new Error(d.error || "检测失败");
    $("#proxyExit").textContent = `${d.ip}${d.country ? " · " + d.country : ""} · ${d.ms}ms`;
  } catch (e) {
    $("#proxyExit").textContent = "检测失败：" + e.message;
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
  try {
    const s = await api("/api/session");
    if (s && s.ok) {
      hideLogin(s.user);
      await consumeHashImport().catch(() => {});
      await loadAll();
      if (typeof loadProxy === "function") loadProxy();
    } else {
      showLogin();
    }
  } catch (e) {
    showLogin();
  }
})();
window.addEventListener("hashchange", () => consumeHashImport().catch(() => {}));
setInterval(refreshStatus, 15000);
setInterval(() => { renderAccounts(); }, 60000);
