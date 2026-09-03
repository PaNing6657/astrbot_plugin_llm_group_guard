/* LLM 群守卫 WebUI：多群管理，进入先选群 */
const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

/* ---------- 全局(LLM 服务池之外的通用项)字段定义 ---------- */
const GLOBAL_FIELDS = [
  { key: "llm_timeout", label: "请求超时（秒）", type: "number" },
  { key: "llm_max_tokens", label: "输出上限（token）", type: "number", hint: "思考型模型可调大" },
];

/* ---------- 本群字段定义 ---------- */
const GROUP_FIELDS = [
  { key: "llm_use", label: "本群 LLM 模型", type: "model-select", full: true, hint: "本群审核/入群审批使用的服务+模型；先在上方全局 LLM 服务中添加" },
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
  { key: "ai_reply_only_manager", label: "AI 仅回复管理", type: "toggle", hint: "仅群主/群管/机器管理员的消息 AI 才回复" },
  { key: "ai_reply_whitelist", label: "AI 回复白名单（逗号分隔）", type: "csv", full: true, hint: "开启“仅回复管理”后，白名单 QQ 号仍可触发 AI 回复" },
];

// 兼容中英文逗号分割
const toCsv = (v) => (Array.isArray(v) ? v.join(", ") : v ?? "");
const fromCsv = (s) => String(s || "").split(/[,，]/).map((x) => x.trim()).filter(Boolean);
const newProviderId = () => "p" + Date.now().toString(36) + Math.floor(Math.random() * 1000).toString(36);

// 页面状态：当前群、该群的配置（global + group）、LLM 服务池
let groups = [];
let currentGroup = "";
let currentGroupName = "";
let globalConfig = {};
let groupConfig = {};
let providers = [];

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
function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
  });
});

/* ---------- 群选择 ---------- */
async function openGroupPicker(force = false) {
  $("groupList").innerHTML = '<div class="empty">正在获取群列表…</div>';
  $("groupModal").classList.add("show");
  let data;
  try {
    data = await api("groups", "GET", force ? { force: 1 } : {});
  } catch (e) {
    renderGroupError("获取群列表失败：" + e);
    return;
  }
  groups = (data && data.groups) || [];
  if (!groups.length) {
    $("groupList").innerHTML =
      '<div class="empty">暂无可管理群<br><span style="font-size:12px">机器人需为群主或群管理员才能被管理</span></div>' +
      '<div class="actions" style="justify-content:center;margin-top:4px"><button class="btn primary" id="retryGroups">重新检测</button></div>';
    $("retryGroups").addEventListener("click", () => openGroupPicker(true));
    return;
  }
  $("groupList").innerHTML = "";
  groups.forEach((g) => {
    const item = document.createElement("div");
    item.className = "group-item";
    const roleBadge = g.role === "owner"
      ? '<span class="badge red">群主</span>'
      : '<span class="badge blue">管理员</span>';
    item.innerHTML =
      `<div class="g-name">${escapeHtml(g.group_name || g.group_id)}</div>` +
      `<div class="g-meta"><span class="badge">${g.group_id}</span>${roleBadge}</div>`;
    item.addEventListener("click", () => selectGroup(g.group_id, g.group_name));
    $("groupList").appendChild(item);
  });
}

function renderGroupError(msg) {
  $("groupList").innerHTML =
    `<div class="empty">${escapeHtml(msg)}</div>` +
    '<div class="actions" style="justify-content:center;margin-top:4px"><button class="btn primary" id="retryGroups">重试</button></div>';
  $("retryGroups").addEventListener("click", () => openGroupPicker(true));
}

async function selectGroup(gid, name) {
  currentGroup = gid;
  currentGroupName = name || gid;
  $("groupModal").classList.remove("show");
  $("currentGroup").textContent = `${currentGroupName}（${currentGroup}）`;
  await loadConfig();
  loadSchedules();
  loadViolations();
  loadJoin();
}

$("switchGroup").addEventListener("click", () => openGroupPicker(false));

/* ---------- 全局 LLM 服务池编辑器 ---------- */
function renderProviders() {
  const rows = $("providerRows");
  if (!providers.length) {
    rows.innerHTML = '<div class="empty" style="padding:16px 0">尚未添加 LLM 服务</div>';
    return;
  }
  rows.innerHTML = providers.map((p, i) =>
    `<div class="provider-row" data-pid="${escapeHtml(p.id || "")}">` +
    `<input class="p-name" placeholder="名称（如 DeepSeek）" value="${escapeHtml(p.name || "")}">` +
    `<input class="p-base" placeholder="接口地址 https://api.xxx.com/v1" value="${escapeHtml(p.base_url || "")}">` +
    `<input class="p-key" type="password" placeholder="API Key" value="${escapeHtml(p.api_key || "")}">` +
    `<input class="p-models" placeholder="模型名（逗号分隔，如 deepseek-chat,glm-4-flash）" value="${escapeHtml(toCsv(p.models))}">` +
    `<button class="btn ghost sm danger" data-i="${i}" type="button">删</button></div>`
  ).join("");
  rows.querySelectorAll("button[data-i]").forEach((btn) => {
    btn.addEventListener("click", () => {
      providers.splice(Number(btn.dataset.i), 1);
      renderProviders();
      renderConfigForm(); // 联动更新本群模型下拉
    });
  });
}

function collectProviders() {
  return [...document.querySelectorAll("#providerRows .provider-row")].map((r) => ({
    id: r.dataset.pid || newProviderId(),
    name: r.querySelector(".p-name").value.trim(),
    base_url: r.querySelector(".p-base").value.trim(),
    api_key: r.querySelector(".p-key").value.trim(),
    models: fromCsv(r.querySelector(".p-models").value),
  })).filter((p) => p.base_url && p.api_key && p.models.length);
}

$("addProvider").addEventListener("click", () => {
  providers.push({ id: newProviderId(), name: "", base_url: "", api_key: "", models: [] });
  renderProviders();
  renderConfigForm();
});

/* ---------- 配置表单 ---------- */
function renderGlobalFields() {
  const grid = $("globalFields");
  grid.innerHTML = GLOBAL_FIELDS.map((f) => {
    const val = globalConfig[f.key];
    return `<div class="field ${f.full ? "full" : ""}"><label>${f.label}</label>` +
      `<input type="number" data-gkey="${f.key}" value="${escapeHtml(val ?? "")}">` +
      (f.hint ? `<div class="hint">${f.hint}</div>` : "") + `</div>`;
  }).join("");
}

function collectGlobalFields() {
  const out = {};
  document.querySelectorAll("#globalFields [data-gkey]").forEach((n) => {
    out[n.dataset.gkey] = n.value === "" ? null : Number(n.value);
  });
  return out;
}

function renderConfigForm() {
  const wrap = $("configForm");
  wrap.innerHTML = '<div class="form-grid" style="grid-template-columns:1fr 1fr 1fr"></div>';
  const grid = wrap.firstElementChild;
  GROUP_FIELDS.forEach((f) => {
    const el = document.createElement("div");
    el.className = "field" + (f.full ? " full" : "");
    const val = groupConfig[f.key];
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
    } else if (f.type === "model-select") {
      // 本群 LLM 选择：提供商 + 模型 两个联动下拉
      const sel = groupConfig.llm_use || {};
      const selPid = sel.provider_id || "";
      const selModel = sel.model || "";
      const pdOpts = providers.map((p) =>
        `<option value="${escapeHtml(p.id)}" ${p.id === selPid ? "selected" : ""}>${escapeHtml(p.name || p.id)}</option>`
      ).join("");
      const activeP = providers.find((p) => p.id === selPid) || providers[0];
      const mdOpts = (activeP ? activeP.models : []).map((m) =>
        `<option value="${escapeHtml(m)}" ${m === selModel ? "selected" : ""}>${escapeHtml(m)}</option>`
      ).join("");
      el.innerHTML = `<label>${f.label}</label><div class="model-sel">` +
        `<select class="sel-provider">${pdOpts || '<option value="">未配置</option>'}</select>` +
        `<select class="sel-model">${mdOpts || '<option value="">暂无模型</option>'}</select></div>` +
        (f.hint ? `<div class="hint">${f.hint}</div>` : "");
      // 提供商切换时联动刷新模型下拉
      const pd = el.querySelector(".sel-provider");
      const ms = el.querySelector(".sel-model");
      pd.addEventListener("change", () => {
        const p = providers.find((x) => x.id === pd.value);
        ms.innerHTML = (p ? p.models : []).map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("") ||
          '<option value="">暂无模型</option>';
      });
    } else if (f.type === "textarea") {
      el.innerHTML = `<label>${f.label}</label><textarea data-key="${f.key}">${escapeHtml(val ?? "")}</textarea>` +
        (f.hint ? `<div class="hint">${f.hint}</div>` : "");
    } else {
      const isCsv = f.type === "csv";
      el.innerHTML = `<label>${f.label}</label><input type="text" data-key="${f.key}" class="${isCsv ? "csv" : ""}" value="${escapeHtml(isCsv ? toCsv(val) : (val ?? ""))}">` +
        (f.hint ? `<div class="hint">${f.hint}</div>` : "");
    }
    grid.appendChild(el);
  });
}

function collectGroupConfig() {
  const out = {};
  document.querySelectorAll("#configForm [data-key]").forEach((node) => {
    const key = node.dataset.key;
    if (node.classList.contains("toggle")) out[key] = node.classList.contains("on");
    else if (node.tagName === "TEXTAREA") out[key] = node.value;
    else if (node.tagName === "SELECT") out[key] = node.value;
    else if (node.classList.contains("csv")) out[key] = fromCsv(node.value);
    else out[key] = node.value;
  });
  // 本群 LLM 选择：把两个联动下拉合成 llm_use 对象
  const pd = document.querySelector("#configForm .sel-provider");
  const ms = document.querySelector("#configForm .sel-model");
  if (pd && ms && pd.value) {
    out.llm_use = { provider_id: pd.value, model: ms.value };
  } else {
    out.llm_use = { provider_id: "", model: "" };
  }
  return out;
}

async function loadConfig() {
  try {
    const data = await api("config", "GET", { group_id: currentGroup });
    globalConfig = data.global || {};
    groupConfig = data.group || {};
    providers = (globalConfig.llm_providers || []).map((p) => ({ ...p, models: [...(p.models || [])] }));
  } catch (e) {
    toast("configToast", "加载配置失败：" + e, true);
  }
  renderProviders();
  renderGlobalFields();
  renderConfigForm();
}

$("saveConfig").addEventListener("click", async () => {
  const btn = $("saveConfig");
  btn.disabled = true;
  try {
    const global = { llm_providers: collectProviders(), ...collectGlobalFields() };
    await api("config/save", "POST", { global, group_id: currentGroup, group: collectGroupConfig() });
    toast("configToast", "已保存");
    // 保存后刷新本群模型下拉关联的服务池
    providers = global.llm_providers;
    renderConfigForm();
  } catch (e) {
    toast("configToast", "保存失败：" + e, true);
  }
  btn.disabled = false;
});

/* ---------- 违规记录（按当前群） ---------- */
let violationLog = []; // 后端违规消息日志

function fmtLogTs(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderViolations(data) {
  // 数据结构：{llm: {gid: {uid: count}}, keyword: {gid: {uid: count}}, log: [...]}
  violationLog = (data.log || []).filter((e) => e.gid === currentGroup);
  const rows = [];
  const collect = (source, type) =>
    Object.entries((data[source] || {})[currentGroup] || {}).forEach(([uid, count]) => rows.push({ uid, count, type }));
  collect("llm", "llm");
  collect("keyword", "keyword");
  const tbody = $("violationBody");
  $("violationEmpty").classList.toggle("hidden", rows.length > 0);
  tbody.innerHTML = "";
  rows.forEach(({ uid, count, type }) => {
    const typeBadge = type === "keyword"
      ? '<span class="badge green">关键词</span>'
      : '<span class="badge blue">LLM</span>';
    const last = violationLog.find((e) => e.uid === uid && e.source === type);
    const lastText = last ? escapeHtml(last.text) : "—";
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${uid}</td>` +
      `<td>${typeBadge}</td>` +
      `<td><span class="badge ${count >= 3 ? "red" : count >= 2 ? "warn" : "blue"}">${count} 次</span></td>` +
      `<td class="msg-cell" title="${lastText}">${lastText}</td>` +
      `<td>` +
      `<button class="btn ghost sm" data-act="log" data-u="${uid}">记录</button> ` +
      `<button class="btn ghost sm" data-act="reset" data-u="${uid}" data-t="${type}">清零</button>` +
      `</td>`;
    tr.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        const b = e.currentTarget;
        if (b.dataset.act === "log") {
          showViolationHistory(b.dataset.u);
        } else {
          await api("violations/reset", "POST", {
            group_id: currentGroup,
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

function showViolationHistory(uid) {
  const entries = violationLog.filter((e) => e.uid === uid);
  const list = $("historyList");
  $("historyTitle").textContent = `违规记录 · 群 ${currentGroupName}（${currentGroup}） · 用户 ${uid}`;
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
  await api("violations/reset", "POST", { group_id: currentGroup });
  loadViolations();
  toast("configToast", "当前群违规记录已全部清零");
});

/* 违规历史弹窗关闭：点按钮或遮罩 */
$("historyClose").addEventListener("click", closeHistoryModal);
$("historyModal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeHistoryModal();
});

/* ---------- 定时禁言（按当前群） ---------- */
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
  // 数据结构 {gid: [task, ...]}：只展示当前群的任务
  const tasks = (data || {})[currentGroup] || [];
  const tbody = $("scheduleBody");
  $("scheduleEmpty").classList.toggle("hidden", tasks.length > 0);
  tbody.innerHTML = "";
  tasks.forEach((s, i) => {
    const key = `${currentGroup}-${s.id || i}`;
    const modeBadge = s.mode === "weekly" ? "blue" : s.mode === "daily" ? "green" : "";
    const modeTxt = { once: "单次", daily: "每日", weekly: "每周" }[s.mode] || s.mode;
    const statusBadge = s.started
      ? '<span class="badge red">禁言中</span>'
      : '<span class="badge">待触发</span>';
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><span class="badge ${modeBadge}">${modeTxt}</span></td>` +
      `<td>${describeSchedule(s)}</td>` +
      `<td>${statusBadge}</td>` +
      `<td><button class="btn ghost sm danger" data-id="${s.id || ""}">删除</button></td>`;
    tr.querySelector("button").addEventListener("click", async (e) => {
      const b = e.currentTarget;
      await api("schedules/delete", "POST", { group_id: currentGroup, task_id: b.dataset.id });
      toast("scheduleToast", "已删除");
      loadSchedules();
    });
    tbody.appendChild(tr);
  });
}

async function loadSchedules() {
  renderSchedules(await api("schedules"));
}

// 定时禁言表单默认作用于当前群
$("sGroup").value = "";
$("setSchedule").addEventListener("click", async () => {
  const payload = {
    group_id: $("sGroup").value.trim() || currentGroup,
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

$("sMode").addEventListener("change", () => {
  $("schedule-form").classList.toggle("weekly", $("sMode").value === "weekly");
});

/* ---------- 入群审批（按当前群） ---------- */
function bindToggle(el, on) {
  el.classList.toggle("on", !!on);
  el.removeEventListener("click", toggleHandler);
  el.addEventListener("click", toggleHandler);
}
function toggleHandler(e) {
  e.currentTarget.classList.toggle("on");
}

async function loadJoin() {
  let data;
  try {
    data = await api("config", "GET", { group_id: currentGroup });
  } catch (e) {
    toast("joinToast", "加载失败：" + e, true);
    return;
  }
  const g = data.group || {};
  bindToggle($("joinVerifyToggle"), g.join_verify_enable);
  bindToggle($("joinCardNotifyToggle"), g.join_card_notify);
  $("joinWelcome").value = g.join_welcome_msg || "";
  $("joinCardNotifyMsg").value = g.join_card_notify_msg || "";
  $("joinCardNotifyFailMsg").value = g.join_card_notify_fail_msg || "";
}
$("saveJoin").addEventListener("click", async () => {
  const payload = {
    join_verify_enable: $("joinVerifyToggle").classList.contains("on"),
    join_welcome_msg: $("joinWelcome").value,
    join_card_notify: $("joinCardNotifyToggle").classList.contains("on"),
    join_card_notify_msg: $("joinCardNotifyMsg").value,
    join_card_notify_fail_msg: $("joinCardNotifyFailMsg").value,
  };
  try {
    await api("config/save", "POST", { group_id: currentGroup, group: payload });
    toast("joinToast", "已保存");
  } catch (e) {
    toast("joinToast", "保存失败：" + e, true);
  }
});

/* ---------- 初始化 ---------- */
(async function init() {
  try {
    const ctx = await bridge.ready();
    const conn = document.querySelector(".conn");
    conn.classList.add("ok");
    $("connText").textContent = "已连接 · " + (ctx.displayName || ctx.pluginName || "");
  } catch (e) {
    document.querySelector(".conn").classList.add("bad");
    $("connText").textContent = "连接失败";
    return;
  }
  // 进入页面先选择管理群
  openGroupPicker(false);
})();