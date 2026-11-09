/* ============================================================
   客户付款开通页（支付宝 / 微信）
   ============================================================ */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  products: [],        // [{id,name,plan,priceYuan}]
  channels: [],        // ["alipay","wechat"]
  productId: null,
  plan: null,
  channel: null,
  token: null,
  orderTimer: null,
  jobId: null,
  jobTimer: null,
  session: null,
};

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let data = {};
  try { data = await res.json(); } catch (e) { data = { error: `HTTP ${res.status}` }; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------- 套餐 ------------------------------- */
async function loadProducts() {
  let d;
  try { d = await api("/api/pay/products"); }
  catch (e) { d = { products: [], channels: [], enabled: false }; }
  state.products = d.products || [];
  state.channels = d.channels || [];
  renderPlans();
  // 默认选中「推荐」的卡片（没有就选第一张），右侧不再是空的
  const pre = $("#planCols .plan-col.recommend") || $("#planCols .plan-col");
  if (pre) selectCol(pre);
  renderChannels(d.enabled);
}

// 后台配置的套餐：标题相同的合成一张卡，档位（variant）做切换
function groupProducts() {
  const groups = [];
  const byTitle = {};
  state.products.forEach((p) => {
    if (!byTitle[p.title]) { byTitle[p.title] = []; groups.push(byTitle[p.title]); }
    byTitle[p.title].push(p);
  });
  return groups;
}

function bulletsHtml(p) {
  return (p.bullets || []).map((b) => `<li>${esc(b)}</li>`).join("");
}

// 切到某个档位：价格 / 副标题 / 说明 / 权益都跟着换
function fillCol(col, p) {
  col.dataset.pid = p.id;
  $(".pv-price", col).textContent = p.priceYuan;
  $(".plan-sub", col).textContent = p.subtitle || "";
  $(".plan-desc", col).textContent = p.desc || "";
  $(".plan-desc", col).classList.toggle("hidden", !p.desc);
  $(".plan-bullets", col).innerHTML = bulletsHtml(p);
  col.classList.toggle("recommend", !!p.recommend);
}

function renderPlans() {
  const groups = groupProducts();
  $("#planCols").innerHTML = groups.map((items, gi) => {
    const first = items[0];
    const chips = items.length > 1
      ? `<div class="plan-variants">${items.map((p, i) =>
          `<button class="pv ${i === 0 ? "on" : ""}" data-pid="${esc(p.id)}">${esc(p.variant || p.priceYuan)}</button>`).join("")}</div>`
      : "";
    return `<div class="plan-col ${first.recommend ? "recommend" : ""}" data-g="${gi}" data-pid="${esc(first.id)}">
      <div class="plan-top">
        <div class="plan-name">${esc(first.title)}</div>
        ${chips}
      </div>
      <div class="plan-sub">${esc(first.subtitle || "")}</div>
      <div class="plan-desc ${first.desc ? "" : "hidden"}">${esc(first.desc || "")}</div>
      <button class="btn plan-pick">选择此套餐</button>
      <div class="plan-price"><span class="cur">¥</span><b class="pv-price">${esc(first.priceYuan)}</b><span class="per">/月</span></div>
      <div class="plan-price-note">人民币计价 · 以收银台最终金额为准</div>
      <ul class="plan-bullets">${bulletsHtml(first)}</ul>
    </div>`;
  }).join("") || '<div class="pay-note">暂未上架套餐</div>';
  $("#planCols").classList.toggle("one", groups.length === 1);
  $("#planCols").classList.toggle("three", groups.length >= 3);

  // 档位切换
  $$("#planCols .pv").forEach((chip) => chip.addEventListener("click", (e) => {
    e.stopPropagation();
    const col = chip.closest(".plan-col");
    $$(".pv", col).forEach((x) => x.classList.toggle("on", x === chip));
    const p = state.products.find((x) => x.id === chip.dataset.pid);
    if (!p) return;
    fillCol(col, p);
    selectCol(col);
  }));
  $$("#planCols .plan-col").forEach((col) => {
    const pick = () => selectCol(col);
    col.addEventListener("click", pick);
    $(".plan-pick", col).addEventListener("click", (e) => { e.stopPropagation(); pick(); });
  });
}

function selectCol(col) {
  $$("#planCols .plan-col").forEach((c) => c.classList.toggle("on", c === col));
  selectProduct(col.dataset.pid);
}

function selectProduct(pid) {
  const p = state.products.find((x) => x.id === pid);
  if (!p) return;
  state.productId = p.id;
  state.plan = p.plan;
  $("#chosenBox").innerHTML = `<div class="chosen-row"><b>${esc(p.name || p.title)}</b><span>¥${esc(p.priceYuan)}</span></div>`;
  refreshPayBtn();
}

function renderChannels(enabled) {
  if (!state.channels.length) {
    $("#payDisabled").classList.remove("hidden");
  }
  $$(".pay-chan").forEach((el) => {
    const ok = state.channels.includes(el.dataset.ch);
    el.classList.toggle("disabled", !ok);
    $("input", el).disabled = !ok;
  });
  if (state.channels.length === 1) {
    const only = $(`.pay-chan[data-ch="${state.channels[0]}"] input`);
    if (only) { only.checked = true; state.channel = state.channels[0]; }
  }
  refreshPayBtn();
}

$$('input[name="chan"]').forEach((r) => r.addEventListener("change", () => {
  state.channel = r.value; refreshPayBtn();
}));
["payEmail", "payContact"].forEach((id) => $("#" + id).addEventListener("input", refreshPayBtn));

function refreshPayBtn() {
  const ok = state.productId && state.channel && $("#payEmail").value.trim() && $("#payContact").value.trim();
  $("#payBtn").disabled = !ok;
}

/* ------------------------------- 下单 + 扫码 ------------------------------- */
function payHint(msg, kind = "") { const el = $("#payHint"); el.textContent = msg || ""; el.className = `hint ${kind}`; }

async function pay() {
  const email = $("#payEmail").value.trim();
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return payHint("请填写正确的充值邮箱", "bad");
  const btn = $("#payBtn"); btn.classList.add("loading"); btn.disabled = true;
  payHint("");
  try {
    const d = await api("/api/pay/create", { method: "POST", body: JSON.stringify({
      channel: state.channel, productId: state.productId, email, contact: $("#payContact").value.trim(),
    }) });
    state.token = d.token;
    $("#qrTitle").textContent = (state.channel === "alipay" ? "支付宝" : "微信") + "扫码支付";
    $("#qrAmount").textContent = "¥" + d.amountYuan;
    $("#qrImg").src = d.qrImg || "";
    $("#qrTip").textContent = d.qrImg
      ? "请用手机打开对应 App 扫码支付，支付成功后自动继续。"
      : "二维码生成失败，请联系客服（订单号 " + d.outTradeNo + "）。";
    $("#qrOverlay").classList.remove("hidden");
    startOrderPoll();
  } catch (e) {
    payHint(e.message, "bad");
  } finally {
    btn.classList.remove("loading"); refreshPayBtn();
  }
}
$("#payBtn").addEventListener("click", pay);

function stopOrderPoll() { if (state.orderTimer) { clearInterval(state.orderTimer); state.orderTimer = null; } }
function startOrderPoll() {
  stopOrderPoll();
  state.orderTimer = setInterval(async () => {
    try {
      const o = await api(`/api/pay/order/${state.token}`);
      if (o.paid) { stopOrderPoll(); onPaid(); }
      else if (o.status === "expired") { stopOrderPoll(); $("#qrPoll").innerHTML = "二维码已过期，请关闭后重试。"; }
    } catch (e) { /* 继续轮询 */ }
  }, 3000);
}
$("#qrClose").addEventListener("click", () => { $("#qrOverlay").classList.add("hidden"); stopOrderPoll(); });

/* ------------------------------- 付款成功 → 开通 ------------------------------- */
function onPaid() {
  $("#qrOverlay").classList.add("hidden");
  $("#actStage").classList.remove("hidden");
  $("#actProcessing").classList.add("hidden");
  $("#actResult").classList.add("hidden");
  $("#actOverlay").classList.remove("hidden");
}

function parseSession(text) {
  const t = (text || "").trim();
  if (!t) return { empty: true };
  const a = t.indexOf("{"), b = t.lastIndexOf("}");
  if (a < 0 || b <= a) return { error: "没识别到登录信息，请复制「登录信息页面」的全部内容" };
  let obj; try { obj = JSON.parse(t.slice(a, b + 1)); }
  catch (e) { return { error: "内容不完整，请回到那个页面全选后重新复制" }; }
  if (!obj || !Object.keys(obj).length) return { error: "粘贴的是 {}：这个浏览器还没登录 ChatGPT" };
  if (!obj.accessToken) return { error: "内容里没有登录凭证，请确认打开的是 /api/auth/session" };
  if (!obj.sessionToken) return { error: "登录信息不完整（缺少 sessionToken），请复制全部内容" };
  const exp = obj.expires ? Date.parse(obj.expires) : NaN;
  if (!Number.isNaN(exp) && exp < Date.now()) return { error: "登录信息已过期，请刷新那个页面后重新复制" };
  return { raw: JSON.stringify(obj), email: obj.user?.email || "" };
}

function validateAct() {
  const r = parseSession($("#actSession").value);
  const box = $("#actStatus");
  state.session = r.raw ? r : null;
  $("#actBtn").disabled = !state.session;
  $("#ags3").classList.toggle("done", !!state.session);
  if (r.empty) { box.className = "session-status"; box.innerHTML = ""; }
  else if (r.error) { box.className = "session-status bad"; box.textContent = r.error; }
  else {
    $("#ags1").classList.add("done"); $("#ags2").classList.add("done");
    box.className = "session-status ok";
    box.innerHTML = `<b>${esc(r.email || "已识别登录信息")}</b><span>请确认这是要开通的账号。</span>`;
  }
}
$("#actSession").addEventListener("input", validateAct);

async function activate() {
  validateAct();
  if (!state.session) return;
  const btn = $("#actBtn"); btn.classList.add("loading"); btn.disabled = true;
  try {
    const d = await api("/api/pay/activate", { method: "POST", body: JSON.stringify({
      token: state.token, session: state.session.raw }) });
    state.jobId = d.jobId;
    $("#actStage").classList.add("hidden");
    $("#actProcessing").classList.remove("hidden");
    startJobPoll();
  } catch (e) {
    const box = $("#actStatus"); box.className = "session-status bad"; box.textContent = e.message;
    btn.classList.remove("loading"); btn.disabled = false;
  }
}
$("#actBtn").addEventListener("click", activate);

function friendlyError(raw) {
  const m = String(raw || "").toLowerCase();
  if (/过期|expired|登录|session|401/.test(m)) return "登录信息可能已失效，请重新获取后再试。";
  if (/库存|客服/.test(m)) return raw;
  if (/当前套餐|已经是|already/.test(m)) return "这个账号可能已经是该套餐了，请确认后再试。";
  return "开通未成功，请凭下单联系方式联系客服处理，款项不受影响。";
}

function stopJobPoll() { if (state.jobTimer) { clearInterval(state.jobTimer); state.jobTimer = null; } }
function startJobPoll() {
  stopJobPoll();
  const tick = async () => {
    try {
      const job = await api(`/api/job/${state.jobId}`);
      if (job.done) {
        stopJobPoll();
        $("#actProcessing").classList.add("hidden");
        const box = $("#actResult"); box.classList.remove("hidden", "ok", "bad");
        box.classList.add(job.ok ? "ok" : "bad");
        box.innerHTML = job.ok
          ? "<b>开通成功 🎉</b><br>订阅已提交，请回到 ChatGPT 查看。如银行要求验证请按提示完成。"
          : `<b>暂未开通</b><br>${esc(friendlyError(job.message))}`;
      }
    } catch (e) { /* 继续 */ }
  };
  state.jobTimer = setInterval(tick, 1500); tick();
}
$("#actClose").addEventListener("click", () => {
  if (state.jobId && !$("#actResult").classList.contains("ok")) {
    if (!confirm("开通可能还在进行，确定关闭？订单号已保存，可联系客服。")) return;
  }
  $("#actOverlay").classList.add("hidden"); stopJobPoll();
});

loadProducts();
