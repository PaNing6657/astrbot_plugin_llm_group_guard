/* LLM 群守卫 WebUI */
const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

/* ---------- 配置字段定义 ---------- */
const CONFIG_FIELDS = [
  { key: "llm_base_url", label: "LLM 接口地址", type: "text", full: true, hint: "OpenAI 规范 /v1 地址，如 https://api.deepseek.com/v1" },
  { key: "llm_api_key", label: "LLM API Key", type: "password", full: true },
  { key: "llm_model", label: "主模型", type: "text", hint: "如 deepseek-chat" },
  { key: "llm_timeout", label: "请求超时（秒）", type: "number" },
  { key: "llm_max_tokens", label: "输出上限（token）", type: "number", hint: "思考型模型可调大" },
  { key: "llm_fallback_base_urls", label: "备用接口（逗号分隔）", type: "csv", full: true, hint: "与备用 Key/模型按索引一一对应" },
  { key: "llm_fallback_api_keys", label: "备用 Key（逗号分隔）", type: "csv", full: true },
  { key: "llm_fallback_models", label: "备用模型（逗号分隔）", type: "csv", full: true },
  { key: "guard_enable", label: "群消息违规审核", type: "toggle", hint: "关闭后 LLM 审核不生效" },
  { key: "guard_action", label: "违规处置方式", type: "select", options: ["ban", "recall", "recall_and_ban"], hint: "ban=禁言 recall=撤回 recall_and_ban=撤回并禁言" },
  { key: "guard_ban_seconds", label: "基础禁言时长（秒）", type: "text", hint: "阶梯第一档，支持 30-120 随机范围" },
  { key: "guard_stair_enable", label: "阶梯禁言", type: "toggle", hint: "违规次数越多禁言越久" },
  { key: "guard_stair_multiplier", label: "阶梯倍数", type: "number" },
  { key: "guard_stair_max_seconds", label: "禁言封顶（秒）", type: "number" },
  { key: "guard_recall_ban_threshold", label: "撤回 N 次自动禁言", type: "number", hint: "仅 recall 模式生效，0=关闭" },
  { key: "guard_interval", label: "审核间隔（秒）", type: "number", hint: "0=每条都审" },
  { key: "guard_risk_as_violation", label: "风控拦截视为违规", type: "toggle" },
  { key: "guard_prompt", label: "审核要求（自定义）", type: "textarea", full: true, hint: "写清本群禁止内容，LLM 侧重审核" },
  { key: "guard_notice", label: "违规通知消息", type: "text", full: true, hint: "支持 {user_id} {duration} {count} 占位符，留空不发送" },
  { key: "keyword_guard_enable", label: "关键词检测", type: "toggle", hint: "命中关键词即判违规，机制同 LLM 审核但独立计数" },
  { key: "keyword_list", label: "违规关键词（逗号分隔）", type: "csv", full: true, hint: "消息包含任一关键词即判违规" },
  { key: "user_whitelist", label: "用户白名单（逗号分隔）", type: "csv", full: true },
  { key: "whole_ban_enable_msg", label: "开启禁言通知", type: "text", full: true, hint: "支持 {start_time} {end_time}" },
  { key: "whole_ban_disable_msg", label: "解除禁言通知", type: "text", full: true },
  { key: "Permission_verification", label: "权限验证", type: "toggle", hint: "全体禁言等操作校验操作者权限" },
  { key: "allow_groupadmin_use", label: "允许群主/管理员使用", type: "toggle" },
];

// 兼容中英文逗号分割
const toCsv = (v) => (Array.isArray(v) ? v.join(", ") : v ?? "");
const fromCsv = (s) => String(s || "").split(/[,，]/).map((x) => x.trim()).filter(Boolean);

let config = {};

/* ---------- bridge 封装 ---------- */
async function api(path, method = "GET", data) {
  return method === "GET" ? bridge.apiGet(path, data || {}) : bridge.apiPost(path, data || {});
}
function toast(el, msg, isErr = false) {
  const t = $(el);
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.textContent = ""), 2600);
}

/* ---------- tab 切换 ---------- */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("page-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "violations") loadViolations();
    if (btn.dataset.tab === "schedules") loadSchedules();
    if (btn.dataset.tab === "join") loadJoin();
    if (btn.dataset.tab === "groups") loadGroups();
  });
});

/* ---------- 配置表单 ---------- */
function renderConfigForm() {
  const wrap = $("configForm");
  wrap.innerHTML = '<div class="form-grid"></div>';
  const grid = wrap.firstElementChild;
  CONFIG_FIELDS.forEach((f) => {
    const el = document.createElement("div");
    el.className = "field" + (f.full ? " full" : "");
    const val = config[f.key];
    if (f.type === "toggle") {
      el.innerHTML =
        `<div class="toggle-row"><div><div class="t-label">${f.label}</div>` +
        (f.hint ? `<div class="t-hint">${f.hint}</div>` : "") + `</div>` +
        `<div class="toggle ${val ? "on" : ""}" data-key="${f.key}"></div></div>`;
      el.querySelector(".toggle").addEventListener("click", (e) => {
        e.currentTarget.classList.toggle("on");
      });
    } else if (f.type === "select") {
      el.innerHTML = `<label>${f.label}</label><select data-key="${f.key}">` +
        f.options.map((o) => `<option value="${o}" ${String(val) === o ? "selected" : ""}>${o}</option>`).join("") +
        "</select>" + (f.hint ? `<div class="hint">${f.hint}</div>` : "");
    } else if (f.type === "textarea") {
      el.innerHTML = `<label>${f.label}</label><textarea data-key="${f.key}">${val ?? ""}</textarea>` +
        (f.hint ? `<div class="hint">${f.hint}</div>` : "");
    } else {
      const isCsv = f.type === "csv";
      const inputType = f.type === "password" ? "password" : "text";
      el.innerHTML = `<label>${f.label}</label><input type="${inputType}" data-key="${f.key}" class="${isCsv ? "csv" : ""}" value="${escapeHtml(isCsv ? toCsv(val) : (val ?? ""))}">` +
        (f.hint ? `<div class="hint">${f.hint}</div>` : "");
    }
    grid.appendChild(el);
  });
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function collectConfig() {
  const out = {};
  document.querySelectorAll("#configForm [data-key]").forEach((node) => {
    const key = node.dataset.key;
    if (node.classList.contains("toggle")) out[key] = node.classList.contains("on");
    else if (node.tagName === "TEXTAREA") out[key] = node.value;
    else if (node.tagName === "SELECT") out[key] = node.value;
    else if (node.classList.contains("csv")) out[key] = fromCsv(node.value);
    else out[key] = node.value;
  });
  return out;
}

$("saveConfig").addEventListener("click", async () => {
  const btn = $("saveConfig");
  btn.disabled = true;
  try {
    await api("config/save", "POST", collectConfig());
    toast("configToast", "已保存");
  } catch (e) {
    toast("configToast", "保存失败：" + e, true);
  }
  btn.disabled = false;
});

/* ---------- 违规记录 ---------- */
let violationLog = []; // 后端违规消息日志

function fmtLogTs(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderViolations(data) {
  // 数据结构：{llm: {gid: {uid: count}}, keyword: {gid: {uid: count}}, log: [...]}
  violationLog = data.log || [];
  const rows = [];
  const collect = (source, type) =>
    Object.entries(data[source] || {}).forEach(([gid, members]) => {
      Object.entries(members).forEach(([uid, count]) => rows.push({ gid, uid, count, type }));
    });
  collect("llm", "llm");
  collect("keyword", "keyword");
  const tbody = $("violationBody");
  $("violationEmpty").classList.toggle("hidden", rows.length > 0);
  tbody.innerHTML = "";
  rows.forEach(({ gid, uid, count, type }) => {
    const typeBadge = type === "keyword"
      ? '<span class="badge green">关键词</span>'
      : '<span class="badge blue">LLM</span>';
    // 该用户此来源最近一条违规消息
    const last = violationLog.find((e) => e.gid === gid && e.uid === uid && e.source === type);
    const lastText = last ? escapeHtml(last.text) : "—";
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${gid}</td><td>${uid}</td>` +
      `<td>${typeBadge}</td>` +
      `<td><span class="badge ${count >= 3 ? "red" : count >= 2 ? "warn" : "blue"}">${count} 次</span></td>` +
      `<td class="msg-cell" title="${lastText}">${lastText}</td>` +
      `<td>` +
      `<button class="btn ghost sm" data-act="log" data-g="${gid}" data-u="${uid}">记录</button> ` +
      `<button class="btn ghost sm" data-act="reset" data-g="${gid}" data-u="${uid}" data-t="${type}">清零</button>` +
      `</td>`;
    tr.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        const b = e.currentTarget;
        if (b.dataset.act === "log") {
          showViolationHistory(b.dataset.g, b.dataset.u);
        } else {
          await api("violations/reset", "POST", {
            group_id: b.dataset.g,
            user_id: b.dataset.u,
            type: b.dataset.t,
          });
          loadViolations();
        }
      });
    });
    tbody.appendChild(tr);
  });
}

function showViolationHistory(gid, uid) {
  // 弹窗展示该群该用户全部违规消息记录
  const entries = violationLog.filter((e) => e.gid === gid && e.uid === uid);
  const list = $("historyList");
  $("historyTitle").textContent = `违规记录 · 群 ${gid} · 用户 ${uid}`;
  list.innerHTML = entries.length
    ? entries.map((e) => {
        const src = e.source === "keyword"
          ? '<span class="badge green">关键词</span>'
          : '<span class="badge blue">LLM</span>';
        return (
          `<div class="history-item">` +
          `<div class="history-meta">${src}<span>${fmtLogTs(e.ts)}</span><span class="history-reason">${escapeHtml(e.reason)}</span></div>` +
          `<div class="history-text">${escapeHtml(e.text)}</div>` +
          `</div>`
        );
      }).join("")
    : '<div class="empty">该用户暂无消息记录</div>';
  $("historyModal").classList.add("show");
}

function closeHistoryModal() {
  $("historyModal").classList.remove("show");
}

async function loadViolations() {
  const data = await api("violations");
  renderViolations(data);
}

$("resetAllViolations").addEventListener("click", async () => {
  await api("violations/reset", "POST", {});
  loadViolations();
  toast("configToast", "违规记录已全部清零");
});

/* 违规历史弹窗关闭：点按钮或遮罩 */
$("historyClose").addEventListener("click", closeHistoryModal);
$("historyModal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeHistoryModal();
});

/* ---------- 定时禁言 ---------- */
function fmtTs(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function describeSchedule(s) {
  if (s.mode === "weekly") {
    const rules = s.rules || {};
    const days = Object.keys(rules).sort().map((d) => {
      const r = rules[d];
      const sm = r.start_min, dm = r.duration_min;
      const names = { 1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日" };
      const em = (sm + dm) % 1440;
      return `周${names[d]} ${String(Math.floor(sm / 60)).padStart(2, "0")}:${String(sm % 60).padStart(2, "0")}~${String(Math.floor(em / 60)).padStart(2, "0")}:${String(em % 60).padStart(2, "0")}`;
    });
    return days.join(" · ");
  }
  const type = s.mode === "daily" ? "每日" : "单次";
  return `${type} ${fmtTs(s.start_ts)} → ${fmtTs(s.end_ts)}`;
}

function renderSchedules(data) {
  // 数据结构 {gid: [task, ...]}：每群可能有多任务
  const rows = [];
  Object.entries(data).forEach(([gid, tasks]) => {
    (tasks || []).forEach((s, i) => rows.push({ gid, s, key: `${gid}-${s.id || i}` }));
  });
  const tbody = $("scheduleBody");
  $("scheduleEmpty").classList.toggle("hidden", rows.length > 0);
  tbody.innerHTML = "";
  rows.forEach(({ gid, s }) => {
    const modeBadge = s.mode === "weekly" ? "blue" : s.mode === "daily" ? "green" : "";
    const modeTxt = { once: "单次", daily: "每日", weekly: "每周" }[s.mode] || s.mode;
    const statusBadge = s.started
      ? '<span class="badge red">禁言中</span>'
      : '<span class="badge">待触发</span>';
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${gid}</td>` +
      `<td><span class="badge ${modeBadge}">${modeTxt}</span></td>` +
      `<td>${describeSchedule(s)}</td>` +
      `<td>${statusBadge}</td>` +
      `<td><button class="btn ghost sm danger" data-g="${gid}" data-id="${s.id || ""}">删除</button></td>`;
    tr.querySelector("button").addEventListener("click", async (e) => {
      const b = e.currentTarget;
      // 传 task_id 只删对应任务
      await api("schedules/delete", "POST", { group_id: b.dataset.g, task_id: b.dataset.id });
      toast("scheduleToast", "已删除");
      loadSchedules();
    });
    tbody.appendChild(tr);
  });
}

async function loadSchedules() {
  renderSchedules(await api("schedules"));
}

$("sMode").addEventListener("change", () => {
  $("schedule-form").classList.toggle("weekly", $("sMode").value === "weekly");
});
$("setSchedule").addEventListener("click", async () => {
  const payload = {
    group_id: $("sGroup").value.trim(),
    mode: $("sMode").value,
    start_time: $("sStart").value.trim(),
    end_time: $("sEnd").value.trim(),
  };
  if (!payload.group_id) return toast("scheduleToast", "请填写群号", true);
  if (payload.mode === "weekly") {
    payload.weekdays = $("sWeekdays").value.trim();
    if (!payload.weekdays) return toast("scheduleToast", "请填写周几", true);
  }
  try {
    await api("schedules/set", "POST", payload);
    toast("scheduleToast", "已设置");
    loadSchedules();
  } catch (e) {
    toast("scheduleToast", "设置失败：" + e, true);
  }
});

/* ---------- 入群审批 ---------- */
// toggle 开关交互（点击切换高亮）
function bindToggle(el, on) {
  el.classList.toggle("on", !!on);
  el.removeEventListener("click", toggleHandler);
  el.addEventListener("click", toggleHandler);
}
function toggleHandler(e) {
  e.currentTarget.classList.toggle("on");
}

async function loadJoin() {
  const data = await api("config");
  bindToggle($("joinVerifyToggle"), data.join_verify_enable);
  bindToggle($("joinCardNotifyToggle"), data.join_card_notify);
  $("joinWelcome").value = data.join_welcome_msg || "";
  $("joinWelcomeOther").value = data.join_welcome_other_msg || "";
  $("joinCardNotifyMsg").value = data.join_card_notify_msg || "";
}
$("saveJoin").addEventListener("click", async () => {
  // config/update 只更新提交的字段，未提交项保持不变，无需回填
  const payload = {
    join_verify_enable: $("joinVerifyToggle").classList.contains("on"),
    join_welcome_msg: $("joinWelcome").value,
    join_welcome_other_msg: $("joinWelcomeOther").value,
    join_card_notify: $("joinCardNotifyToggle").classList.contains("on"),
    join_card_notify_msg: $("joinCardNotifyMsg").value,
  };
  try {
    await api("config/save", "POST", payload);
    toast("joinToast", "已保存");
  } catch (e) {
    toast("joinToast", "保存失败：" + e, true);
  }
});

/* ---------- 群管理 ---------- */
async function loadGroups() {
  const data = await api("config");
  $("groupWhitelist").value = toCsv(data.group_whitelist);
}
$("saveGroups").addEventListener("click", async () => {
  try {
    await api("config/save", "POST", { group_whitelist: fromCsv($("groupWhitelist").value) });
    toast("groupsToast", "已保存");
  } catch (e) {
    toast("groupsToast", "保存失败：" + e, true);
  }
});

/* ---------- 初始化 ---------- */
(async function init() {
  try {
    // ready() 返回的 context 只含 pluginName/displayName 等元信息，无 config
    const ctx = await bridge.ready();
    const conn = document.querySelector(".conn");
    conn.classList.add("ok");
    $("connText").textContent = "已连接 · " + (ctx.displayName || ctx.pluginName || "");
  } catch (e) {
    document.querySelector(".conn").classList.add("bad");
    $("connText").textContent = "连接失败";
    return;
  }
  // 配置需通过后端 API 拉取，context 中不包含
  try {
    config = await api("config");
  } catch (e) {
    console.error("加载配置失败:", e);
  }
  renderConfigForm();
  loadSchedules();
})();
