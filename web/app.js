/* ChatGPT 账号管理台 前端逻辑 */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  accounts: [],
  status: null,
  keyword: "",
  busy: new Set(),
  currentAcc: null,
  currentPlan: null,
};

/* ------------------------------- 基础工具 ------------------------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    data = { error: `HTTP ${res.status}` };
  }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    el.style.transition = "all .25s ease";
    setTimeout(() => el.remove(), 260);
  }, 3600);
}

function busy(el, on) {
  if (!el) return;
  el.classList.toggle("loading", on);
  el.disabled = on;
}

function initials(acc) {
  const src = acc.name || acc.email || "?";
  const s = src.trim();
  if (/[\u4e00-\u9fa5]/.test(s)) return s.slice(0, 2);
  return s.slice(0, 2).toUpperCase();
}

function remain(exp) {
  if (!exp) return "未知";
  const ms = exp * 1000 - Date.now();
  if (ms <= 0) return "已过期";
  const d = Math.floor(ms / 86400000);
  const h = Math.floor((ms % 86400000) / 3600000);
  if (d > 0) return `剩 ${d} 天 ${h} 小时`;
  const m = Math.floor((ms % 3600000) / 60000);
  return `剩 ${h} 小时 ${m} 分`;
}

/* ------------------------------- 渲染 ------------------------------- */
function renderStatus() {
  const b = state.status?.browser || {};
  const pill = $("#browserPill");
  const on = !!b.running;
  pill.classList.toggle("on", on);
  $("#browserText").textContent = on
    ? `浏览器运行中 · ${b.currentEmail || "未登录"}`
    : "浏览器未启动";
}

function cardHtml(acc) {
  const plan = (acc.plan || "free").toLowerCase();
  const planClass = ["free", "go", "plus", "pro", "prolite", "team", "business", "enterprise"].includes(plan)
    ? plan
    : "free";
  const expSoon = acc.accessExp && acc.accessExp * 1000 - Date.now() < 2 * 86400000;
  const statusOk = acc.status === "active";
  const statusText = acc.status === "active" ? "token 有效"
    : acc.status === "invalid" ? (acc.lastError || "token 失效")
    : "未校验";

  const shotPlus = acc.shots?.plus?.price;
  const shotPro = acc.shots?.pro?.price;

  return `
  <article class="card ${acc.isCurrent ? "current" : ""}" data-id="${esc(acc.id)}">
    <div class="card-top">
      <div class="avatar">${esc(initials(acc))}</div>
      <div class="who">
        <div class="email" title="${esc(acc.email)}">${esc(acc.email || "未知邮箱")}</div>
        <div class="alias">${esc(acc.alias || acc.name || "—")}</div>
      </div>
      <span class="badge ${planClass}">${esc(acc.planLabel || plan)}</span>
    </div>

    <div class="meta">
      <span class="sdot ${statusOk ? "ok" : acc.status === "invalid" ? "bad" : ""}"></span>
      <span class="mono ${expSoon ? "warn" : ""}">${esc(remain(acc.accessExp))}</span>
      <span class="sep">·</span>
      <span>到期 ${esc(acc.accessExpText || "—")}</span>
    </div>

    <div class="tokens">
      <button class="chip" data-act="copy" data-type="access" title="点击复制 accessToken">
        <span class="k">access</span>
        <span class="v">${esc(acc.accessTokenMasked)}</span>
        <span class="copy">复制</span>
      </button>
      ${acc.hasSession
        ? `<button class="chip" data-act="copy" data-type="session" title="点击复制 sessionToken">
             <span class="k">session</span>
             <span class="v">${esc(acc.sessionTokenMasked)}</span>
             <span class="copy">复制</span>
           </button>`
        : `<span class="chip missing"><span class="k">session</span><span class="v">缺失 · 无法浏览器登录</span></span>`}
    </div>

    <div class="actions">
      <div class="act-row">
        <button class="btn" data-act="login" ${acc.hasSession ? "" : "disabled"}>一键登录</button>
        <span class="spacer"></span>
        <button class="btn icon" data-act="check" title="重新校验 token">⟳</button>
        <button class="btn icon danger" data-act="del" title="删除账号">✕</button>
      </div>
      <div class="act-row plans">
        <button class="btn plan plus" data-act="plan" data-plan="plus" ${acc.hasSession ? "" : "disabled"}>
          Plus<span class="price">${shotPlus ? esc(shotPlus) : ""}</span>
        </button>
        <button class="btn plan pro" data-act="plan" data-plan="pro" ${acc.hasSession ? "" : "disabled"}>
          Pro<span class="price">${shotPro ? esc(shotPro) : ""}</span>
        </button>
      </div>
    </div>
  </article>`;
}

function render() {
  const kw = state.keyword.trim().toLowerCase();
  const list = state.accounts.filter((a) => {
    if (!kw) return true;
    return `${a.email} ${a.alias} ${a.name} ${a.plan}`.toLowerCase().includes(kw);
  });

  $("#count").textContent = state.accounts.length;
  $("#accounts").innerHTML = list.map(cardHtml).join("");
  $("#empty").classList.toggle("hidden", state.accounts.length > 0);
  if (state.accounts.length && !list.length) {
    $("#accounts").innerHTML = `<p class="empty">没有匹配的账号</p>`;
  }
  renderStatus();
}

/* ------------------------------- 数据 ------------------------------- */
async function loadAll() {
  const data = await api("/api/accounts");
  state.accounts = data.accounts || [];
  state.status = data.status;
  render();
}

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
    renderStatus();
    const cur = state.status?.browser?.currentId;
    let changed = false;
    state.accounts.forEach((a) => {
      const isCur = a.id === cur;
      if (a.isCurrent !== isCur) { a.isCurrent = isCur; changed = true; }
    });
    if (changed) render();
  } catch (e) { /* 静默 */ }
}

/* ------------------------------- 交互 ------------------------------- */
async function importAccount() {
  const raw = $("#rawInput").value.trim();
  const alias = $("#aliasInput").value.trim();
  const log = $("#importLog");
  if (!raw) return toast("先粘贴 token 再导入", "bad");

  busy($("#importBtn"), true);
  log.innerHTML = "";
  try {
    const data = await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({ raw, alias }),
    });
    const results = data.results || [];
    const ok = results.filter((r) => r.ok).length;
    log.innerHTML = results
      .map((r) => r.ok
        ? `<div class="ok">✓ 已导入 ${esc(r.account.email)}（${esc(r.account.planLabel)}，${esc(remain(r.account.accessExp))}）</div>`
        : `<div class="bad">✗ 导入失败：${esc(r.error || "未知错误")}</div>`)
      .join("");
    if (ok) {
      $("#rawInput").value = "";
      $("#aliasInput").value = "";
      toast(`导入成功 ${ok} 个账号`, "ok");
    } else {
      toast("导入失败，检查 token 是否完整", "bad");
    }
    await loadAll();
  } catch (e) {
    toast(e.message, "bad");
    log.innerHTML = `<div class="bad">✗ ${esc(e.message)}</div>`;
  } finally {
    busy($("#importBtn"), false);
  }
}

async function loginAccount(id, btn) {
  busy(btn, true);
  toast("正在启动浏览器并注入会话…");
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(id)}/login`, { method: "POST" });
    toast(`已登录 ${data.email}`, "ok");
    state.status = data.status;
    await loadAll();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

async function openPlan(acc, plan, btn) {
  state.currentAcc = acc;
  state.currentPlan = plan;
  $("#checkoutBox").classList.add("hidden");
  $("#stagePlan").classList.remove("hidden");
  $("#planNote").classList.remove("hidden");
  $("#coAmount").textContent = "—";
  $("#coState").className = "co-state";
  $("#coState").textContent = "等待填写卡信息";
  $("#submitBtn").disabled = true;
  $("#submitBtn").textContent = "提交订阅";

  const drawer = $("#drawer");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#drawerTitle").textContent = plan === "plus" ? "Plus 订阅" : "Pro 订阅";
  $("#drawerSub").textContent = `${acc.email} · 正在打开订阅页并选中套餐`;
  $("#priceRow").innerHTML = "";
  $("#drawerShot").removeAttribute("src");
  $("#shotLoading").classList.remove("hidden");
  $("#shotLoading").textContent = acc.isCurrent ? "正在打开订阅弹窗…" : "正在登录并打开订阅弹窗…";

  busy(btn, true);
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(acc.id)}/plan`, {
      method: "POST",
      body: JSON.stringify({ plan }),
    });
    state.status = data.status;

    const tags = [];
    if (data.price) tags.push(`<span class="price-tag">参考价 <b>${esc(data.price)}</b>/月</span>`);
    if (data.cta) tags.push(`<span class="price-tag">按钮状态 <b>${esc(data.cta)}</b></span>`);
    tags.push(`<span class="price-tag">套餐 <b>${esc(plan === "plus" ? "Plus" : "Pro")}</b> 已选中</span>`);
    $("#priceRow").innerHTML = tags.join("");

    if (data.screenshotUrl) {
      const img = new Image();
      img.onload = () => $("#shotLoading").classList.add("hidden");
      img.onerror = () => { $("#shotLoading").textContent = "截图加载失败"; };
      img.src = data.screenshotUrl;
      $("#drawerShot").src = data.screenshotUrl;
    } else {
      $("#shotLoading").textContent = "没有拿到截图";
    }
    $("#drawerSub").textContent = `${data.email || acc.email} · 已打开 chatgpt.com 订阅页`;
    toast(`${plan === "plus" ? "Plus" : "Pro"} 已选中，去浏览器窗口确认付款`, "ok");
    await loadAll();
  } catch (e) {
    $("#shotLoading").textContent = e.message;
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

async function checkAccount(id, btn) {
  busy(btn, true);
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(id)}/check`, { method: "POST" });
    const acc = data.account;
    const idx = state.accounts.findIndex((a) => a.id === id);
    if (idx >= 0) state.accounts[idx] = acc;
    render();
    toast(acc.status === "active" ? "token 有效" : `校验失败：${acc.lastError}`, acc.status === "active" ? "ok" : "bad");
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

async function deleteAccount(id, acc) {
  if (!confirm(`确定删除 ${acc.email} ？只删本地记录，不影响账号本身。`)) return;
  try {
    await api(`/api/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("已删除", "ok");
    await loadAll();
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function copyToken(acc, type, chip) {
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(acc.id)}/token?type=${type}`);
    await navigator.clipboard.writeText(data.token || "");
    const tip = chip.querySelector(".copy");
    const old = tip.textContent;
    tip.textContent = "已复制";
    setTimeout(() => (tip.textContent = old), 1200);
  } catch (e) {
    toast(`复制失败：${e.message}`, "bad");
  }
}

/* ------------------------------- 事件绑定 ------------------------------- */
$("#importBtn").addEventListener("click", importAccount);
$("#rawInput").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") importAccount();
});
$("#refreshBtn").addEventListener("click", async () => {
  await loadAll();
  toast("已刷新", "ok");
});
$("#startBrowserBtn").addEventListener("click", async () => {
  try {
    toast("正在启动浏览器…");
    const data = await api("/api/browser/start", { method: "POST" });
    state.status = data.status;
    renderStatus();
    toast("浏览器已就绪", "ok");
  } catch (e) {
    toast(e.message, "bad");
  }
});
$("#search").addEventListener("input", (e) => {
  state.keyword = e.target.value;
  render();
});
$("#drawerClose").addEventListener("click", () => {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
});

$("#accounts").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const card = e.target.closest(".card");
  const acc = state.accounts.find((a) => a.id === card?.dataset.id);
  if (!acc) return;
  const act = btn.dataset.act;

  if (act === "copy") return copyToken(acc, btn.dataset.type, btn);
  if (act === "login") return loginAccount(acc.id, btn);
  if (act === "plan") return openPlan(acc, btn.dataset.plan, btn);
  if (act === "check") return checkAccount(acc.id, btn);
  if (act === "del") return deleteAccount(acc.id, acc);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#drawerClose").click();
});

/* ------------------------------- 结账 & 卡信息 ------------------------------- */
function setCoState(text, kind = "") {
  const el = $("#coState");
  el.textContent = text;
  el.className = "co-state" + (kind ? " " + kind : "");
}

async function goCheckout() {
  const acc = state.currentAcc;
  const plan = state.currentPlan;
  if (!acc || !plan) return toast("先从卡片上选 Plus / Pro", "bad");

  const btn = $("#goCheckoutBtn");
  busy(btn, true);
  $("#shotLoading").classList.remove("hidden");
  $("#shotLoading").textContent = "正在进入付款页…";
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(acc.id)}/checkout`, {
      method: "POST",
      body: JSON.stringify({ plan }),
    });
    if (data.screenshotUrl) $("#drawerShot").src = data.screenshotUrl;
    $("#shotLoading").classList.add("hidden");
    $("#coAmount").textContent = data.amount || "—";
    $("#stagePlan").classList.add("hidden");
    $("#planNote").classList.add("hidden");
    $("#checkoutBox").classList.remove("hidden");
    $("#drawerSub").textContent = `${data.email || acc.email} · 已进入付款页`;
    setCoState(data.ready ? "卡信息字段已就绪" : "结账页仍在加载", data.ready ? "ok" : "");
    if (data.ready) $("#cardNumber").focus();
    toast(data.ready ? "付款页已就绪，填写卡信息" : "付款页还在加载，稍后再试", data.ready ? "ok" : "bad");
    await loadAll();
  } catch (e) {
    $("#shotLoading").textContent = e.message;
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

async function fillCard() {
  const acc = state.currentAcc;
  if (!acc) return;
  const number = $("#cardNumber").value.replace(/\D/g, "");
  const exp = $("#cardExp").value.replace(/\D/g, "");
  const cvc = $("#cardCvc").value.replace(/\D/g, "");

  if (number.length < 13) return toast("卡号不完整", "bad");
  if (exp.length !== 4) return toast("有效期请填 MM/YY", "bad");
  if (cvc.length < 3) return toast("安全码不完整", "bad");

  const btn = $("#fillCardBtn");
  busy(btn, true);
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(acc.id)}/card`, {
      method: "POST",
      body: JSON.stringify({ number, exp, cvc }),
    });
    setCoState(`已填入 ${data.count} 项 · ${data.fields.number}`, data.count === 3 ? "ok" : "bad");
    $("#submitBtn").disabled = data.count !== 3;
    toast("卡信息已填入浏览器，请核对后提交", "ok");
  } catch (e) {
    setCoState("填充失败", "bad");
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

async function submitCheckout() {
  const acc = state.currentAcc;
  if (!acc) return;
  const amount = $("#coAmount").textContent.trim();
  const ok = confirm(
    `确认提交订阅？\n\n账号：${acc.email}\n金额：${amount}\n\n` +
    `点击「确定」会真实扣款，无法撤回。`
  );
  if (!ok) return;

  const btn = $("#submitBtn");
  busy(btn, true);
  setCoState("提交中…");
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(acc.id)}/subscribe`, {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    });
    const done = !data.pending;
    setCoState(done ? "已提交，见窗口提示" : "等待银行/验证处理", done ? "ok" : "");
    $("#drawerSub").textContent = `${acc.email} · ${data.url || ""}`.slice(0, 90);
    toast(done ? "已提交，去窗口确认结果" : "已提交，可能需要 3DS 验证", done ? "ok" : "");
    if (data.text) console.info("[subscribe]", data.text.slice(0, 300));
    await loadAll();
  } catch (e) {
    setCoState("提交失败", "bad");
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

$("#goCheckoutBtn").addEventListener("click", goCheckout);
$("#fillCardBtn").addEventListener("click", fillCard);
$("#submitBtn").addEventListener("click", submitCheckout);

/* 卡号/有效期格式化（只影响输入框显示） */
$("#cardNumber").addEventListener("input", (e) => {
  const d = e.target.value.replace(/\D/g, "").slice(0, 19);
  e.target.value = d.replace(/(.{4})/g, "$1 ").trim();
});
$("#cardExp").addEventListener("input", (e) => {
  const d = e.target.value.replace(/\D/g, "").slice(0, 4);
  e.target.value = d.length > 2 ? d.slice(0, 2) + "/" + d.slice(2) : d;
});
$("#cardCvc").addEventListener("input", (e) => {
  e.target.value = e.target.value.replace(/\D/g, "").slice(0, 4);
});

/* ------------------------------- 账单地址 ------------------------------- */
async function revealBilling() {
  const acc = state.currentAcc;
  if (!acc) return;
  const btn = $("#revealBtn");
  busy(btn, true);
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(acc.id)}/reveal`, { method: "POST" });
    if (data.screenshotUrl) $("#drawerShot").src = data.screenshotUrl;
    setCoState(data.revealed ? "账单地址已展开" : "没展开，检查页面", data.revealed ? "ok" : "bad");
    toast(data.revealed ? "账单地址区块已展开，去填写" : "没检测到地址区块", data.revealed ? "ok" : "bad");
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

async function fillAddress() {
  const acc = state.currentAcc;
  if (!acc) return;
  const body = {
    name: $("#addrName").value.trim(),
    country: $("#addrCountry").value,
    postal: $("#addrPostal").value.trim(),
    state: $("#addrState").value.trim(),
    city: $("#addrCity").value.trim(),
    line1: $("#addrLine1").value.trim(),
  };
  if (!body.name) return toast("填持卡人姓名", "bad");
  if (!body.postal) return toast("填邮编", "bad");
  if (!body.line1) return toast("填地址", "bad");

  const btn = $("#fillAddrBtn");
  busy(btn, true);
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(acc.id)}/address`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (data.screenshotUrl) $("#drawerShot").src = data.screenshotUrl;
    const filled = Object.values(data.values || {}).filter(Boolean).length;
    setCoState(`地址已填 ${filled} 项`, "ok");
    toast("账单地址已填入，确认无误后提交订阅", "ok");
  } catch (e) {
    setCoState("地址填写失败", "bad");
    toast(e.message, "bad");
  } finally {
    busy(btn, false);
  }
}

$("#revealBtn").addEventListener("click", revealBilling);
$("#fillAddrBtn").addEventListener("click", fillAddress);

/* ------------------------------- 启动 ------------------------------- */
loadAll().catch((e) => toast(`加载失败：${e.message}`, "bad"));
setInterval(refreshStatus, 15000);
setInterval(() => {
  if (state.accounts.length) render();
}, 60000);
