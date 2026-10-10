/* =====================================================================
   Genie TTS · 语音合成工作台 · 前端逻辑
   全部请求走 window.AstrBotPluginPage 桥（iframe 无 same-origin，不能直接 fetch）
   ===================================================================== */
(function () {
"use strict";

var SDK = window.AstrBotPluginPage;
var D = document;
var SEP = String.fromCharCode(1);

/* ------------------------------------------------------------ DOM 工具 */

function $(id) { return D.getElementById(id); }

function append(node, kids) {
  if (kids === null || kids === undefined || kids === false || kids === true) return node;
  if (Array.isArray(kids)) {
    for (var i = 0; i < kids.length; i++) append(node, kids[i]);
    return node;
  }
  if (typeof kids === "object" && kids.nodeType) { node.appendChild(kids); return node; }
  node.appendChild(D.createTextNode(String(kids)));
  return node;
}

function h(tag, attrs, kids) {
  var n = D.createElement(tag);
  if (attrs) {
    for (var k in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
      var v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") { n.className = v; }
      else if (k === "text") { n.textContent = String(v); }
      else if (k === "value") { n.value = v; }
      else if (k === "checked") { n.checked = !!v; }
      else if (k === "disabled") { n.disabled = !!v; }
      else if (k === "hidden") { n.hidden = !!v; }
      else if (k.slice(0, 2) === "on") { n.addEventListener(k.slice(2), v); }
      else { n.setAttribute(k, v === true ? "" : String(v)); }
    }
  }
  if (kids !== undefined) append(n, kids);
  return n;
}

function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); return node; }

/* ------------------------------------------------------------ 滚动
   插件页跑在 iframe 里，滚的是文档本身（topbar / tabbar 是 sticky）。
   harness 的 window 只有 scrollTo，别的一概没有，所以每一步都 typeof + try/catch。 */

function scrollYSafe() {
  try { if (typeof window !== "undefined" && typeof window.pageYOffset === "number") return window.pageYOffset; } catch (e1) {}
  try { if (D.documentElement && typeof D.documentElement.scrollTop === "number") return D.documentElement.scrollTop; } catch (e2) {}
  try { if (D.body && typeof D.body.scrollTop === "number") return D.body.scrollTop; } catch (e3) {}
  return 0;
}

function scrollToSafe(y) {
  y = Number(y) || 0;
  if (y < 0) y = 0;
  try {
    if (typeof window !== "undefined" && typeof window.scrollTo === "function") { window.scrollTo(0, y); return; }
  } catch (e1) {}
  try { if (D.documentElement) { D.documentElement.scrollTop = y; return; } } catch (e2) {}
  try { if (D.body) D.body.scrollTop = y; } catch (e3) {}
}

/* 切分区后把滚动位置拉回顶部。不复位的话，从长分区（比如感情库）切到别的
   分区时视口还停在页尾，看起来像「没切过去」。 */
function scrollTopSafe() { scrollToSafe(0); }

/* 原地重画一个分区时保住视口位置。
   clear(view) 会先把内容全删掉，文档瞬间变短，浏览器立刻把 scrollTop 夹到
   新的最大值（往往就是 0）；等内容补回来，滚动条已经回页首了 —— 从用户角度
   看就是「点一下展开，页面自己往上跳」。这里做两件事：重画期间给容器钉住
   原来的高度，让文档别缩；重画完再把 scrollTop 写回去兜第二道。
   offsetHeight 在 harness 里不存在，所以只在拿得到数字时才钉高度。 */
/* 节点是否还挂在文档上。整页重画之后旧节点的 parentNode 往往仍指向一个
   已经被摘掉的容器，光看 parentNode 会误判成「还在」，往上插新节点就成了空操作。 */
function attachedSafe(node) {
  var p = node;
  var guard = 0;
  while (p && guard++ < 400) {
    if (p === D.documentElement || p === D.body || p.localName === "html") return true;
    p = p.parentNode;
  }
  return false;
}

function keepScroll(node, fn) {
  var y = scrollYSafe();
  var lock = null;
  try {
    if (node && node.style && typeof node.offsetHeight === "number" && node.offsetHeight > 0) {
      lock = node.style.minHeight || "";
      node.style.minHeight = node.offsetHeight + "px";
    }
  } catch (e1) {}
  try {
    fn();
  } finally {
    try { if (lock !== null && node && node.style) node.style.minHeight = lock; } catch (e2) {}
    try { if (scrollYSafe() !== y) scrollToSafe(y); } catch (e3) {}
  }
}

/* ------------------------------------------------------------ 格式化 */

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

function fmtSec(n) {
  n = Number(n) || 0;
  if (n <= 0) return "0.00s";
  if (n < 60) return n.toFixed(2) + "s";
  var m = Math.floor(n / 60);
  return m + "m" + (n - m * 60).toFixed(1) + "s";
}

function fmtMs(n) { return (Number(n) || 0) + "ms"; }

function fmtTime(v) {
  if (!v) return "—";
  var d = new Date(v);
  if (isNaN(d.getTime())) return String(v);
  function p(x) { return (x < 10 ? "0" : "") + x; }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
         p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function shorten(v, n) {
  v = v === null || v === undefined ? "" : String(v);
  return v.length > n ? v.slice(0, n - 1) + "…" : v;
}

function safeName(v, fallback) {
  v = String(v || "").trim();
  v = v.replace(new RegExp("[^0-9A-Za-z._\u4e00-\u9fff-]+", "g"), "_");
  v = v.replace(new RegExp("^[._]+"), "");
  if (!v) return fallback;
  if (v.toLowerCase().slice(-5) !== ".json") v = v + ".json";
  return v;
}

/* ------------------------------------------------------------ 小部件 */

function kicker(t) { return h("p", { class: "kicker", text: t }); }
function rule() { return h("div", { class: "rule" }, h("i")); }
function chip(text, cls) { return h("span", { class: "chip" + (cls ? " " + cls : ""), text: text }); }
function badge(text, tone) { return h("span", { class: "badge", "data-tone": tone || null, text: text }); }
function note(text, tone) { return h("div", { class: "note", "data-tone": tone || null }, h("span", {}, text)); }
function empty(title, desc) { return h("div", { class: "empty" }, [h("b", { text: title }), desc || ""]); }
function dim(text) { return h("span", { class: "dim", text: text }); }
function mono(text) { return h("span", { class: "mono", text: text }); }

function stat(num, cap, tone) {
  return h("div", { class: "stat", "data-tone": tone || null }, [
    h("span", { class: "stat-num", text: num }),
    h("span", { class: "stat-cap", text: cap })
  ]);
}

function kvRow(label, value, cls) {
  return h("div", { class: "kv-row" }, [
    h("span", { class: "kv-label", text: label }),
    h("span", { class: "kv-value" + (cls ? " " + cls : ""), text: value === null || value === undefined || value === "" ? "—" : String(value) })
  ]);
}

function kv(rows) {
  var box = h("div", { class: "kv" });
  for (var i = 0; i < rows.length; i++) {
    if (!rows[i]) continue;
    box.appendChild(kvRow(rows[i][0], rows[i][1], rows[i][2]));
  }
  return box;
}

function card(opts) {
  var c = h("section", { class: "card" + (opts.sub ? " card-sub" : "") + (opts.class ? " " + opts.class : "") });
  if (opts.kicker || opts.title || opts.desc || opts.tools) {
    var head = h("div", { class: "card-head" });
    var grow = h("div", { class: "grow" });
    if (opts.kicker) grow.appendChild(kicker(opts.kicker));
    if (opts.title) grow.appendChild(h("h2", { class: "card-title", text: opts.title }));
    if (opts.desc) grow.appendChild(h("p", { class: "card-desc", text: opts.desc }));
    head.appendChild(grow);
    if (opts.tools) head.appendChild(h("div", { class: "card-tools" }, opts.tools));
    c.appendChild(head);
    c.appendChild(rule());
  }
  c.appendChild(h("div", { class: "card-body" }, opts.body || []));
  return c;
}

function btn(text, opts) {
  opts = opts || {};
  return h("button", {
    type: "button",
    class: "btn" + (opts.kind ? " btn-" + opts.kind : "") + (opts.sm ? " btn-sm" : "") + (opts.class ? " " + opts.class : ""),
    title: opts.title || null,
    disabled: opts.disabled || false,
    onclick: opts.onclick || null
  }, text);
}

function field(opts) {
  var f = h("div", { class: "field" + (opts.class ? " " + opts.class : "") });
  if (opts.label || opts.key) {
    var lab = h("label", { class: "field-label" });
    if (opts.label) lab.appendChild(h("span", { text: opts.label }));
    if (opts.key) lab.appendChild(h("span", { class: "field-key", text: opts.key }));
    if (opts.tag) lab.appendChild(opts.tag);
    f.appendChild(lab);
  }
  if (opts.desc) f.appendChild(h("p", { class: "field-desc", text: opts.desc }));
  append(f, opts.control);
  if (opts.hint) f.appendChild(h("p", { class: "field-hint", text: opts.hint }));
  if (opts.hintNode) f.appendChild(opts.hintNode);  /* 需要随输入实时改写的提示行走这个口子 */
  return f;
}

function select(options, value, onchange, cls) {
  var s = h("select", { class: "select" + (cls ? " " + cls : ""), onchange: onchange });
  for (var i = 0; i < options.length; i++) {
    var o = options[i];
    var val = typeof o === "object" ? o.value : o;
    var lab = typeof o === "object" ? o.label : o;
    s.appendChild(h("option", { value: val, text: lab, selected: String(val) === String(value) ? true : null }));
  }
  s.value = value === null || value === undefined ? "" : String(value);
  return s;
}

function input(value, onchange, opts) {
  opts = opts || {};
  return h("input", {
    class: "input" + (opts.mono ? " mono" : ""),
    type: opts.type || "text",
    value: value === null || value === undefined ? "" : String(value),
    placeholder: opts.placeholder || null,
    min: opts.min === undefined ? null : opts.min,
    max: opts.max === undefined ? null : opts.max,
    step: opts.step === undefined ? null : opts.step,
    oninput: opts.oninput || null,
    onchange: onchange || null
  });
}

function textarea(value, onchange, opts) {
  opts = opts || {};
  var t = h("textarea", {
    class: "textarea" + (opts.mono ? " mono" : ""),
    rows: opts.rows || null,
    placeholder: opts.placeholder || null,
    oninput: opts.oninput || null,
    onchange: onchange || null
  });
  t.value = value === null || value === undefined ? "" : String(value);
  return t;
}

function switchBox(checked, text, onchange) {
  var inp = h("input", { type: "checkbox", checked: !!checked, onchange: onchange });
  return h("label", { class: "switch" }, [inp, h("span", { class: "switch-track" }), h("span", { class: "switch-text", text: text })]);
}

function segment(options, value, onpick) {
  var box = h("div", { class: "seg" });
  options.forEach(function (o) {
    var val = typeof o === "object" ? o.value : o;
    var lab = typeof o === "object" ? o.label : o;
    box.appendChild(h("button", {
      type: "button",
      "aria-pressed": String(val) === String(value) ? "true" : "false",
      text: lab,
      onclick: function () { onpick(val); }
    }));
  });
  return box;
}

function table(head, cols) {
  var wrap = h("div", { class: "table-wrap" });
  var t = h("table", { class: "tbl" });
  if (cols) {
    var cg = h("colgroup");
    cols.forEach(function (c) { cg.appendChild(h("col", { class: c || null })); });
    t.appendChild(cg);
  }
  var tr = h("tr");
  head.forEach(function (x) { tr.appendChild(h("th", { text: typeof x === "object" ? x.label : x, class: typeof x === "object" ? x.class : null })); });
  t.appendChild(h("thead", {}, tr));
  var tb = h("tbody");
  t.appendChild(tb);
  wrap.appendChild(t);
  wrap.body = tb;
  return wrap;
}

/* ------------------------------------------------------------ toast / modal */

function toast(msg, tone, ms) {
  var host = $("toasts");
  if (!host) return;
  var box = h("div", { class: "toast", "data-tone": tone || "info" }, [
    h("i", { class: "toast-bar" }),
    h("span", { class: "grow", text: String(msg) })
  ]);
  var x = h("button", { type: "button", class: "toast-x", "aria-label": "关闭", text: "×" });
  box.appendChild(x);
  function kill() {
    if (box.dataset.out === "true") return;
    box.dataset.out = "true";
    setTimeout(function () { if (box.parentNode) box.parentNode.removeChild(box); }, 220);
  }
  x.addEventListener("click", kill);
  host.appendChild(box);
  while (host.children.length > 5) host.removeChild(host.firstChild);
  setTimeout(kill, ms || (tone === "danger" ? 8000 : 4200));
}

var modalState = { resolve: null, onOk: null };

function closeModal(result) {
  var m = $("modal");
  if (!m || m.hidden) return;
  m.hidden = true;
  var r = modalState.resolve;
  modalState.resolve = null;
  modalState.onOk = null;
  if (r) r(result);
}

function openModal(opts) {
  var m = $("modal");
  $("modal-kicker").textContent = opts.kicker || "CONFIRM";
  $("modal-title").textContent = opts.title || "";
  clear($("modal-body"));
  append($("modal-body"), opts.body === undefined ? "" : opts.body);
  var ok = $("modal-ok");
  var cancel = $("modal-cancel");
  ok.textContent = opts.okText || "确定";
  ok.className = "btn " + (opts.danger ? "btn-danger" : "btn-primary");
  cancel.textContent = opts.cancelText || "取消";
  cancel.hidden = !!opts.hideCancel;
  m.hidden = false;
  setTimeout(function () { try { ok.focus(); } catch (e) {} }, 30);
  return new Promise(function (resolve) { modalState.resolve = resolve; });
}

function confirmModal(title, body, opts) {
  opts = opts || {};
  return openModal({
    kicker: opts.kicker || "CONFIRM",
    title: title,
    body: body,
    okText: opts.okText || "确定",
    danger: opts.danger
  });
}

function copyText(text, label) {
  var ta = h("textarea", { "aria-hidden": "true" });
  ta.value = String(text === null || text === undefined ? "" : text);
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  ta.style.opacity = "0";
  D.body.appendChild(ta);
  ta.focus();
  ta.select();
  var ok = false;
  try { ok = D.execCommand("copy"); } catch (e) { ok = false; }
  D.body.removeChild(ta);
  toast(ok ? (label || "已复制到剪贴板") : "复制失败，请手动选中文本", ok ? "ok" : "warn");
  return ok;
}

/* ------------------------------------------------------------ API */

function apiGet(ep, params) { return SDK.apiGet(ep, params || {}); }
function apiPost(ep, body) { return SDK.apiPost(ep, body || {}); }

function fail(e, prefix) {
  var msg = e && e.message ? e.message : String(e || "未知错误");
  toast((prefix ? prefix + "：" : "") + msg, "danger");
  return null;
}

/* ------------------------------------------------------------ 状态 */

var TABS = [
  { id: "studio",   label: "工作台",  ico: "◈" },
  { id: "emotions", label: "感情库",  ico: "❀" },
  { id: "packs",    label: "感情包",  ico: "❐" },
  { id: "favorites", label: "收藏",   ico: "★" },
  { id: "config",   label: "配置",    ico: "⚙" },
  { id: "servers",  label: "服务器",  ico: "☁" },
  { id: "sessions", label: "会话",    ico: "◉" },
  { id: "commands", label: "指令表",  ico: "⌘" },
  { id: "logs",     label: "日志",    ico: "▤" },
  { id: "about",    label: "关于",    ico: "ⓘ" }
];

var state = {
  tab: "studio",
  prefs: { theme: "moonlit", density: "comfortable", tab: "studio", log_paint: true, themes: [], densities: ["comfortable", "compact"] },
  overview: null,
  emotions: null,
  packs: null,
  favorites: null,
  servers: null,
  config: null,
  sessions: null,
  commands: null,
  logs: null,
  synths: null,
  loaded: {},
  busy: {},
  studio: {
    character: "", emotion: "", language: "",
    text: "", preview: null, result: null, history: [],
    previewing: false, synthing: false, refPath: "", refText: "", freeRef: false
  },
  emo: { q: "", ch: "", picked: {}, editing: null, form: null },
  pack: { importText: "", mode: "merge", report: null, note: "", filename: "", dry: true, fileName: "" },
  /* 语音收藏：q/ch/emo/src/pinned 是服务端筛选条件，picked 是批量导出的勾选，
     open 记哪几条展开了播放器，audio 缓存已经取回来的 base64（避免重复请求），
     nodes 记住每条收藏的 DOM 节点 —— 展开/折叠只换那一条，不整页重画。 */
  fav: {
    q: "", ch: "", emo: "", src: "", pinned: false,
    picked: {}, open: {}, audio: {}, nodes: {}, loadingAudio: {},
    mode: "merge", report: null, bundleName: "", bundleData: "",
    upName: "", upData: "", upAlias: "", upChar: "", upEmo: "", upText: "", uploading: false,
    seq: 0, loading: false, debounce: null, busy: false, rebuilding: false,
    listBox: null, metaNode: null, statBox: null, exportPicked: null, resetBtn: null
  },
  cfg: { dirty: {}, needsReload: false, saving: false },
  /* 日志面板：sub 选子视图，其余是两套互不干扰的服务端筛选条件。
     lastKey 记最后一次按键时间，自动刷新在打字时会让路，避免输入框被重渲染打断。
     paint 控制运行日志的正文着色；recNodes 记住每条合成记录的 DOM 节点，
     展开/折叠只换那一条，不整页重画（否则视口会被浏览器夹回页首）。 */
  logsUI: {
    sub: "synths",
    q: "", level: "", tag: "",
    sq: "", status: "", source: "", character: "",
    auto: false, seq: 0, timer: null, debounce: null, lastKey: 0,
    loading: false, expanded: {}, stats: 60, paint: true, recNodes: {}
  }
};

function pickedKeys() {
  var out = [];
  for (var k in state.emo.picked) {
    if (state.emo.picked[k]) {
      var parts = k.split(SEP);
      out.push({ character: parts[0], emotion: parts[1] });
    }
  }
  return out;
}

/* ------------------------------------------------------------ 主题 / 密度 */

function themeList() {
  var t = (state.prefs.themes && state.prefs.themes.length) ? state.prefs.themes
        : (state.overview && state.overview.themes) ? state.overview.themes : [];
  return t.length ? t : [{ id: "moonlit", name: "月夜", hint: "", dark: true }];
}

function applyTheme(id, persist) {
  var list = themeList();
  var hit = null;
  for (var i = 0; i < list.length; i++) if (list[i].id === id) hit = list[i];
  if (!hit) hit = list[0];
  state.prefs.theme = hit.id;
  D.documentElement.setAttribute("data-theme", hit.id);
  var sel = $("theme-select");
  if (sel && sel.value !== hit.id) sel.value = hit.id;
  renderStatus();
  if (state.tab === "about") renderAbout();
  if (persist) savePrefs();
}

function applyDensity(d, persist) {
  state.prefs.density = d === "compact" ? "compact" : "comfortable";
  D.documentElement.setAttribute("data-density", state.prefs.density);
  var lab = $("density-label");
  if (lab) lab.textContent = state.prefs.density === "compact" ? "紧凑" : "宽松";
  renderStatus();
  if (persist) savePrefs();
}

var prefTimer = null;
function savePrefs() {
  if (prefTimer) clearTimeout(prefTimer);
  prefTimer = setTimeout(function () {
    prefTimer = null;
    apiPost("prefs/save", {
      theme: state.prefs.theme, density: state.prefs.density, tab: state.tab,
      log_paint: !!state.logsUI.paint
    })
      .catch(function () { /* 偏好保存失败不打扰用户 */ });
  }, 420);
}

/* ------------------------------------------------------------ tab 栏 */

function tabBadge(id) {
  var o = state.overview;
  if (!o) return null;
  if (id === "emotions") {
    var w = Number(o.counts && o.counts.warnings) || 0;
    return { text: String((o.counts && o.counts.emotions) || 0), tone: w > 0 ? "warn" : null };
  }
  if (id === "packs") return { text: String(o.packs || 0), tone: null };
  if (id === "favorites") {
    var fv = Number(o.counts && o.counts.favorites) || 0;
    return fv ? { text: String(fv), tone: null } : null;
  }
  if (id === "servers") return { text: String(o.servers || 0), tone: null };
  if (id === "commands") return { text: String((o.counts && o.counts.commands) || 0), tone: null };
  if (id === "sessions") {
    var s = o.session || {};
    var n = Number(s.known_sessions) || 0;
    return n ? { text: String(n), tone: null } : null;
  }
  /* 日志页的角标直接摆「值得看一眼的条数」：优先失败/跳过，其次 WARNING 以上。 */
  if (id === "logs") {
    var rl = o.run_log || {};
    if (!rl.available) return null;
    var bad = (Number(rl.failed) || 0) + (Number(rl.skipped) || 0);
    if (bad > 0) return { text: String(bad), tone: "danger" };
    var iss = Number(rl.issues) || 0;
    if (iss > 0) return { text: String(iss), tone: "warn" };
    var size = Number(rl.synth_size) || 0;
    return size ? { text: String(size), tone: null } : null;
  }
  return null;
}

function renderTabbar() {
  var bar = clear($("tabbar"));
  TABS.forEach(function (t) {
    var kids = [h("span", { class: "tab-ico", "aria-hidden": "true", text: t.ico }), h("span", { text: t.label })];
    var b = tabBadge(t.id);
    if (b) kids.push(h("span", { class: "tab-badge", "data-tone": b.tone, text: b.text }));
    bar.appendChild(h("button", {
      type: "button", class: "tab", id: "tab-" + t.id, role: "tab",
      "aria-selected": state.tab === t.id ? "true" : "false",
      "aria-controls": "view-" + t.id,
      onclick: function () { go(t.id); }
    }, kids));
  });
}

/* ------------------------------------------------------------ 状态条 */

function sep() { return h("span", { class: "sep", text: "·" }); }

function renderStatus() {
  var o = state.overview;
  var left = clear($("status-left"));
  var right = clear($("status-right"));
  if (!o) { left.appendChild(h("span", { text: "正在连接 AstrBot…" })); return; }
  var c = o.counts || {};
  var s = o.stats || {};
  append(left, [
    "角色 ", h("b", { text: String(c.characters || 0) }), sep(),
    "感情 ", h("b", { text: String(c.emotions || 0) }), sep(),
    "请求 ", h("b", { text: String(s.requests || 0) }), sep(),
    "成功 ", h("b", { text: String(s.succeeded || 0) }), sep(),
    "失败 ", h("b", { text: String(s.failed || 0) }), sep(),
    "队列 ", h("b", { text: String(s.queue_size || 0) })
  ]);
  var themeName = state.prefs.theme;
  themeList().forEach(function (t) { if (t.id === state.prefs.theme) themeName = t.name; });
  append(right, [
    h("span", { text: themeName }), sep(),
    h("span", { text: state.prefs.density === "compact" ? "紧凑" : "宽松" }), sep(),
    "服务器 ", h("b", { text: String(o.servers || 0) }), sep(),
    h("span", { text: (o.plugin && o.plugin.name) || "astrbot" }), sep(),
    h("b", { text: "v" + ((o.plugin && o.plugin.version) || "?") })
  ]);
}

/* ------------------------------------------------------------ 导航 / 加载 */

var VIEWS = {
  studio: renderStudio, emotions: renderEmotions, packs: renderPacks, favorites: renderFavorites, config: renderConfig,
  servers: renderServers, sessions: renderSessions, commands: renderCommands,
  logs: renderLogs, about: renderAbout
};

var LOADERS = {
  emotions: function () { return apiGet("emotions").then(function (d) { state.emotions = d; }); },
  packs: function () { return apiGet("packs").then(function (d) { state.packs = d; }); },
  favorites: function () { return apiGet("favorites", favParams()).then(function (d) { state.favorites = d; }); },
  config: function () { return apiGet("config").then(function (d) { state.config = d; state.cfg.dirty = {}; }); },
  servers: function () { return apiGet("servers").then(function (d) { state.servers = d; }); },
  sessions: function () { return apiGet("sessions").then(function (d) { state.sessions = d; }); },
  commands: function () { return apiGet("commands").then(function (d) { state.commands = d; }); },
  logs: function () { return fetchLogs(); }
};

function viewNode(id) { return $("view-" + id); }

function busyView(id, msg) {
  var v = clear(viewNode(id));
  v.appendChild(h("div", { class: "card" }, h("div", { class: "row-tight" }, [h("span", { class: "spinner" }), dim(msg || "正在加载…")])));
}

function go(tab) {
  if (!VIEWS[tab]) tab = "studio";
  var changed = state.tab !== tab;
  state.tab = tab;
  if (changed && (tab === "sessions" || tab === "logs")) state.loaded[tab] = false;
  TABS.forEach(function (t) {
    var v = viewNode(t.id);
    if (v) v.hidden = t.id !== tab;
    var b = $("tab-" + t.id);
    if (b) b.setAttribute("aria-selected", t.id === tab ? "true" : "false");
  });
  if (changed) scrollTopSafe();
  if (changed && tab !== "logs") stopLogAudio();
  savePrefs();
  if (LOADERS[tab] && !state.loaded[tab]) {
    busyView(tab, "正在读取数据…");
    state.loaded[tab] = true;
    LOADERS[tab]()
      .then(function () { VIEWS[tab](); })
      .catch(function (e) {
        state.loaded[tab] = false;
        var v = clear(viewNode(tab));
        v.appendChild(card({
          kicker: "ERROR", title: "读取失败",
          body: [note(e && e.message ? e.message : String(e), "danger"),
                 h("div", { class: "btnrow" }, btn("重试", { kind: "soft", onclick: function () { go(tab); } }))]
        }));
      });
    return;
  }
  VIEWS[tab]();
}

function reloadTab(tab, silent) {
  tab = tab || state.tab;
  var jobs = [apiGet("overview").then(function (d) { state.overview = d; })];
  if (LOADERS[tab]) jobs.push(LOADERS[tab]());
  return Promise.all(jobs).then(function () {
    renderTabbar();
    renderStatus();
    VIEWS[tab]();
    if (!silent) toast("已刷新", "ok", 1800);
  }).catch(function (e) { fail(e, "刷新失败"); });
}

function refreshOverview() {
  return apiGet("overview").then(function (d) {
    state.overview = d;
    renderTabbar();
    renderStatus();
  }).catch(function () {});
}

/* ------------------------------------------------------------ 启动 */

function bindChrome() {
  var sel = clear($("theme-select"));
  themeList().forEach(function (t) {
    sel.appendChild(h("option", { value: t.id, text: t.name + (t.dark ? " · 暗" : " · 亮") }));
  });
  sel.value = state.prefs.theme;
  sel.addEventListener("change", function () { applyTheme(sel.value, true); });

  $("density-toggle").addEventListener("click", function () {
    applyDensity(state.prefs.density === "compact" ? "comfortable" : "compact", true);
  });

  $("refresh-btn").addEventListener("click", function () {
    var b = $("refresh-btn");
    b.disabled = true;
    reloadTab(state.tab).then(function () { b.disabled = false; });
  });

  $("reset-btn").addEventListener("click", function () {
    confirmModal("清空本页草稿？", "会清掉工作台的文本草稿、试听历史和预览结果。感情库、配置和服务器上的数据都不会动。", { danger: true, okText: "清空" })
      .then(function (ok) {
        if (!ok) return;
        state.studio.text = "";
        state.studio.history = [];
        state.studio.preview = null;
        state.studio.result = null;
        state.pack.importText = "";
        state.pack.report = null;
        toast("本页草稿已清空", "ok");
        go(state.tab);
      });
  });

  $("modal-cancel").addEventListener("click", function () { closeModal(false); });
  $("modal-mask").addEventListener("click", function () { closeModal(false); });
  $("modal-ok").addEventListener("click", function () {
    if (modalState.onOk) {
      var r = modalState.onOk();
      if (r === false) return;
      closeModal(r === undefined ? true : r);
      return;
    }
    closeModal(true);
  });
  D.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !$("modal").hidden) closeModal(false);
  });
}

function bootFail(msg) {
  var boot = $("boot");
  if (!boot) return;
  clear(boot);
  boot.appendChild(h("div", { class: "card", style: "max-width:520px" }, [
    kicker("BRIDGE ERROR"),
    h("h2", { class: "card-title", text: "无法连接插件页桥" }),
    rule(),
    note(msg, "danger"),
    h("p", { class: "card-desc", text: "请确认这是从 AstrBot WebUI 的「插件 → 管理页面」打开的，而不是直接访问 HTML 文件。" })
  ]));
}

async function init() {
  if (!SDK || typeof SDK.ready !== "function") {
    bootFail("window.AstrBotPluginPage 不存在。bridge-sdk.js 没有加载成功。");
    return;
  }
  try { await SDK.ready(); } catch (e) {
    bootFail("SDK.ready() 失败：" + (e && e.message ? e.message : String(e)));
    return;
  }
  var prefs = null;
  var overview = null;
  try {
    var got = await Promise.all([
      apiGet("prefs").catch(function () { return null; }),
      apiGet("overview")
    ]);
    prefs = got[0];
    overview = got[1];
  } catch (e) {
    bootFail("读取插件数据失败：" + (e && e.message ? e.message : String(e)));
    return;
  }
  if (prefs) state.prefs = Object.assign(state.prefs, prefs);
  /* 日志着色开关也存在偏好里，缺省为开（老快照里没有这个键）。 */
  if (prefs && prefs.log_paint !== undefined) state.logsUI.paint = !!prefs.log_paint;
  state.overview = overview;
  if (overview && overview.defaults) {
    state.studio.character = overview.defaults.character || "";
    state.studio.emotion = overview.defaults.emotion || "";
    state.studio.language = overview.defaults.language || "";
  }
  bindChrome();
  applyTheme(state.prefs.theme, false);
  applyDensity(state.prefs.density, false);
  renderTabbar();
  renderStatus();
  var startTab = VIEWS[state.prefs.tab] ? state.prefs.tab : "studio";
  $("boot").hidden = true;
  go(startTab);
}

/* =====================================================================
   1) 工作台
   ===================================================================== */

LOADERS.studio = function () { return apiGet("emotions").then(function (d) { state.emotions = d; }); };

function emoRows() { return (state.emotions && state.emotions.rows) ? state.emotions.rows : []; }
function emoChars() { return (state.emotions && state.emotions.characters) ? state.emotions.characters : []; }

function emotionsOf(character) {
  return emoRows().filter(function (r) { return r.character === character; });
}

function rowFor(character, emotion) {
  var rows = emoRows();
  for (var i = 0; i < rows.length; i++) if (rows[i].character === character && rows[i].emotion === emotion) return rows[i];
  return null;
}

function normalizeStudioPick() {
  var s = state.studio;
  var chars = emoChars();
  if (!chars.length) { s.character = ""; s.emotion = ""; return; }
  var names = chars.map(function (c) { return c.name; });
  if (names.indexOf(s.character) < 0) s.character = names[0];
  var list = emotionsOf(s.character).map(function (r) { return r.emotion; });
  if (list.indexOf(s.emotion) < 0) s.emotion = list.length ? list[0] : "";
}

function insertAtCursor(ta, text) {
  var start = ta.selectionStart === undefined ? ta.value.length : ta.selectionStart;
  var end = ta.selectionEnd === undefined ? ta.value.length : ta.selectionEnd;
  ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
  var pos = start + text.length;
  try { ta.setSelectionRange(pos, pos); } catch (e) {}
  ta.focus();
  var ev = D.createEvent ? D.createEvent("Event") : null;
  if (ev) { ev.initEvent("input", true, true); ta.dispatchEvent(ev); }
  else { ta.dispatchEvent(new Event("input")); }
}

function langOptions() {
  var langs = (state.emotions && state.emotions.languages) ? state.emotions.languages.slice() : ["jp", "zh", "en"];
  var out = [{ value: "", label: "跟随音色 / 默认" }];
  var names = { jp: "日语 jp", zh: "中文 zh", en: "英语 en" };
  langs.forEach(function (l) { out.push({ value: l, label: names[l] || l }); });
  return out;
}

function renderStudio() {
  var v = clear(viewNode("studio"));
  var s = state.studio;
  var o = state.overview || {};
  var lim = o.limits || {};
  var tog = o.toggles || {};
  normalizeStudioPick();
  if (!s.adv) {
    s.adv = {
      enable_sentence_splitting: tog.sentence_splitting !== false,
      sentences_per_chunk: Number(lim.sentences_per_chunk) || 2,
      chunk_gap_ms: Number(lim.chunk_gap_ms) || 260,
      enable_custom_pause_marker: tog.custom_pause_marker !== false
    };
  }

  var ui = {};
  var synthLimit = Number(lim.synth_text_limit) || 1500;

  /* ---------- 音色选择 ---------- */
  var chars = emoChars();
  var charSel = select(chars.map(function (c) { return { value: c.name, label: c.name + "（" + c.count + "）" }; }), s.character, function () {
    s.character = charSel.value;
    var list = emotionsOf(s.character).map(function (r) { return r.emotion; });
    s.emotion = list.length ? list[0] : "";
    refillEmotion();
    syncVoice();
  });
  var emoSel = select([], s.emotion, function () { s.emotion = emoSel.value; syncVoice(); });
  function refillEmotion() {
    clear(emoSel);
    var list = emotionsOf(s.character);
    if (!list.length) emoSel.appendChild(h("option", { value: "", text: "（该角色没有感情）" }));
    list.forEach(function (r) {
      emoSel.appendChild(h("option", { value: r.emotion, text: r.emotion + (r.warning ? "  ⚠" : "") }));
    });
    emoSel.value = s.emotion || "";
  }
  refillEmotion();

  var langSel = select(langOptions(), s.language, function () { s.language = langSel.value; });

  var freeSwitch = switchBox(s.freeRef, "手动指定参考音频（试听未登记的候选）", function (e) {
    s.freeRef = e.target.checked;
    renderStudio();
  });

  ui.ref = h("div", { class: "ref-card" });
  ui.name = h("span", { class: "vn-name" });

  function syncVoice() {
    var row = rowFor(s.character, s.emotion);
    clear(ui.name);
    append(ui.name, [
      h("span", { text: s.character || "未选择角色" }),
      h("span", { class: "vn-name-sep", text: "·" }),
      h("span", { class: "vn-name-emo", text: s.emotion || "无感情" })
    ]);
    clear(ui.ref);
    if (s.freeRef) {
      var p = input(s.refPath, null, { mono: true, placeholder: "相对路径，例如 kisaki/happy.wav", oninput: function (e) { s.refPath = e.target.value; } });
      var t = input(s.refText, null, { placeholder: "参考音频里念的那句原文", oninput: function (e) { s.refText = e.target.value; } });
      append(ui.ref, [
        field({ label: "参考音频路径", control: p, desc: "相对 Space 工作目录，不能以 / 开头也不能含 .." }),
        h("div", { style: "height:8px" }),
        field({ label: "参考文本", control: t, desc: "留空则由服务端自行判断，识别质量会下降" })
      ]);
      return;
    }
    if (!row) {
      ui.ref.appendChild(note("当前角色下没有可用的感情，先去「感情库」登记一条。", "warn"));
      return;
    }
    ui.ref.appendChild(kv([
      ["参考音频", row.ref_audio_path],
      ["参考文本", shorten(row.ref_audio_text, 120)],
      ["语言", row.language || "（默认）"]
    ]));
    if (row.warning) ui.ref.appendChild(note(row.warning, "warn"));
  }

  /* ---------- VN 对话框 ---------- */
  var ta = h("textarea", { class: "vn-text", placeholder: "在这里写要合成的台词……" + nl() + "支持 [pause=600] 手动停顿，句末标点和「……」会自动补停顿。", spellcheck: "false" });
  ta.value = s.text || "";
  ui.count = h("span", { class: "vn-count" });
  function syncCount() {
    var n = ta.value.length;
    ui.count.textContent = n + " / " + synthLimit + " 字";
    ui.count.setAttribute("data-tone", n > synthLimit ? "danger" : (n > synthLimit * 0.8 ? "warn" : ""));
  }
  ta.addEventListener("input", function () { s.text = ta.value; syncCount(); });
  syncCount();

  var tools = h("div", { class: "vn-tools" }, [
    btn("短停 300", { sm: true, title: "插入 [pause=300]", onclick: function () { insertAtCursor(ta, "[pause=300]"); } }),
    btn("中停 600", { sm: true, title: "插入 [pause=600]", onclick: function () { insertAtCursor(ta, "[pause=600]"); } }),
    btn("长停 1200", { sm: true, title: "插入 [pause=1200]", onclick: function () { insertAtCursor(ta, "[pause=1200]"); } }),
    btn("……", { sm: true, title: "插入省略号，会自动补一段较长的停顿", onclick: function () { insertAtCursor(ta, "……"); } }),
    btn("。", { sm: true, title: "补句末标点，句末才会补满段间停顿", onclick: function () { insertAtCursor(ta, "。"); } }),
    btn("复制", { sm: true, kind: "ghost", onclick: function () { copyText(ta.value, "台词已复制"); } }),
    btn("清空", { sm: true, kind: "ghost", onclick: function () { ta.value = ""; s.text = ""; syncCount(); ta.focus(); } }),
    ui.count
  ]);

  var vn = h("div", { class: "vn-box" }, [ui.name, ta, h("span", { class: "vn-cursor", "aria-hidden": "true", text: "▼" }), tools]);

  /* ---------- 动作按钮 ---------- */
  ui.previewBtn = btn("分段预览", { kind: "soft", onclick: doPreview });
  ui.synthBtn = btn("合成并试听", { kind: "primary", onclick: doSynth });
  var actions = h("div", { class: "btnrow" }, [ui.synthBtn, ui.previewBtn, h("span", { class: "spacer" }), ui.status = h("span", { class: "dim tiny" })]);

  /* ---------- 预览试算参数 ---------- */
  var adv = h("div", { class: "grid-4" }, [
    field({ label: "分句", control: switchBox(s.adv.enable_sentence_splitting, "启用", function (e) { s.adv.enable_sentence_splitting = e.target.checked; }) }),
    field({ label: "每段句数", control: input(s.adv.sentences_per_chunk, null, { type: "number", min: 1, max: 20, mono: true, oninput: function (e) { s.adv.sentences_per_chunk = Number(e.target.value) || 1; } }) }),
    field({ label: "段间停顿 ms", control: input(s.adv.chunk_gap_ms, null, { type: "number", min: 0, max: Number(lim.max_chunk_gap_ms) || 2000, step: 20, mono: true, oninput: function (e) { s.adv.chunk_gap_ms = Number(e.target.value) || 0; } }) }),
    field({ label: "[pause] 标记", control: switchBox(s.adv.enable_custom_pause_marker, "启用", function (e) { s.adv.enable_custom_pause_marker = e.target.checked; }) })
  ]);

  var mainCard = card({
    kicker: "SYNTH DESK",
    title: "语音合成工作台",
    desc: "选音色 → 写台词 → 分段预览 → 直接在浏览器里试听。合成走的是和聊天里完全一样的那条链路。",
    tools: [badge("队列 " + ((o.stats && o.stats.queue_size) || 0), (o.stats && o.stats.queue_size) ? "warn" : null),
            badge("上限 " + synthLimit + " 字")],
    body: [
      h("div", { class: "voice-head" }, [
        field({ label: "角色", control: charSel }),
        field({ label: "感情", control: emoSel }),
        field({ label: "语言", control: langSel })
      ]),
      h("div", { class: "row-tight" }, freeSwitch),
      ui.ref,
      vn,
      actions,
      h("details", {}, [
        h("summary", { class: "field-label", style: "cursor:pointer", text: "预览试算参数（只影响下面的分段预览，不写入配置）" }),
        h("div", { style: "margin-top:10px" }, adv)
      ])
    ]
  });

  ui.previewCard = h("div");
  ui.resultCard = h("div");

  /* ---------- 侧栏 ---------- */
  /* RUNTIME 卡只重建这一格，不整体重渲分区：
     整体重渲会把试听区的 <audio> 换成新节点，正在播放的语音会被打断。 */
  ui.statGrid = h("div", { class: "stat-grid" });
  function syncStats() {
    var st = (state.overview || {}).stats || {};
    clear(ui.statGrid);
    append(ui.statGrid, [
      stat(st.requests || 0, "总请求", "accent"),
      stat(st.succeeded || 0, "成功", "ok"),
      stat(st.failed || 0, "失败", (st.failed ? "danger" : null)),
      stat(st.skipped_no_speech || 0, "无可读内容"),
      stat(st.leak_guard_hits || 0, "泄漏拦截", (st.leak_guard_hits ? "warn" : null)),
      stat(st.truncation_guard_hits || 0, "截断拦截", (st.truncation_guard_hits ? "warn" : null)),
      stat(st.text_truncated || 0, "文本超长", (st.text_truncated ? "warn" : null)),
      stat(st.empty_result_retries || 0, "空结果重试", (st.empty_result_retries ? "warn" : null)),
      stat(st.queue_size || 0, "排队中")
    ]);
  }
  var side = h("aside", { class: "side" }, [
    card({
      kicker: "RUNTIME", title: "运行统计", sub: false,
      tools: [btn("⟳", { sm: true, kind: "ghost", title: "只刷新统计", onclick: function () { refreshOverview().then(syncStats); } })],
      body: [ui.statGrid]
    }),
    card({
      kicker: "LIMITS", title: "限额与开关",
      body: [
        kv([
          ["文本上限", (lim.max_text_length || "—") + " 字"],
          ["试听上限", synthLimit + " 字"],
          ["超时", (lim.timeout_seconds || "—") + "s"],
          ["重试", lim.max_retries],
          ["段间停顿", fmtMs(lim.chunk_gap_ms)],
          ["每段句数", lim.sentences_per_chunk],
          ["尾部静音", fmtMs(lim.tail_padding_ms)],
          ["采样率", (lim.sample_rate || 32000) + " Hz"]
        ]),
        h("div", { class: "chips" }, [
          chip((tog.sentence_splitting ? "分句 开" : "分句 关"), tog.sentence_splitting ? "chip-ok" : "chip-warn"),
          chip((tog.custom_pause_marker ? "[pause] 开" : "[pause] 关"), tog.custom_pause_marker ? "chip-ok" : "chip-warn"),
          chip((tog.text_cleaning ? "清洗 开" : "清洗 关"), tog.text_cleaning ? "chip-ok" : ""),
          chip((tog.translation ? "翻译 开" : "翻译 关"), tog.translation ? "chip-ok" : ""),
          chip((tog.leak_guard ? "泄漏防护 开" : "泄漏防护 关"), tog.leak_guard ? "chip-ok" : "chip-warn"),
          chip((tog.truncation_guard ? "截断防护 开" : "截断防护 关"), tog.truncation_guard ? "chip-ok" : "chip-warn")
        ])
      ]
    }),
    ui.histCard = h("div")
  ]);

  v.appendChild(h("div", { class: "split" }, [
    h("div", { class: "grow", style: "display:flex;flex-direction:column;gap:var(--gap)" }, [mainCard, ui.previewCard, ui.resultCard]),
    side
  ]));

  syncStats();
  syncVoice();
  renderPreviewCard();
  renderResultCard();
  renderHistory();

  /* ---------- 预览 ---------- */
  function renderPreviewCard() {
    clear(ui.previewCard);
    var p = s.preview;
    if (!p) return;
    var body = [];
    if (p.blocked) body.push(note(p.blocked, "warn"));
    if (p.steps && p.steps.length) {
      var steps = h("ol", { class: "steps" });
      p.steps.forEach(function (x) {
        steps.appendChild(h("li", { class: "step" }, [
          h("div", { class: "step-name", text: x.name }),
          h("div", { class: "step-detail", text: x.detail })
        ]));
      });
      body.push(h("div", {}, [h("p", { class: "kicker", text: "PIPELINE" }), steps]));
    }
    var chunks = p.chunks || [];
    if (chunks.length) {
      var list = h("div", { class: "chunk-list" });
      chunks.forEach(function (ck, i) {
        if (i > 0) {
          var g = ck.gap_before_ms || 0;
          list.appendChild(h("div", { class: "gap-link" }, h("span", {
            class: "gap-tag", "data-tone": g > 0 ? null : "mute",
            text: g > 0 ? "+" + g + "ms 停顿" : "无停顿（切在句中）"
          })));
        }
        var metas = [
          badge(ck.chars + " 字"),
          badge("可读 " + ck.pronounceable, ck.pronounceable ? null : "warn")
        ];
        if (ck.custom_pause_ms) metas.push(badge("[pause] " + ck.custom_pause_ms + "ms", "accent"));
        if (ck.auto_pause_ms) metas.push(badge("自动停顿 " + ck.auto_pause_ms + "ms"));
        if (ck.tail) metas.push(badge("句末 " + ck.tail));
        if (!ck.voiceable) metas.push(badge("不会发声", "danger"));
        list.appendChild(h("div", { class: "chunk", "data-mute": ck.voiceable ? null : "true" }, [
          h("span", { class: "chunk-no", text: String(ck.index === undefined ? i + 1 : ck.index) }),
          h("div", { class: "chunk-main" }, [
            h("div", { class: "chunk-text", text: ck.text }),
            h("div", { class: "chunk-meta" }, metas)
          ])
        ]));
      });
      body.push(h("div", {}, [h("p", { class: "kicker", text: "CHUNKS" }), list]));
    } else if (!p.blocked) {
      body.push(empty("没有可发声的分段", "文本清洗后没剩下能读的内容。"));
    }
    var tt = p.totals;
    if (tt) {
      body.push(h("div", { class: "totals" }, [
        badge("分段 " + tt.chunks, "accent"),
        badge("总字数 " + tt.chars),
        badge("可读 " + tt.pronounceable),
        badge("段间停顿 " + fmtMs(tt.chunk_gap_ms)),
        badge("[pause] " + fmtMs(tt.custom_pause_ms)),
        badge("自动停顿 " + fmtMs(tt.auto_pause_ms)),
        badge("停顿合计 " + fmtMs(tt.pause_total_ms), tt.pause_total_ms ? "accent" : null),
        badge("预计时长 " + fmtSec(tt.expected_seconds))
      ]));
    }
    if (p.truncated) body.push(note("文本超过上限已被截断，实际合成也会被截断。", "warn"));
    ui.previewCard.appendChild(card({
      kicker: "PREVIEW", title: "分段与停顿预览",
      desc: "这是合成前的完整预处理链：剥标记 → 清洗 → 截断 → 补句末标点 → 可读性检查 → 分段。",
      tools: [btn("复制分段文本", { sm: true, kind: "ghost", onclick: function () {
        copyText(chunks.map(function (ck, i) { return "#" + (i + 1) + "  " + ck.text; }).join(nl()), "分段文本已复制");
      } })],
      body: body
    }));
  }

  function doPreview() {
    if (s.previewing) return;
    var text = ta.value.trim();
    if (!text) { toast("先写点台词再预览", "warn"); return; }
    s.previewing = true;
    ui.previewBtn.disabled = true;
    ui.status.textContent = "正在试算分段…";
    apiPost("preview", {
      text: text,
      language: s.language || "",
      enable_sentence_splitting: !!s.adv.enable_sentence_splitting,
      sentences_per_chunk: s.adv.sentences_per_chunk,
      chunk_gap_ms: s.adv.chunk_gap_ms,
      enable_custom_pause_marker: !!s.adv.enable_custom_pause_marker
    }).then(function (d) {
      s.preview = d || {};
      renderPreviewCard();
      ui.status.textContent = d && d.totals ? ("已试算：" + d.totals.chunks + " 段 / 预计 " + fmtSec(d.totals.expected_seconds)) : "已试算";
    }).catch(function (e) { fail(e, "预览失败"); ui.status.textContent = ""; })
      .then(function () { s.previewing = false; ui.previewBtn.disabled = false; });
  }

  /* ---------- 合成 ---------- */
  function renderResultCard() {
    clear(ui.resultCard);
    var r = s.result;
    if (!r) return;
    var kids = [];
    if (r.audio_base64) {
      var au = h("audio", { controls: true, preload: "metadata" });
      au.src = "data:" + (r.mime || "audio/wav") + ";base64," + r.audio_base64;
      kids.push(au);
    } else {
      kids.push(note("音频体积超过内联上限（12 MB），没法在浏览器里直接播。文件已生成在服务器上：" + (r.path || "?"), "warn"));
    }
    kids.push(h("div", { class: "player-meta" }, [
      badge(r.character + " · " + r.emotion, "accent"),
      badge("时长 " + fmtSec(r.duration_seconds)),
      badge("预计 " + fmtSec(r.expected_seconds)),
      badge("耗时 " + fmtSec(r.elapsed_seconds), r.elapsed_seconds > 20 ? "warn" : "ok"),
      badge(fmtBytes(r.bytes)),
      badge("队列 " + (r.queue_size || 0)),
      badge(r.language || "默认语言")
    ]));
    ui.resultCard.appendChild(card({
      kicker: "AUDITION", title: "试听",
      desc: "文件名 " + (r.filename || "—") + " · 生成于 " + fmtTime(r.created_at),
      tools: [
        btn("存入收藏", { sm: true, kind: "soft", title: "把这条音频原样收进语音收藏库", onclick: function () { favSaveSynth(r); } }),
        btn("复制服务器路径", { sm: true, kind: "ghost", onclick: function () { copyText(r.path, "路径已复制"); } })
      ],
      body: [h("div", { class: "player" }, [h("div", { class: "player-title" }, [h("span", { text: "🔊" }), h("span", { text: shorten(r.text, 60) })])].concat(kids))]
    }));
  }

  function renderHistory() {
    clear(ui.histCard);
    var list = h("div", { class: "hist" });
    if (!s.history.length) list.appendChild(empty("还没有试听记录", "合成过的音频会留在这里，可以随时重播。"));
    s.history.forEach(function (it, idx) {
      list.appendChild(h("div", { class: "hist-item" }, [
        h("div", { class: "hist-body" }, [
          h("div", { class: "hist-line", text: shorten(it.text, 42) }),
          h("div", { class: "hist-sub", text: it.character + " · " + it.emotion + " · " + fmtSec(it.duration) + " · " + fmtBytes(it.bytes) })
        ]),
        btn("▶", { sm: true, kind: "soft", title: "重新播放", onclick: function () {
          s.result = it.raw;
          renderResultCard();
          toast("已载入到试听区", "ok", 1600);
        } }),
        btn("↺", { sm: true, kind: "ghost", title: "把台词填回工作台", onclick: function () {
          ta.value = it.text; s.text = it.text; syncCount(); ta.focus();
        } })
      ]));
    });
    ui.histCard.appendChild(card({
      kicker: "HISTORY", title: "本页历史",
      desc: "只存在这个页面的内存里，刷新就没了。",
      tools: s.history.length ? [btn("清空", { sm: true, kind: "ghost", onclick: function () { s.history = []; renderHistory(); } })] : null,
      body: [list]
    }));
  }

  function doSynth() {
    if (s.synthing) return;
    var text = ta.value.trim();
    if (!text) { toast("先写点台词", "warn"); return; }
    if (text.length > synthLimit) { toast("试听最多 " + synthLimit + " 字，现在 " + text.length + " 字", "warn"); return; }
    var payload = { character: s.character, emotion: s.emotion, text: text, language: s.language || "" };
    if (s.freeRef) {
      if (!s.refPath) { toast("手动模式下必须填参考音频路径", "warn"); return; }
      payload.ref_audio_path = s.refPath;
      payload.ref_audio_text = s.refText;
      if (!payload.character) payload.character = (emoChars()[0] || {}).name || "";
    } else if (!s.character || !s.emotion) {
      toast("先选一个角色和感情", "warn");
      return;
    }
    s.synthing = true;
    ui.synthBtn.disabled = true;
    clear(ui.synthBtn);
    append(ui.synthBtn, [h("span", { class: "spinner" }), h("span", { text: "合成中…" })]);
    ui.status.textContent = "已排队，服务端正在生成…";
    var t0 = Date.now();
    apiPost("synthesize", payload).then(function (d) {
      s.result = d;
      renderResultCard();
      s.history.unshift({
        text: d.text || text, character: d.character, emotion: d.emotion,
        duration: d.duration_seconds, bytes: d.bytes, raw: d
      });
      while (s.history.length > 12) s.history.pop();
      renderHistory();
      ui.status.textContent = "完成，耗时 " + fmtSec(d.elapsed_seconds || (Date.now() - t0) / 1000);
      toast("合成成功：" + fmtSec(d.duration_seconds) + " / " + fmtBytes(d.bytes), "ok");
    }).catch(function (e) { fail(e, "合成失败"); ui.status.textContent = ""; })
      .then(function () {
        s.synthing = false;
        ui.synthBtn.disabled = false;
        clear(ui.synthBtn);
        append(ui.synthBtn, "合成并试听");
        /* 成功和失败都要刷：失败也会让「失败」计数 +1。
           refreshOverview() 内部已吞掉异常，这里不会再抛。 */
        return refreshOverview().then(syncStats);
      });
  }
}

/* =====================================================================
   2) 感情库
   ===================================================================== */

function formModal(opts) {
  var p = openModal(opts);
  modalState.onOk = opts.onOk || null;
  return p;
}

function applyEmotionRows(d) {
  if (!state.emotions) state.emotions = {};
  state.emotions.rows = d.rows || [];
  state.emotions.warnings = d.warnings || 0;
  var map = {};
  var order = [];
  state.emotions.rows.forEach(function (r) {
    if (map[r.character] === undefined) { map[r.character] = 0; order.push(r.character); }
    map[r.character] += 1;
  });
  order.sort();
  state.emotions.characters = order.map(function (n) { return { name: n, count: map[n] }; });
  state.loaded.packs = false;
  refreshOverview();
}

function charOptions(includeBlank) {
  var out = includeBlank ? [{ value: "", label: "（全部角色）" }] : [];
  emoChars().forEach(function (c) { out.push({ value: c.name, label: c.name + "（" + c.count + "）" }); });
  return out;
}

function emotionFormBody(row, ctl) {
  ctl.character = input(row ? row.character : (state.emo.ch || state.studio.character || ""), null, { placeholder: "角色名，例如 kisaki" });
  ctl.emotion = input(row ? row.emotion : "", null, { placeholder: "感情名，例如 悲伤" });
  ctl.path = input(row ? row.ref_audio_path : "", null, { mono: true, placeholder: "kisaki/sad_01.wav" });
  ctl.text = textarea(row ? row.ref_audio_text : "", null, { rows: 3, placeholder: "参考音频里念的那句原文，越准越像" });
  ctl.lang = select(langOptions(), row ? (row.language || "") : "", null);
  return [
    field({ label: "角色", control: ctl.character, desc: "同名会归到同一个角色下" }),
    field({ label: "感情", control: ctl.emotion }),
    field({ label: "参考音频路径", control: ctl.path, desc: "相对 Space 工作目录，必须是相对路径，不能含 .." }),
    field({ label: "参考文本", control: ctl.text }),
    field({ label: "语言", control: ctl.lang, desc: "留空表示跟随全局默认语言" })
  ];
}

function openEmotionForm(row) {
  var ctl = {};
  formModal({
    kicker: row ? "EDIT" : "NEW",
    title: row ? "编辑感情 · " + row.character + " / " + row.emotion : "登记一条新感情",
    body: emotionFormBody(row, ctl),
    okText: "保存",
    onOk: function () {
      var c = ctl.character.value.trim();
      var e = ctl.emotion.value.trim();
      var p = ctl.path.value.trim();
      if (!c || !e) { toast("角色名和感情名都不能空", "warn"); return false; }
      if (!p) { toast("参考音频路径不能空", "warn"); return false; }
      if (p.charAt(0) === "/" || p.indexOf("..") >= 0 || p.indexOf(":") >= 0) { toast("必须是相对路径且不能含 ..", "warn"); return false; }
      return {
        character: c, emotion: e, ref_audio_path: p,
        ref_audio_text: ctl.text.value.trim(), language: ctl.lang.value,
        original_character: row ? row.character : "", original_emotion: row ? row.emotion : "",
        overwrite: true
      };
    }
  }).then(function (payload) {
    if (!payload) return;
    apiPost("emotions/upsert", payload).then(function (d) {
      applyEmotionRows(d);
      toast((d.renamed ? "已改名并保存：" : "已保存：") + d.saved.character + " / " + d.saved.emotion, "ok");
      renderEmotions();
    }).catch(function (e) { fail(e, "保存失败"); });
  });
}

function deleteEmotions(items, label) {
  confirmModal("删除 " + items.length + " 条感情？", (label || items.map(function (i) { return i.character + " / " + i.emotion; }).join("、")) + nl() + "只会从 emotions.json 里移除登记，参考音频文件本身不会被删。", { danger: true, okText: "删除" })
    .then(function (ok) {
      if (!ok) return;
      apiPost("emotions/delete", { items: items }).then(function (d) {
        applyEmotionRows(d);
        state.emo.picked = {};
        toast("已删除 " + d.removed.length + " 条" + (d.missing && d.missing.length ? "，" + d.missing.length + " 条本来就不存在" : ""), "ok");
        renderEmotions();
      }).catch(function (e) { fail(e, "删除失败"); });
    });
}

function openCopyForm(items) {
  var ctl = {};
  ctl.target = input("", null, { placeholder: "目标角色名（可以是新角色）" });
  ctl.pick = select(charOptions(true), "", function () { if (ctl.pick.value) ctl.target.value = ctl.pick.value; });
  var moveOn = false;
  var owOn = false;
  formModal({
    kicker: "COPY / MOVE",
    title: "把 " + items.length + " 条感情复制或移动到别的角色",
    body: [
      field({ label: "从已有角色里选", control: ctl.pick }),
      field({ label: "目标角色", control: ctl.target, desc: "填一个不存在的名字就会新建这个角色" }),
      h("div", { class: "row-tight", style: "margin-top:12px;gap:16px" }, [
        switchBox(false, "移动（源角色下会删掉）", function (e) { moveOn = e.target.checked; }),
        switchBox(false, "覆盖同名感情", function (e) { owOn = e.target.checked; })
      ])
    ],
    okText: "执行",
    onOk: function () {
      var t = ctl.target.value.trim();
      if (!t) { toast("目标角色不能空", "warn"); return false; }
      return { items: items, target_character: t, move: moveOn, overwrite: owOn };
    }
  }).then(function (payload) {
    if (!payload) return;
    apiPost("emotions/copy", payload).then(function (d) {
      applyEmotionRows(d);
      state.emo.picked = {};
      var msg = (d.moved ? "已移动 " : "已复制 ") + d.copied.length + " 条";
      if (d.skipped && d.skipped.length) msg += "，跳过 " + d.skipped.length + " 条（" + d.skipped[0].reason + "）";
      toast(msg, "ok");
      renderEmotions();
    }).catch(function (e) { fail(e, "操作失败"); });
  });
}

function openRenameChar(name) {
  var ctl = {};
  ctl.name = input(name, null, { placeholder: "新的角色名" });
  var mergeOn = false;
  formModal({
    kicker: "RENAME",
    title: "重命名角色 · " + name,
    body: [
      field({ label: "新角色名", control: ctl.name }),
      h("div", { style: "margin-top:12px" }, switchBox(false, "目标角色已存在时合并进去", function (e) { mergeOn = e.target.checked; })),
      note("改名后，配置里的「默认角色」如果还写着旧名字，需要自己去配置页改一下。", "warn")
    ],
    okText: "重命名",
    onOk: function () {
      var n = ctl.name.value.trim();
      if (!n) { toast("新名字不能空", "warn"); return false; }
      if (n === name) { toast("名字没变", "warn"); return false; }
      return { character: name, new_name: n, merge: mergeOn };
    }
  }).then(function (payload) {
    if (!payload) return;
    apiPost("emotions/rename-character", payload).then(function (d) {
      applyEmotionRows(d);
      if (state.emo.ch === payload.character) state.emo.ch = payload.new_name;
      if (state.studio.character === payload.character) state.studio.character = payload.new_name;
      toast("已重命名：" + d.renamed.from + " → " + d.renamed.to, "ok");
      renderEmotions();
    }).catch(function (e) { fail(e, "重命名失败"); });
  });
}

function deleteCharacter(name, count) {
  confirmModal("删掉整个角色 " + name + "？", "会移除它名下全部 " + count + " 条感情登记。参考音频文件本身不动。", { danger: true, okText: "全部删除" })
    .then(function (ok) {
      if (!ok) return;
      apiPost("emotions/delete", { character: name }).then(function (d) {
        applyEmotionRows(d);
        if (state.emo.ch === name) state.emo.ch = "";
        toast("已删除角色 " + name + "（" + d.removed.length + " 条）", "ok");
        renderEmotions();
      }).catch(function (e) { fail(e, "删除失败"); });
    });
}

function renderEmotions() {
  var v = clear(viewNode("emotions"));
  var e = state.emotions || { rows: [], characters: [] };
  var st = state.emo;

  var q = st.q.trim().toLowerCase();
  var rows = (e.rows || []).filter(function (r) {
    if (st.ch && r.character !== st.ch) return false;
    if (!q) return true;
    return (r.character + " " + r.emotion + " " + r.ref_audio_path + " " + (r.ref_audio_text || "")).toLowerCase().indexOf(q) >= 0;
  });

  var picked = pickedKeys();

  /* ---------- 搜索 / 筛选 ---------- */
  var qInput = input(st.q, null, { placeholder: "搜角色 / 感情 / 路径 / 参考文本…", oninput: function (ev) {
    st.q = ev.target.value;
    renderEmotions();
    var n = $("emo-q");
    if (n) { n.focus(); try { n.setSelectionRange(n.value.length, n.value.length); } catch (x) {} }
  } });
  qInput.id = "emo-q";

  var chipsBox = h("div", { class: "chips" });
  chipsBox.appendChild(h("button", {
    type: "button", class: "chip chip-btn", "aria-pressed": st.ch ? "false" : "true",
    text: "全部 " + (e.rows || []).length, onclick: function () { st.ch = ""; renderEmotions(); }
  }));
  (e.characters || []).forEach(function (c) {
    chipsBox.appendChild(h("button", {
      type: "button", class: "chip chip-btn", "aria-pressed": st.ch === c.name ? "true" : "false",
      text: c.name + " " + c.count, onclick: function () { st.ch = st.ch === c.name ? "" : c.name; renderEmotions(); }
    }));
  });

  /* ---------- 批量条 ---------- */
  var batch = h("div", { class: "row-tight", hidden: picked.length === 0 }, [
    badge("已选 " + picked.length + " 条", "accent"),
    btn("导出所选", { sm: true, kind: "soft", onclick: function () { exportEmotions({ items: picked }); } }),
    btn("复制 / 移动", { sm: true, onclick: function () { openCopyForm(picked); } }),
    btn("存为快照", { sm: true, onclick: function () { savePackFrom({ items: picked }); } }),
    btn("删除所选", { sm: true, kind: "danger", onclick: function () { deleteEmotions(picked); } }),
    btn("取消选择", { sm: true, kind: "ghost", onclick: function () { st.picked = {}; renderEmotions(); } })
  ]);

  /* ---------- 表格 ---------- */
  var allOn = rows.length > 0 && rows.every(function (r) { return st.picked[r.character + SEP + r.emotion]; });
  var headPick = h("input", { type: "checkbox", checked: allOn, "aria-label": "全选", onchange: function (ev) {
    rows.forEach(function (r) { st.picked[r.character + SEP + r.emotion] = ev.target.checked; });
    renderEmotions();
  } });
  var t = table([{ label: "", class: "col-pick" }, "角色", "感情", "参考音频", "参考文本", "语言", { label: "操作", class: "acts" }], ["col-pick"]);
  t.querySelector("thead th").appendChild(headPick);

  if (!rows.length) {
    v.appendChild(card({
      kicker: "EMOTIONS", title: "感情库",
      desc: "emotions.json：" + (e.file || "—"),
      tools: [btn("＋ 新增感情", { sm: true, kind: "primary", onclick: function () { openEmotionForm(null); } })],
      body: [
        h("div", { class: "row-tight" }, [h("span", { class: "grow" }, qInput)]),
        chipsBox,
        empty((e.rows || []).length ? "没有匹配的结果" : "感情库还是空的", (e.rows || []).length ? "换个关键词，或者点上面的「全部」。" : "点右上角「新增感情」登记第一条参考音频。")
      ]
    }));
  } else {
    rows.forEach(function (r) {
      var key = r.character + SEP + r.emotion;
      var tr = h("tr", { "data-selected": st.picked[key] ? "true" : null, "data-warn": r.warning ? "true" : null });
      tr.appendChild(h("td", {}, h("input", {
        type: "checkbox", checked: !!st.picked[key], "aria-label": "选择 " + r.character + " " + r.emotion,
        onchange: function (ev) { st.picked[key] = ev.target.checked; tr.setAttribute("data-selected", ev.target.checked ? "true" : ""); renderEmotions(); }
      })));
      tr.appendChild(h("td", {}, h("b", { text: r.character })));
      tr.appendChild(h("td", {}, [h("span", { text: r.emotion }), r.warning ? h("span", { class: "badge", "data-tone": "warn", text: "⚠", title: r.warning }) : null]));
      tr.appendChild(h("td", { class: "cell-path", title: r.ref_audio_path }, r.ref_audio_path));
      tr.appendChild(h("td", { class: "cell-text", title: r.ref_audio_text || "" }, r.ref_audio_text || "—"));
      tr.appendChild(h("td", {}, badge(r.language || "默认")));
      tr.appendChild(h("td", { class: "acts" }, [
        btn("试听", { sm: true, kind: "soft", title: "带着这个音色跳到工作台", onclick: function () {
          state.studio.character = r.character;
          state.studio.emotion = r.emotion;
          state.studio.freeRef = false;
          go("studio");
          toast("已切到工作台：" + r.character + " / " + r.emotion, "ok", 2000);
        } }),
        btn("编辑", { sm: true, onclick: function () { openEmotionForm(r); } }),
        btn("复制", { sm: true, kind: "ghost", onclick: function () { openCopyForm([{ character: r.character, emotion: r.emotion }]); } }),
        btn("删除", { sm: true, kind: "danger", onclick: function () { deleteEmotions([{ character: r.character, emotion: r.emotion }], r.character + " / " + r.emotion); } })
      ]));
      t.body.appendChild(tr);
    });

    var body = [];
    if (e.warnings) body.push(note("有 " + e.warnings + " 条登记存在问题（路径为空、绝对路径或含 ..）。表格里带 ⚠ 的就是，建议改掉，否则合成时会失败。", "warn"));
    body.push(h("div", { class: "row-tight" }, [h("span", { class: "grow" }, qInput), badge(rows.length + " / " + (e.rows || []).length + " 条")]));
    body.push(chipsBox);
    body.push(batch);
    body.push(t);
    v.appendChild(card({
      kicker: "EMOTIONS", title: "感情库",
      desc: "emotions.json：" + (e.file || "—"),
      tools: [
        btn("＋ 新增感情", { sm: true, kind: "primary", onclick: function () { openEmotionForm(null); } }),
        btn("导出全部", { sm: true, kind: "soft", onclick: function () { exportEmotions({}); } }),
        btn("导入", { sm: true, onclick: function () { go("packs"); } })
      ],
      body: body
    }));
  }

  /* ---------- 角色管理 ---------- */
  var chBody = [];
  if (!(e.characters || []).length) {
    chBody.push(empty("还没有角色", "登记一条感情就会自动建出角色。"));
  } else {
    var ct = table(["角色", "感情数", { label: "操作", class: "acts" }]);
    (e.characters || []).forEach(function (c) {
      ct.body.appendChild(h("tr", {}, [
        h("td", {}, h("b", { text: c.name })),
        h("td", {}, badge(c.count + " 条", "accent")),
        h("td", { class: "acts" }, [
          btn("只看它", { sm: true, kind: "ghost", onclick: function () { state.emo.ch = c.name; renderEmotions(); } }),
          btn("导出", { sm: true, kind: "soft", onclick: function () { exportEmotions({ characters: [c.name] }); } }),
          btn("重命名", { sm: true, onclick: function () { openRenameChar(c.name); } }),
          btn("删除", { sm: true, kind: "danger", onclick: function () { deleteCharacter(c.name, c.count); } })
        ])
      ]));
    });
    chBody.push(ct);
  }
  v.appendChild(card({
    kicker: "CHARACTERS", title: "角色",
    desc: "重命名会把这个角色名下所有感情一起搬过去。",
    body: chBody
  }));
}


/* =====================================================================
   3) 感情包 · 导入 / 导出 / 快照
   ===================================================================== */

LOADERS.packs = function () {
  var jobs = [apiGet("packs").then(function (d) { state.packs = d; })];
  if (!state.emotions) {
    jobs.push(apiGet("emotions").then(function (d) { state.emotions = d; state.loaded.emotions = true; }));
  }
  return Promise.all(jobs);
};

function packState() {
  var p = state.pack;
  if (!p.scope) p.scope = "all";
  if (!p.chars) p.chars = {};
  if (!p.mode) p.mode = (state.emotions && state.emotions.default_import_mode) || "merge";
  if (p.dry === undefined || p.dry === null) p.dry = true;
  return p;
}

function importModeOptions() {
  var raw = (state.emotions && state.emotions.import_modes) || ["merge", "overwrite", "replace"];
  var labels = { merge: "合并 · 只补新", overwrite: "覆盖 · 冲突用新值", replace: "替换 · 先清空" };
  return raw.map(function (m) { return { value: m, label: labels[m] || m }; });
}

function modeHint(mode) {
  if (mode === "replace") return note("替换：先清空现有 emotions.json 再整份写入，包里没有的条目会被丢弃。建议先试运行，或先存一份快照。", "danger");
  if (mode === "overwrite") return note("覆盖：同名条目用包里的值替换；包里没有的条目保持不动。", "warn");
  return note("合并：只补新条目，同名冲突保留现有值（记进「跳过」）。最安全，推荐默认用它。", "info");
}

/* ---------- 范围 ---------- */

function scopeChars() {
  var p = packState();
  var out = [];
  emoChars().forEach(function (c) { if (p.chars[c.name]) out.push(c.name); });
  return out;
}

function currentScope() {
  var p = packState();
  if (p.scope === "picked") return { items: pickedKeys() };
  if (p.scope === "chars") return { characters: scopeChars() };
  return {};
}

function scopeLabel(sc) {
  sc = sc || {};
  if (sc.items) return "感情库勾选的 " + sc.items.length + " 条";
  if (sc.characters) return sc.characters.length ? "角色 " + sc.characters.join("、") : "未选角色";
  return "全部感情";
}

function scopeCount(sc) {
  sc = sc || {};
  if (sc.items) return sc.items.length;
  if (sc.characters) {
    var n = 0;
    emoChars().forEach(function (c) { if (sc.characters.indexOf(c.name) >= 0) n += c.count; });
    return n;
  }
  return emoRows().length;
}

function scopeReady(sc) {
  sc = sc || {};
  if (sc.items) return sc.items.length > 0;
  if (sc.characters) return sc.characters.length > 0;
  return emoRows().length > 0;
}

function normScope(opts) {
  opts = opts || {};
  var sc = {};
  if (opts.items) sc.items = opts.items;
  if (opts.characters) sc.characters = opts.characters;
  return sc;
}

function exportQuery(sc, memo, filename) {
  var q = {};
  if (sc.characters && sc.characters.length) q.characters = sc.characters.join(",");
  if (sc.items && sc.items.length) q.items = JSON.stringify(sc.items);
  if (memo) q.note = memo;
  if (filename) q.filename = filename;
  return q;
}

function exportBody(sc, memo, filename) {
  return {
    characters: sc.characters || [],
    items: sc.items || [],
    note: memo || "",
    filename: filename || ""
  };
}

/* ---------- 导出 ---------- */

function exportEmotions(opts) {
  opts = opts || {};
  var sc = normScope(opts);
  if (!scopeReady(sc)) { toast("这个范围里没有可导出的条目", "warn"); return Promise.resolve(null); }
  return apiPost("emotions/export-preview", exportBody(sc, opts.note, opts.filename)).then(function (d) {
    var okMsg = "已导出 " + d.filename + " · " + d.summary.characters + " 角色 / " + d.summary.emotions + " 条 · " + fmtBytes(d.bytes);
    var task = null;
    try { task = SDK.download("emotions/export", exportQuery(sc, opts.note, d.filename), d.filename); }
    catch (e) { fail(e, "下载失败"); return d; }
    if (task && typeof task.then === "function") task.then(function () { toast(okMsg, "ok", 4600); }, function (e) { fail(e, "下载失败"); });
    else toast(okMsg, "ok", 4600);
    return d;
  }).catch(function (e) { return fail(e, "导出失败"); });
}

function previewExportText(opts) {
  opts = opts || {};
  var sc = normScope(opts);
  if (!scopeReady(sc)) { toast("这个范围里没有可导出的条目", "warn"); return; }
  apiPost("emotions/export-preview", exportBody(sc, opts.note, opts.filename)).then(function (d) {
    var box = textarea(d.text, null, { mono: true, rows: 15 });
    openModal({
      kicker: "EXPORT TEXT",
      title: d.filename,
      body: [
        kv([["范围", scopeLabel(sc)], ["角色 / 感情", d.summary.characters + " / " + d.summary.emotions], ["体积", fmtBytes(d.bytes)]]),
        box,
        note("浏览器不给剪贴板权限时，直接在文本框里全选复制也一样。", "info")
      ],
      okText: "复制全文",
      cancelText: "关闭"
    }).then(function (ok) { if (ok) copyText(d.text, "导出文本已复制"); });
    setTimeout(function () { try { box.focus(); box.setSelectionRange(0, 0); } catch (e) {} }, 60);
  }).catch(function (e) { fail(e, "生成导出文本失败"); });
}

function savePackFrom(opts) {
  opts = opts || {};
  var sc = normScope(opts);
  if (!scopeReady(sc)) { toast("这个范围里没有可保存的条目", "warn"); return; }
  var ctl = {};
  var over = { on: false };
  ctl.name = input(opts.filename || "", null, { mono: true, placeholder: "留空自动命名，例如 emotions-20260903.json" });
  ctl.memo = input(opts.note || "", null, { placeholder: "备注，例如：上线前基线" });
  formModal({
    kicker: "SNAPSHOT",
    title: "存为快照 · " + scopeLabel(sc),
    body: [
      note("快照写在插件数据目录（不进仓库），随时可以恢复或下载。", "info"),
      field({ label: "文件名", control: ctl.name, hint: "非法字符会被替换，后缀自动补 .json" }),
      field({ label: "备注", control: ctl.memo }),
      field({ label: "同名处理", control: switchBox(false, "允许覆盖同名快照", function (e) { over.on = e.target.checked; }) })
    ],
    okText: "保存",
    onOk: function () {
      return {
        characters: sc.characters || [],
        items: sc.items || [],
        filename: ctl.name.value.trim(),
        note: ctl.memo.value.trim(),
        overwrite: over.on
      };
    }
  }).then(function (payload) {
    if (!payload) return;
    apiPost("packs/save", payload).then(function (d) {
      state.packs = Object.assign({}, state.packs || {}, { packs: d.packs || [] });
      toast("已保存快照 " + d.filename + "（" + d.summary.characters + " 角色 / " + d.summary.emotions + " 条）", "ok", 4600);
      refreshOverview();
      if (state.tab === "packs") renderPacks();
    }).catch(function (e) { fail(e, "保存快照失败"); });
  });
}

/* ---------- 变更报告 ---------- */

/* 五个桶（新增/更新/跳过/移除/无效）装的内容量差得极大：
   一次导入里常见的形态是「4 个桶是空的、1 个桶 1 条」，也可能是
   「added 39 条、每条带一串 reference_audio/xxx.ogg 长路径」。
   所以这里不再画五个等宽固定列：
     · 空桶不建列，收成底下一行灰色 pill（.diff-quiet）；
     · 有内容的列按条目数分档（data-load=light/mid/heavy）抢宽度；
     · 每条拆成「名字行 + 原因行 + 新旧值行」，长文本断行 + 两行截断 + title 兜全文；
     · 超过 COLLAPSED_ROWS 条先折叠，展开后列内滚动，不让报告把整页顶飞。 */

var COLLAPSED_ROWS = 10;

var DIFF_FIELDS = [
  ["ref_audio_path", "参考音频"],
  ["ref_audio_text", "参考文本"],
  ["language", "语言"]
];

function baseName(v) {
  v = v === null || v === undefined ? "" : String(v);
  var i = Math.max(v.lastIndexOf("/"), v.lastIndexOf("\\"));
  return i >= 0 ? v.slice(i + 1) : v;
}

/* 老版本对 updated 只会写死一句「文本/语言有变」，或者把前后两条完整路径
   各截 18 字 —— 参考音频全在 reference_audio/ 下，前缀一样，截完两边一模一样，
   等于什么都没说。现在按字段逐个比，路径只比文件名，全文进 title。 */
function diffDetail(before, after) {
  before = before || {};
  after = after || {};
  var labels = [];
  var pairs = [];
  var full = [];
  DIFF_FIELDS.forEach(function (f) {
    var key = f[0];
    var b = before[key] === null || before[key] === undefined ? "" : String(before[key]);
    var a = after[key] === null || after[key] === undefined ? "" : String(after[key]);
    if (b === a) return;
    labels.push(f[1]);
    var bs = key === "ref_audio_path" ? baseName(b) : b;
    var as = key === "ref_audio_path" ? baseName(a) : a;
    pairs.push(shorten(bs || "空", 26) + " → " + shorten(as || "空", 26));
    full.push(f[1] + "：" + (b || "（空）") + "  →  " + (a || "（空）"));
  });
  if (!labels.length) return { why: "内容有变", pair: "", full: "" };
  return { why: labels.join(" / ") + "有变", pair: pairs.join(" · "), full: full.join("\n") };
}

/* 一条条目里能给的信息，取决于它落在哪个桶：
     added   —— 只有新值，写清楚这条新感情指向哪个参考音频；
     removed —— 只有旧值，写清楚即将被清掉的是什么（replace 模式下最要紧）；
     skipped —— 新旧都有但没采用，得说明「保留了现有值」并把被忽略的值摆出来，
                 用户看到才知道要不要换成覆盖模式；
     updated —— 新旧都有且已生效，逐字段给 旧 → 新；
     invalid —— 只有 reason。 */
function entryLine(entry) {
  entry = entry || {};
  var parts = [];
  var file = baseName(entry.ref_audio_path);
  if (file) parts.push(shorten(file, 30));
  if (entry.language) parts.push(String(entry.language));
  if (entry.ref_audio_text) parts.push("「" + shorten(String(entry.ref_audio_text), 16) + "」");
  return parts.join(" · ");
}

function entryFull(entry) {
  entry = entry || {};
  return DIFF_FIELDS.map(function (f) {
    var v = entry[f[0]];
    return f[1] + "：" + (v === null || v === undefined || v === "" ? "（空）" : String(v));
  }).join("\n");
}

function diffItem(it, kind) {
  var name = String(it.character || "?") + " · " + String(it.emotion || "整个角色");
  var li = h("li", { class: "diff-item" }, h("span", { class: "diff-name", title: name, text: name }));
  var pair = "";
  var pairTitle = "";
  if (it.reason) {
    var reason = String(it.reason);
    li.appendChild(h("span", { class: "diff-why", title: reason, text: reason }));
  } else if (kind === "skipped" && it.before && it.after) {
    li.appendChild(h("span", { class: "diff-why", text: "已存在，保留现有值" }));
    pair = "现有 " + (entryLine(it.before) || "空") + "  ｜  包内 " + (entryLine(it.after) || "空");
    pairTitle = "现有\n" + entryFull(it.before) + "\n\n包内（未采用）\n" + entryFull(it.after);
  } else if (it.before && it.after) {
    var d = diffDetail(it.before, it.after);
    li.appendChild(h("span", { class: "diff-why", title: d.full || d.why, text: d.why }));
    pair = d.pair;
    pairTitle = d.full || d.pair;
  } else if (it.after || it.before) {
    var one = it.after || it.before;
    pair = entryLine(one);
    pairTitle = entryFull(one);
  }
  if (pair) li.appendChild(h("span", { class: "diff-pair", title: pairTitle || pair, text: pair }));
  return li;
}

function diffLoad(n) { return n >= 8 ? "heavy" : (n >= 3 ? "mid" : "light"); }

function diffCol(kind, label, items) {
  items = items || [];
  var col = h("div", { class: "diff-col", "data-kind": kind, "data-load": diffLoad(items.length) });
  col.appendChild(h("div", { class: "diff-head" }, [
    h("span", { text: label }),
    h("b", { text: String(items.length) })
  ]));

  var list = h("ul", { class: "diff-list", "data-open": "false" });
  var rest = [];
  if (!items.length) {
    list.appendChild(h("li", { class: "diff-item" }, dim("—")));
  } else {
    items.forEach(function (it, i) {
      var li = diffItem(it, kind);
      if (i >= COLLAPSED_ROWS) { li.hidden = true; rest.push(li); }
      list.appendChild(li);
    });
  }
  col.appendChild(list);

  if (rest.length) {
    var open = false;
    var more = btn("展开全部 " + items.length + " 条", {
      sm: true, kind: "ghost", class: "diff-more",
      onclick: function () {
        open = !open;
        for (var i = 0; i < rest.length; i++) rest[i].hidden = !open;
        list.dataset.open = open ? "true" : "false";
        more.textContent = open ? "只看前 " + COLLAPSED_ROWS + " 条" : "展开全部 " + items.length + " 条";
      }
    });
    col.appendChild(more);
  }
  return col;
}

function reportCard(report, opts) {
  opts = opts || {};
  var c = report.counts || {};
  var modeText = { merge: "合并", overwrite: "覆盖", replace: "替换" }[report.mode] || String(report.mode || "?");
  var head = [];
  if (report.dry_run) head.push(note("这是试运行，emotions.json 没有被写入。确认没问题后关掉「试运行」再执行一次。", "warn"));
  else if (!report.changed) head.push(note("没有任何实际变更，文件未改动。", "info"));
  else head.push(note("已写入 emotions.json，现在共 " + ((report.result || {}).characters || 0) + " 角色 / " + ((report.result || {}).emotions || 0) + " 条感情。", "ok"));

  var meta = report.meta || {};
  var metaRows = [
    ["模式", modeText + (report.dry_run ? " · 试运行" : "")],
    report.filename ? ["来源快照", report.filename] : null,
    meta.format ? ["包格式", String(meta.format) + (meta.version ? " v" + meta.version : "")] : null,
    meta.exported_at ? ["导出时间", String(meta.exported_at)] : null,
    meta.plugin_version ? ["导出自插件", "v" + String(meta.plugin_version)] : null,
    meta.source ? ["来源", String(meta.source)] : null,
    meta.note ? ["备注", String(meta.note)] : null
  ];

  var buckets = [
    ["added", "新增", report.added],
    ["updated", "更新", report.updated],
    ["skipped", "跳过", report.skipped],
    ["removed", "移除", report.removed],
    ["invalid", "无效", report.invalid]
  ];
  var diffBox = h("div", { class: "diff" });
  var quiet = h("p", { class: "diff-quiet" });
  var liveCount = 0;
  var quietCount = 0;
  buckets.forEach(function (b) {
    var items = b[2] || [];
    if (items.length) { liveCount += 1; diffBox.appendChild(diffCol(b[0], b[1], items)); return; }
    quietCount += 1;
    quiet.appendChild(h("i", { "data-kind": b[0], text: b[1] + " 0" }));
  });
  var diffZone = [];
  if (liveCount) diffZone.push(diffBox);
  if (quietCount) {
    quiet.insertBefore(h("span", { text: liveCount ? "其余分类没有条目：" : "五个分类都没有条目，这份包对现有数据毫无影响：" }), quiet.firstChild);
    diffZone.push(quiet);
  }
  if (c.unchanged) diffZone.push(h("p", { class: "diff-quiet" }, h("span", {
    text: "另有 " + c.unchanged + " 条和现有数据完全一致，未列出。"
  })));

  return card({
    sub: true,
    kicker: opts.kicker || "REPORT",
    title: opts.title || "变更报告",
    tools: report.summary_text ? [btn("复制摘要", { sm: true, kind: "ghost", onclick: function () { copyText(report.summary_text, "摘要已复制"); } })] : null,
    body: [
      h("div", { class: "stat-grid" }, [
        stat(c.added || 0, "新增", c.added ? "ok" : null),
        stat(c.updated || 0, "更新", c.updated ? "accent" : null),
        stat(c.skipped || 0, "跳过", c.skipped ? "warn" : null),
        stat(c.unchanged || 0, "无变化", null),
        stat(c.removed || 0, "移除", c.removed ? "danger" : null),
        stat(c.invalid || 0, "无效", c.invalid ? "danger" : null)
      ]),
      head,
      kv(metaRows),
      diffZone
    ]
  });
}

/* ---------- 导入 ---------- */

function applyImportResult(d, label) {
  var report = d.report || {};
  state.pack.report = report;
  if (!report.dry_run && report.changed) applyEmotionRows(d);
  else { refreshOverview(); }
  var c = report.counts || {};
  var tone = report.dry_run ? "warn" : (report.changed ? "ok" : "info");
  toast((label || "导入") + (report.dry_run ? " 试运行完成" : " 完成") + " · 新增 " + (c.added || 0) + " / 更新 " + (c.updated || 0) + " / 跳过 " + (c.skipped || 0) + " / 移除 " + (c.removed || 0), tone, 5200);
}

function runImport(dryRun) {
  var p = packState();
  var text = String(p.importText || "").trim();
  if (!text) { toast("先粘贴或拖入一份感情包", "warn"); return; }
  var go2 = function () {
    p.busy = true;
    renderPacks();
    apiPost("emotions/import", { text: text, mode: p.mode, dry_run: !!dryRun })
      .then(function (d) { p.busy = false; applyImportResult(d, "导入"); renderPacks(); })
      .catch(function (e) { p.busy = false; renderPacks(); fail(e, "导入失败"); });
  };
  if (dryRun || p.mode !== "replace") { go2(); return; }
  confirmModal("确认用替换模式导入？", "替换模式会先清空当前 emotions.json，包里没有的条目全部丢弃。这一步不可撤销，建议先存快照。", { danger: true, okText: "我确认，替换" })
    .then(function (ok) { if (ok) go2(); });
}

function restorePack(item, mode, dryRun) {
  var run = function () {
    apiPost("packs/restore", { filename: item.filename, mode: mode, dry_run: !!dryRun })
      .then(function (d) { applyImportResult(d, "恢复 " + item.filename); renderPacks(); })
      .catch(function (e) { fail(e, "恢复失败"); });
  };
  if (dryRun || mode !== "replace") { run(); return; }
  confirmModal("用替换模式恢复 " + item.filename + "？", "会先清空当前 emotions.json，再整份写入这个快照的内容。不可撤销。", { danger: true, okText: "替换恢复" })
    .then(function (ok) { if (ok) run(); });
}

function openRestoreForm(item) {
  var pick = { mode: packState().mode, dry: true };
  var host = h("div", {});
  var draw = function () {
    clear(host);
    append(host, [
      segment(importModeOptions(), pick.mode, function (v) { pick.mode = v; draw(); }),
      h("div", { style: "margin-top:10px" }, modeHint(pick.mode))
    ]);
  };
  draw();
  formModal({
    kicker: "RESTORE",
    title: "恢复快照 · " + item.filename,
    body: [
      kv([
        ["角色 / 感情", (item.characters || 0) + " / " + (item.emotions || 0)],
        ["体积", fmtBytes(item.bytes || 0)],
        ["修改时间", item.modified || "—"],
        item.note ? ["备注", item.note] : null
      ]),
      field({ label: "合并模式", control: host }),
      field({ label: "试运行", control: switchBox(true, "只看变更，不写文件", function (e) { pick.dry = e.target.checked; }) })
    ],
    okText: "执行",
    onOk: function () { return { mode: pick.mode, dry: pick.dry }; }
  }).then(function (r) {
    if (!r) return;
    restorePack(item, r.mode, r.dry);
  });
}

function deletePack(item) {
  confirmModal("删除快照 " + item.filename + "？", "文件会从插件数据目录里删掉，无法找回。当前 emotions.json 不受影响。", { danger: true, okText: "删除" })
    .then(function (ok) {
      if (!ok) return;
      apiPost("packs/delete", { filename: item.filename }).then(function (d) {
        state.packs = Object.assign({}, state.packs || {}, { packs: d.packs || [] });
        toast("已删除 " + d.deleted, "ok");
        refreshOverview();
        renderPacks();
      }).catch(function (e) { fail(e, "删除失败"); });
    });
}

function downloadPack(item) {
  try {
    var task = SDK.download("packs/download", { filename: item.filename }, item.filename);
    if (task && typeof task.then === "function") task.then(function () { toast("已下载 " + item.filename, "ok"); }, function (e) { fail(e, "下载失败"); });
    else toast("已下载 " + item.filename, "ok");
  } catch (e) { fail(e, "下载失败"); }
}

/* ---------- 拖放区 ---------- */

function dropZone(onText) {
  var picker = h("input", { type: "file", accept: ".json,application/json" });
  var zone = h("div", { class: "drop", tabindex: "0", role: "button" }, [
    h("b", { text: "把 .json 感情包拖进来" }),
    h("span", { text: "或点这里选文件 · 上限 4MB · 完整感情包和裸 emotions.json 都吃" }),
    picker
  ]);
  function read(file) {
    if (!file) return;
    if (file.size > 4 * 1024 * 1024) { toast("文件超过 4MB，已拒绝", "danger"); return; }
    var fr = new FileReader();
    fr.onload = function () { onText(String(fr.result || ""), file.name); };
    fr.onerror = function () { toast("读取文件失败", "danger"); };
    try { fr.readAsText(file, "utf-8"); } catch (e) { fail(e, "读取文件失败"); }
  }
  zone.addEventListener("click", function (e) { if (e.target !== picker) picker.click(); });
  zone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); }
  });
  picker.addEventListener("change", function () {
    read(picker.files && picker.files[0]);
    picker.value = "";
  });
  ["dragenter", "dragover"].forEach(function (n) {
    zone.addEventListener(n, function (e) { e.preventDefault(); e.stopPropagation(); zone.dataset.over = "true"; });
  });
  ["dragleave", "dragend"].forEach(function (n) {
    zone.addEventListener(n, function (e) { e.preventDefault(); zone.dataset.over = "false"; });
  });
  zone.addEventListener("drop", function (e) {
    e.preventDefault();
    e.stopPropagation();
    zone.dataset.over = "false";
    var dt = e.dataTransfer;
    if (!dt) return;
    if (dt.files && dt.files.length) { read(dt.files[0]); return; }
    var txt = "";
    try { txt = dt.getData("text/plain") || ""; } catch (err) { txt = ""; }
    if (txt) onText(txt, "");
  });
  return zone;
}

/* ---------- 页面 ---------- */

function renderPacks() {
  var v = clear(viewNode("packs"));
  var p = packState();
  var d = state.packs || { packs: [], directory: "" };
  var chars = emoChars();
  var rows = emoRows();
  var sc = currentScope();
  var ready = scopeReady(sc);

  /* ===== 导出 ===== */
  var scopeBox = h("div", {}, segment([
    { value: "all", label: "全部 · " + rows.length },
    { value: "picked", label: "感情库勾选 · " + pickedKeys().length },
    { value: "chars", label: "按角色挑" }
  ], p.scope, function (val) { p.scope = val; renderPacks(); }));

  var exBody = [
    field({ label: "导出范围", control: scopeBox, desc: "「感情库勾选」用的是感情库页面表格里打勾的那些条目。" })
  ];

  if (p.scope === "chars") {
    var picker = h("div", { class: "chips" });
    if (!chars.length) picker.appendChild(dim("还没有角色，先去感情库登记一条。"));
    chars.forEach(function (c) {
      var b = h("button", {
        type: "button",
        class: "chip chip-btn",
        "aria-pressed": p.chars[c.name] ? "true" : "false",
        text: c.name + " · " + c.count
      });
      b.addEventListener("click", function () { p.chars[c.name] = !p.chars[c.name]; renderPacks(); });
      picker.appendChild(b);
    });
    var picks = [picker];
    if (chars.length > 1) {
      picks.push(h("div", { class: "btnrow" }, [
        btn("全选", { sm: true, kind: "ghost", onclick: function () { chars.forEach(function (c) { p.chars[c.name] = true; }); renderPacks(); } }),
        btn("清空", { sm: true, kind: "ghost", onclick: function () { p.chars = {}; renderPacks(); } })
      ]));
    }
    exBody.push(field({ label: "挑角色", control: picks }));
  }

  if (p.scope === "picked" && !pickedKeys().length) {
    exBody.push(note("感情库里还没有勾选任何条目。切到「感情库」页面，用行首的复选框选中要导出的感情。", "warn"));
  }

  exBody.push(h("div", { class: "grid-2" }, [
    field({ label: "备注", control: input(p.note, null, { placeholder: "会写进包头 note，方便日后认出这是什么", oninput: function (e) { p.note = e.target.value; } }) }),
    field({ label: "文件名", control: input(p.filename, null, { mono: true, placeholder: "留空自动命名", oninput: function (e) { p.filename = e.target.value; } }) })
  ]));

  exBody.push(kv([
    ["当前范围", scopeLabel(sc)],
    ["条目数", String(scopeCount(sc)) + " 条感情"]
  ]));

  exBody.push(h("div", { class: "btnrow" }, [
    btn("下载 .json", { kind: "primary", disabled: !ready, onclick: function () { exportEmotions({ characters: sc.characters, items: sc.items, note: p.note, filename: p.filename }); } }),
    btn("预览 / 复制文本", { kind: "soft", disabled: !ready, onclick: function () { previewExportText({ characters: sc.characters, items: sc.items, note: p.note, filename: p.filename }); } }),
    btn("存为服务端快照", { disabled: !ready, onclick: function () { savePackFrom({ characters: sc.characters, items: sc.items, note: p.note, filename: p.filename }); } })
  ]));

  v.appendChild(card({
    kicker: "EXPORT",
    title: "导出感情包",
    desc: "导出的是一份带头部信息的 JSON，可以直接发给别人，或者当作模板备份。",
    tools: [btn("导出全部", { sm: true, kind: "ghost", onclick: function () { exportEmotions({}); } })],
    body: exBody
  }));

  /* ===== 导入 ===== */
  /* 这里以前是「oninput 只写 p.importText，不重渲染」：
     结果粘完 JSON，试运行 / 执行导入 / 直接写入 / 清空内容 / 格式化 JSON 五个按钮
     还是渲染时那份 disabled 状态，字数提示也一直停在「还是空的」，
     非得再去点一下合并模式或试运行开关才活过来。
     但整块重渲染会把 textarea 连焦点带光标一起换掉，所以按 syncCount 的老路子
     只定点刷这几个节点。 */
  var imChip = chip(p.fileName || "", "chip-mono chip-accent");
  var imHint = h("p", { class: "field-hint" });
  var imClear = btn("清空内容", { sm: true, kind: "ghost", onclick: function () { p.importText = ""; p.fileName = ""; p.report = null; renderPacks(); } });
  var imFormat = btn("格式化 JSON", { sm: true, kind: "ghost", onclick: function () {
    try {
      p.importText = JSON.stringify(JSON.parse(String(p.importText)), null, 2);
      renderPacks();
      toast("已格式化", "ok", 1800);
    } catch (e) { toast("这不是合法 JSON：" + (e && e.message ? e.message : String(e)), "danger"); }
  } });
  var imRun = btn(p.busy ? "处理中…" : (p.dry ? "试运行" : "执行导入"), {
    kind: p.dry ? "soft" : "primary",
    onclick: function () { runImport(p.dry); }
  });
  var imWrite = p.dry ? btn("直接写入", { kind: "primary", onclick: function () { runImport(false); } }) : null;

  function syncImport() {
    var raw = String(p.importText || "");
    var has = !!raw.trim();
    imHint.textContent = raw.length ? fmtBytes(raw.length) + " · " + raw.length + " 字符" : "还是空的";
    imChip.textContent = p.fileName || "";
    imChip.hidden = !p.fileName;
    imClear.disabled = !raw.length;
    imFormat.disabled = !raw.length;
    imRun.disabled = !!p.busy || !has;
    if (imWrite) imWrite.disabled = !!p.busy || !has;
  }

  var ta = textarea(p.importText, null, {
    mono: true, rows: 9,
    placeholder: "把感情包 JSON 粘在这里，也可以直接粘裸 emotions.json（{角色:{感情:{...}}}）",
    oninput: function (e) { p.importText = e.target.value; if (p.fileName) { p.fileName = ""; } syncImport(); }
  });
  syncImport();

  var zone = dropZone(function (text, name) {
    p.importText = text;
    p.fileName = name || "";
    p.report = null;
    renderPacks();
    toast(name ? "已读入 " + name + "（" + fmtBytes(text.length) + "）" : "已读入拖放的文本", "ok");
  });

  var imBody = [
    h("div", { class: "io-panel" }, [
      h("div", {}, [
        field({ label: "感情包内容", tag: imChip, control: ta, hintNode: imHint })
      ]),
      h("div", {}, [
        field({ label: "从文件读入", control: zone }),
        h("div", { class: "btnrow" }, [imClear, imFormat])
      ])
    ]),
    field({ label: "合并模式", control: segment(importModeOptions(), p.mode, function (val) { p.mode = val; renderPacks(); }) }),
    modeHint(p.mode),
    field({ label: "试运行", control: switchBox(p.dry, "只算差异，不写 emotions.json", function (e) { p.dry = e.target.checked; renderPacks(); }) }),
    h("div", { class: "btnrow" }, [
      imRun,
      imWrite,
      btn("先存一份当前快照", { kind: "ghost", onclick: function () { savePackFrom({ note: "导入前自动备份" }); } })
    ].filter(Boolean))
  ];

  v.appendChild(card({
    kicker: "IMPORT",
    title: "导入感情包",
    desc: "先试运行看差异，确认无误再写入。写入前建议存一份快照当保险。",
    body: imBody
  }));

  if (p.report) v.appendChild(reportCard(p.report, { title: p.report.filename ? "恢复报告 · " + p.report.filename : "导入报告" }));

  /* ===== 快照 ===== */
  var list = h("div", { class: "pack-list" });
  var packs = d.packs || [];
  if (!packs.length) {
    list.appendChild(empty("还没有快照", "点右上角「保存当前」，会把现在的 emotions.json 存成一份带时间戳的快照。"));
  }
  packs.forEach(function (item) {
    var box = h("div", { class: "pack", "data-error": item.error ? "true" : null });
    var main = h("div", { class: "pack-main" }, [
      h("div", { class: "pack-name", text: item.filename, title: item.filename }),
      h("div", { class: "pack-sub", text: (item.modified || "—") + " · " + fmtBytes(item.bytes || 0) + (item.note ? " · " + shorten(item.note, 40) : "") })
    ]);
    box.appendChild(main);
    var chipbox = h("div", { class: "pack-chips" });
    if (item.error) chipbox.appendChild(chip("解析失败", "chip-danger"));
    else {
      chipbox.appendChild(chip((item.characters || 0) + " 角色", "chip-accent"));
      chipbox.appendChild(chip((item.emotions || 0) + " 条", "chip-mono"));
      if (item.exported_at) chipbox.appendChild(chip(shorten(String(item.exported_at), 19), "chip-mono"));
    }
    box.appendChild(chipbox);
    box.appendChild(h("div", { class: "btnrow" }, [
      btn("恢复", { sm: true, kind: "soft", disabled: !!item.error, onclick: function () { openRestoreForm(item); } }),
      btn("下载", { sm: true, onclick: function () { downloadPack(item); } }),
      btn("删除", { sm: true, kind: "danger", onclick: function () { deletePack(item); } })
    ]));
    if (item.error) box.appendChild(note(String(item.error), "danger"));
    list.appendChild(box);
  });

  v.appendChild(card({
    kicker: "SNAPSHOTS",
    title: "服务端快照",
    desc: "存在插件数据目录里，重装插件不会丢；上限 300 份，按修改时间倒序。",
    tools: [
      btn("保存当前", { sm: true, kind: "primary", onclick: function () { savePackFrom({ note: "手动快照" }); } }),
      btn("刷新", { sm: true, kind: "ghost", onclick: function () { reloadTab("packs"); } })
    ],
    body: [
      kv([["目录", d.directory || "—"], ["数量", packs.length + " / 300"]]),
      list
    ]
  }));
}

/* =====================================================================
   4) 语音收藏

   页面拆成「壳 + 列表」两层：renderFavorites() 只建壳并记下几个节点引用，
   之后筛选变化只重画列表和统计条（搜索框节点不动，焦点和光标都不丢），
   展开 / 折叠只换那一条的 DOM（照 toggleSynthRec 的做法回填滚动），
   只有导入 / 上传 / 清空 / 重置筛选这类结构性变化才整页重画。
   ===================================================================== */

/* 文件名里不能出现的字符。反斜杠 / 双引号 / 回车 / 换行 / 制表符 用
   fromCharCode 拼，省掉一层转义，读起来也不容易看错。 */
var FAV_BAD_CHARS = "/:*?<>|" + String.fromCharCode(92) + String.fromCharCode(34) +
  String.fromCharCode(13) + String.fromCharCode(10) + String.fromCharCode(9);

var FAV_TAG_SEPS = " ,，、" + String.fromCharCode(9) + String.fromCharCode(10) + String.fromCharCode(13);

var FAV_UPLOAD_MODES = {
  auto: "自动（先试协议端上传接口，失败退回文件消息段）",
  component: "只用文件消息段",
  onebot_action: "只用协议端上传接口"
};

function favData() { return state.favorites || {}; }
function favRows() { return favData().rows || []; }

function favParams() {
  var f = state.fav;
  var q = {};
  var kw = String(f.q || "").trim();
  if (kw) q.q = kw;
  if (f.ch) q.character = f.ch;
  if (f.emo) q.emotion = f.emo;
  if (f.src) q.source = f.src;
  if (f.pinned) q.pinned = "1";
  return q;
}

function favHasFilter() {
  var f = state.fav;
  return !!(String(f.q || "").trim() || f.ch || f.emo || f.src || f.pinned);
}

function favRowById(id) {
  var rows = favRows();
  var key = String(id);
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i].id) === key) return rows[i];
  }
  return null;
}

function favPickedIds() {
  var box = state.fav.picked;
  var out = [];
  for (var k in box) {
    if (Object.prototype.hasOwnProperty.call(box, k) && box[k]) out.push(k);
  }
  return out;
}

/* 换了一批列表之后，picked / open / audio 里可能还留着已经不在结果里的 id。
   不摘掉的话「已勾选 N 条」会虚高，批量导出还会报「没找到这条收藏」。 */
function favPrune() {
  var f = state.fav;
  var live = {};
  favRows().forEach(function (r) { live[String(r.id)] = true; });
  ["picked", "open", "audio", "loadingAudio", "nodes"].forEach(function (name) {
    var box = f[name] || {};
    for (var k in box) {
      if (!Object.prototype.hasOwnProperty.call(box, k)) continue;
      if (!live[k]) delete box[k];
    }
  });
}

function favSplitTags(v) {
  var raw = String(v === null || v === undefined ? "" : v);
  var out = [];
  var cur = "";
  for (var i = 0; i < raw.length; i++) {
    var ch = raw.charAt(i);
    if (FAV_TAG_SEPS.indexOf(ch) >= 0) {
      if (cur) { out.push(cur); cur = ""; }
      continue;
    }
    cur += ch;
  }
  if (cur) out.push(cur);
  return out;
}

/* 下载用的文件名。safeName() 会强行补 .json，收藏是音频，所以自己过一遍。 */
function favFileName(r) {
  var base = String(r.alias || r.text || ("收藏-" + String(r.id || ""))).trim();
  var out = "";
  for (var i = 0; i < base.length && out.length < 40; i++) {
    var ch = base.charAt(i);
    out += FAV_BAD_CHARS.indexOf(ch) >= 0 ? "_" : ch;
  }
  out = out.replace(new RegExp("^[._ ]+"), "").replace(new RegExp("[. ]+$"), "");
  if (!out) out = "genie-voice";
  return out + (r.suffix || ".wav");
}

function favStamp() {
  var d = new Date();
  function p(x) { return (x < 10 ? "0" : "") + x; }
  return String(d.getFullYear()) + p(d.getMonth() + 1) + p(d.getDate()) + "-" +
         p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
}

function favOpts(list, allLabel) {
  var out = [{ value: "", label: allLabel }];
  (list || []).forEach(function (x) { out.push({ value: String(x), label: String(x) }); });
  return out;
}

function favSourceOpts() {
  var out = [{ value: "", label: "全部来源" }];
  (favData().sources || []).forEach(function (s) {
    out.push({ value: s.value, label: String(s.label || s.value) + " · " + String(s.count || 0) });
  });
  return out;
}

function favModeOpts() {
  var list = favData().modes || [];
  if (!list.length) {
    return [{ value: "merge", label: "合并" }, { value: "overwrite", label: "覆盖" }, { value: "replace", label: "整库替换" }];
  }
  return list.map(function (m) { return { value: m.value, label: m.label || m.value }; });
}

function favUploadModeLabel(v) {
  var key = String(v || "auto");
  return FAV_UPLOAD_MODES[key] || key;
}

/* ---------- 取数 / 落库 ---------- */

/* 筛选变化走这条路：只重画列表和统计条，页面骨架（搜索框、拖放区、上传表单）
   原地不动。seq 防串场 —— 连打关键词时旧请求回得比新请求晚是常事。 */
function favFetch(silent) {
  var f = state.fav;
  var seq = ++f.seq;
  f.loading = true;
  favSyncMeta();
  return apiGet("favorites", favParams()).then(function (d) {
    if (seq !== f.seq) return null;
    f.loading = false;
    state.favorites = d || {};
    state.loaded.favorites = true;
    favPrune();
    favSyncStats();
    favRenderList();
    favSyncMeta();
    if (!silent) toast("收藏已刷新", "ok", 1600);
    return d;
  }, function (e) {
    if (seq !== f.seq) return null;
    f.loading = false;
    favSyncMeta();
    return fail(e, "读取收藏失败");
  });
}

function favDebounce() {
  var f = state.fav;
  if (f.debounce) { clearTimeout(f.debounce); f.debounce = null; }
  f.debounce = softTimer(function () {
    f.debounce = null;
    if (state.tab !== "favorites") return;
    favFetch(true);
  }, 320);
}

/* 写操作的返回值本身就带一份最新列表，直接换掉即可，不用再多请求一次。
   轻量版只刷列表 / 统计 / 计数条。 */
function favApply(d, msg, tone) {
  state.favorites = d || {};
  state.loaded.favorites = true;
  favPrune();
  if (state.tab === "favorites") {
    favSyncStats();
    favRenderList();
    favSyncMeta();
  }
  refreshOverview();
  if (msg) toast(msg, tone || "ok");
}

/* 结构性变化（导入 / 上传 / 清空 / 重置筛选）才整页重画，走 keepScroll 保住视口。 */
function favApplyFull(d, msg, tone) {
  state.favorites = d || {};
  state.loaded.favorites = true;
  favPrune();
  if (state.tab === "favorites") keepScroll(viewNode("favorites"), renderFavorites);
  refreshOverview();
  if (msg) toast(msg, tone || "ok");
}

/* 「重置筛选」：条件清光之后上面那几个输入框也得回到初始态，
   所以拉完新数据整页重画一次；顺手把还没落地的防抖请求掐掉，省一次空跑。 */
function favResetFilter() {
  var f = state.fav;
  f.q = ""; f.ch = ""; f.emo = ""; f.src = ""; f.pinned = false;
  if (f.debounce) { clearTimeout(f.debounce); f.debounce = null; }
  favFetch(true).then(function () {
    if (state.tab === "favorites") keepScroll(viewNode("favorites"), renderFavorites);
  });
}

/* ---------- 试听 ---------- */

function favLoadAudio(key) {
  var f = state.fav;
  key = String(key);
  if (f.loadingAudio[key]) return;
  f.loadingAudio[key] = true;
  apiGet("favorites/audio", { id: key }).then(function (d) {
    f.audio[key] = d || {};
  }, function (e) {
    f.audio[key] = { error: e && e.message ? e.message : String(e) };
  }).then(function () {
    delete f.loadingAudio[key];
    if (f.open[key] && state.tab === "favorites") favSwap(key);
  });
}

function favToggle(id) {
  var f = state.fav;
  var key = String(id);
  f.open[key] = !f.open[key];
  if (f.open[key] && !f.audio[key]) favLoadAudio(key);
  favSwap(key);
}

/* 展开 / 折叠只换这一条的节点，再按同一行在视口里的位置回填滚动。
   整页重画的话 clear() 一清文档就变短，浏览器会把 scrollTop 夹掉 ——
   用户看到的就是「点一下展开，页面自己往上跳」。 */
function favSwap(key) {
  var f = state.fav;
  key = String(key);
  var r = favRowById(key);
  var old = f.nodes[key];
  if (!r || !old || !old.parentNode || !attachedSafe(old)) { favRenderList(); return; }

  var before = 0;
  var measured = false;
  try {
    if (typeof old.getBoundingClientRect === "function") { before = old.getBoundingClientRect().top; measured = true; }
  } catch (e1) {}

  var next = favEntryNode(r);
  try {
    old.parentNode.insertBefore(next, old);
    old.parentNode.removeChild(old);
  } catch (e2) { favRenderList(); return; }

  if (measured) {
    try {
      var delta = next.getBoundingClientRect().top - before;
      if (delta) scrollToSafe(scrollYSafe() + delta);
    } catch (e3) {}
  }
  try {
    var nh = next.querySelector(".fav-head");
    if (nh && typeof nh.focus === "function") {
      try { nh.focus({ preventScroll: true }); } catch (e4) { nh.focus(); }
    }
  } catch (e5) {}
}

/* ---------- 一条收藏 ---------- */

function favEntryNode(r) {
  var f = state.fav;
  var key = String(r.id);
  var open = !!f.open[key];

  var pick = h("input", {
    type: "checkbox", checked: !!f.picked[key],
    "aria-label": "勾选这条收藏",
    onchange: function (e) {
      if (e.target.checked) f.picked[key] = true;
      else delete f.picked[key];
      favSyncMeta();
    }
  });

  var head = h("button", {
    type: "button", class: "fav-head", "aria-expanded": open ? "true" : "false",
    onclick: function () { favToggle(key); }
  }, [
    h("span", { class: "fav-idx mono", text: "#" + String(r.index || "?") }),
    h("span", { class: "fav-name", text: r.alias || shorten(r.text, 44) || ("收藏 " + key) }),
    h("span", { class: "fav-dur mono", text: r.duration_human || "—" }),
    h("span", { class: "fav-caret", "aria-hidden": "true", text: open ? "▾" : "▸" })
  ]);

  var sub = h("div", { class: "fav-sub", text: [
    (r.character || "未标角色") + " · " + (r.emotion || "未标感情"),
    r.bytes_human || "—",
    r.created_text || "—"
  ].join(" · ") });

  var box = h("div", {
    class: "fav", "data-fav": key,
    "data-pinned": r.pinned ? "true" : null,
    "data-open": open ? "true" : null,
    "data-lossy": r.lossy ? "true" : null
  }, [
    h("label", { class: "fav-pick", title: "勾选后可以批量导出" }, pick),
    h("div", { class: "fav-main" }, [head, sub])
  ]);
  f.nodes[key] = box;

  var chips = h("div", { class: "fav-chips" });
  if (r.pinned) chips.appendChild(chip("置顶", "chip-accent"));
  chips.appendChild(chip(r.source_label || r.source || "—", r.lossy ? "chip-warn" : "chip-mono"));
  chips.appendChild(chip(String(r.suffix || ".wav").replace(".", "").toUpperCase(), "chip-mono"));
  if (r.play_count) chips.appendChild(chip("发过 " + r.play_count + " 次", "chip-mono"));
  (r.tags || []).forEach(function (t) { chips.appendChild(chip(String(t))); });
  box.appendChild(chips);

  box.appendChild(h("div", { class: "btnrow" }, [
    btn(open ? "收起" : "试听", { sm: true, kind: "soft", onclick: function () { favToggle(key); } }),
    btn("下载", { sm: true, onclick: function () { favDownload(r); } }),
    btn("编辑", { sm: true, kind: "ghost", onclick: function () { openFavEdit(r); } }),
    btn(r.pinned ? "取消置顶" : "置顶", { sm: true, kind: "ghost", onclick: function () { favPin(r); } }),
    btn("删除", { sm: true, kind: "danger", onclick: function () { favDelete(r); } })
  ]));

  if (!open) {
    if (r.text) box.appendChild(h("p", { class: "fav-text", title: r.text, text: shorten(r.text, 150) }));
    return box;
  }

  var body = h("div", { class: "fav-body" });
  var a = f.audio[key];
  if (!a) {
    if (!f.loadingAudio[key]) favLoadAudio(key);   /* 兜底：状态被裁掉过就重新拉一次 */
    body.appendChild(h("div", { class: "row-tight" }, [h("span", { class: "spinner" }), dim("正在读取音频…")]));
  } else if (a.error) {
    body.appendChild(note(String(a.error), "danger"));
  } else if (a.audio_base64) {
    var au = h("audio", { controls: true, preload: "metadata" });
    au.src = "data:" + (a.mime || r.mime || "audio/wav") + ";base64," + a.audio_base64;
    body.appendChild(h("div", { class: "player" }, [
      h("div", { class: "player-title" }, [
        h("span", { text: "🔊" }),
        h("span", { text: shorten(r.alias || r.text || key, 60) })
      ]),
      au
    ]));
  } else {
    body.appendChild(note(
      "音频体积超过网页内联上限（" + fmtBytes(favData().max_inline_bytes || 0) +
      "），浏览器里没法直接播。点「下载」拿原文件，或者在聊天里发 /发收藏 " + String(r.index || key) + "。",
      "warn"));
  }

  body.appendChild(kv([
    ["台词", r.text || "—"],
    ["角色 · 感情", (r.character || "—") + " · " + (r.emotion || "—")],
    ["时长 · 体积", (r.duration_human || "—") + " · " + (r.bytes_human || "—")],
    ["来源", (r.source_label || r.source || "—") +
      (r.lossy ? "（协议端回捞，已统一转成 WAV，音质取决于原始编码）" : "（原始文件逐字节复制，无损）")],
    ["所属会话", r.session_id || "—"],
    ["收藏时间", r.created_text || "—"],
    ["最近发送", r.last_played_text ? (r.last_played_text + " · 共 " + (r.play_count || 0) + " 次") : "还没发过"],
    ["指纹 sha256", r.sha256 || "—", "mono"],
    ["内部 id", r.id, "mono"]
  ]));

  body.appendChild(h("div", { class: "btnrow" }, [
    btn("导出这一条", { sm: true, kind: "soft", onclick: function () { favExport([key], r.alias || r.text || key); } }),
    btn("复制发送指令", { sm: true, kind: "ghost", onclick: function () { copyText("/发收藏 " + String(r.index || key), "指令已复制"); } }),
    btn("复制台词", { sm: true, kind: "ghost", disabled: !r.text, onclick: function () { copyText(r.text, "台词已复制"); } })
  ]));
  box.appendChild(body);
  return box;
}

function favEmptyNode() {
  if (favHasFilter()) {
    return empty("没有匹配的收藏", "换个关键词，或者点上面的「重置筛选」看全部。");
  }
  return empty("收藏夹还是空的",
    "在聊天里引用一条 bot 发过的语音，回一句 /语音收藏 就能原样存进来；也可以在下面直接上传音频文件。");
}

function favFillList(box) {
  var f = state.fav;
  f.nodes = {};
  clear(box);
  var rows = favRows();
  if (!rows.length) { box.appendChild(favEmptyNode()); return; }
  rows.forEach(function (r) { box.appendChild(favEntryNode(r)); });
}

function favRenderList() {
  var f = state.fav;
  if (f.listBox && attachedSafe(f.listBox)) {
    keepScroll(f.listBox, function () { favFillList(f.listBox); });
    return;
  }
  /* 节点引用对不上（比如刚从别的分区切回来）就整页重画一次，
     rebuilding 挡住递归：renderFavorites 只调 favFillList，不会再回到这里。 */
  if (state.tab !== "favorites" || f.rebuilding) return;
  f.rebuilding = true;
  try { keepScroll(viewNode("favorites"), renderFavorites); }
  finally { f.rebuilding = false; }
}

function favSyncStats() {
  var f = state.fav;
  if (!f.statBox) return;
  var d = favData();
  var s = d.stats || {};
  var cfg = d.config || {};
  var count = Number(s.count) || 0;
  var limit = Number(s.limit) || Number(cfg.limit) || 0;
  var ratio = limit ? count / limit : 0;
  clear(f.statBox);
  append(f.statBox, [
    stat(String(count) + (limit ? " / " + limit : ""), "已收藏", ratio >= 0.9 ? "warn" : (count ? "accent" : null)),
    stat(String(Number(s.pinned) || 0), "置顶", Number(s.pinned) ? "ok" : null),
    stat(s.total_bytes_human || fmtBytes(s.total_bytes || 0), "占用空间"),
    stat(s.total_duration_human || "0s", "总时长"),
    stat(String(Object.keys(s.characters || {}).length), "涉及角色"),
    stat(String(Number(s.lossy) || 0), "协议端回捞", Number(s.lossy) ? "warn" : null)
  ]);
}

function favSyncMeta() {
  var f = state.fav;
  var d = favData();
  var picked = favPickedIds().length;
  if (f.metaNode) {
    var bits = ["命中 " + (Number(d.matched) || favRows().length) + " 条"];
    if (d.truncated) bits.push("只显示前 " + (Number(d.max_rows) || 400) + " 条");
    if (picked) bits.push("已勾选 " + picked + " 条");
    if (f.loading) bits.push("正在刷新…");
    if (d.generated_at) bits.push("数据时间 " + d.generated_at);
    f.metaNode.textContent = bits.join(" · ");
  }
  if (f.exportPicked) {
    f.exportPicked.disabled = !picked;
    f.exportPicked.textContent = picked ? ("导出勾选 " + picked + " 条") : "导出勾选";
  }
  /* 筛选条件是在列表局部刷新时改的，工具条不会跟着重画，
     所以「重置筛选」的可用态得在这里补上一笔。 */
  if (f.resetBtn) f.resetBtn.disabled = !favHasFilter();
}

/* ---------- 动作 ---------- */

var FAV_MODE_HINTS = {
  merge: "合并：包里有、库里没有的加进来；同一条（按内部 id）已经存在就跳过，现有收藏一条都不动。",
  overwrite: "覆盖：同一条已经存在时用包里的版本替换掉，其余按合并处理。",
  replace: "整库替换：先清空现在的收藏（音频文件真删），再把包里的内容整份写进去。不可撤销。"
};

function favDownload(r) {
  var name = favFileName(r);
  try {
    var task = SDK.download("favorites/download", { id: r.id }, name);
    if (task && typeof task.then === "function") {
      task.then(function () { toast("已下载 " + name, "ok"); }, function (e) { fail(e, "下载失败"); });
    } else {
      toast("已下载 " + name, "ok");
    }
  } catch (e) { fail(e, "下载失败"); }
}

/* ids 传 null / 空数组就是导出全部。label 只用来把提示语说得具体点。 */
function favExport(ids, label) {
  var many = !!(ids && ids.length);
  var params = many ? { ids: ids.join(",") } : {};
  var name = "genie-voices-" + favStamp() + ".zip";
  var tip = label ? ("已导出「" + shorten(label, 24) + "」")
                  : (many ? ("已导出 " + ids.length + " 条") : "已导出全部收藏");
  try {
    var task = SDK.download("favorites/export", params, name);
    if (task && typeof task.then === "function") {
      task.then(function () { toast(tip + " · " + name, "ok"); }, function (e) { fail(e, "导出失败"); });
    } else {
      toast(tip + " · " + name, "ok");
    }
  } catch (e) { fail(e, "导出失败"); }
}

function favPin(r) {
  apiPost("favorites/update", { id: r.id, pinned: !r.pinned })
    .then(function (d) { favApply(d, r.pinned ? "已取消置顶" : "已置顶，清空收藏时默认保留"); })
    .catch(function (e) { fail(e, "操作失败"); });
}

function favLabel(r) {
  return r.alias || shorten(r.text, 30) || ("#" + String(r.index || r.id));
}

function favDelete(r) {
  var name = favLabel(r);
  confirmModal("删除收藏「" + name + "」？",
    "音频文件会从收藏目录里删掉，无法找回。参考音频和 emotions.json 不受影响。",
    { danger: true, okText: "删除" })
    .then(function (ok) {
      if (!ok) return;
      apiPost("favorites/delete", { id: r.id }).then(function (d) {
        delete state.fav.picked[String(r.id)];
        delete state.fav.open[String(r.id)];
        favApply(d, "已删除「" + name + "」");
      }).catch(function (e) { fail(e, "删除失败"); });
    });
}

function favClear() {
  var keep = true;
  openModal({
    kicker: "DANGER",
    title: "清空收藏夹？",
    danger: true,
    okText: "清空",
    body: [
      note("收藏目录里的音频文件会被真删掉，无法找回。建议先点「导出全部」留一份收藏包。", "danger"),
      switchBox(true, "保留置顶的收藏", function (e) { keep = e.target.checked; })
    ]
  }).then(function (ok) {
    if (!ok) return;
    apiPost("favorites/clear", { confirm: true, keep_pinned: keep }).then(function (d) {
      state.fav.picked = {};
      state.fav.open = {};
      favApplyFull(d, "已清空 " + (Number(d.removed) || 0) + " 条" + (keep ? "（置顶的留着）" : "（含置顶）"));
    }).catch(function (e) { fail(e, "清空失败"); });
  });
}

/* ---------- 编辑 ---------- */

/* 改名走 favorites/rename（后端会做重名去重），其余字段走 favorites/update。
   两个接口都会回一份最新列表，所以串起来跑、只用最后一份即可。 */
function openFavEdit(r) {
  var d = favData();
  var ctl = {};
  ctl.alias = input(r.alias || "", null, { placeholder: "给这条起个好记的名字" });
  ctl.character = input(r.character || "", null, { placeholder: "角色名，例如 kisaki" });
  ctl.emotion = input(r.emotion || "", null, { placeholder: "感情名，例如 悲伤" });
  ctl.text = textarea(r.text || "", null, { rows: 3, placeholder: "这条语音念的台词，写上之后关键词能搜到" });
  ctl.tags = input((r.tags || []).join(" "), null, { mono: true, placeholder: "标签，空格 / 逗号 / 顿号分隔" });
  var chars = d.characters || [];
  var emos = d.emotions || [];
  formModal({
    kicker: "EDIT",
    title: "编辑收藏 · " + favLabel(r),
    okText: "保存",
    body: [
      field({ label: "名字", control: ctl.alias, desc: "重名会自动加后缀；/发收藏 可以直接用这个名字，不用记序号" }),
      h("div", { class: "grid-2" }, [
        field({ label: "角色", control: ctl.character, desc: chars.length ? ("已有：" + shorten(chars.join(" / "), 60)) : "" }),
        field({ label: "感情", control: ctl.emotion, desc: emos.length ? ("已有：" + shorten(emos.join(" / "), 60)) : "" })
      ]),
      field({ label: "台词", control: ctl.text }),
      field({ label: "标签", control: ctl.tags, desc: "最多留 8 个，用来分组；筛选框里搜标签也能命中" })
    ],
    onOk: function () {
      var alias = String(ctl.alias.value || "").trim();
      if (!alias) { toast("名字不能为空", "warn"); return false; }
      return {
        alias: alias,
        character: String(ctl.character.value || "").trim(),
        emotion: String(ctl.emotion.value || "").trim(),
        text: String(ctl.text.value || "").trim(),
        tags: favSplitTags(ctl.tags.value)
      };
    }
  }).then(function (form) {
    if (form) favSaveEdit(r, form);
  });
}

function favSaveEdit(r, form) {
  var jobs = [];
  if (form.alias !== String(r.alias || "")) {
    jobs.push({ ep: "favorites/rename", body: { id: r.id, alias: form.alias } });
  }
  var fields = { id: r.id };
  var dirty = false;
  if (form.character !== String(r.character || "")) { fields.character = form.character; dirty = true; }
  if (form.emotion !== String(r.emotion || "")) { fields.emotion = form.emotion; dirty = true; }
  if (form.text !== String(r.text || "")) { fields.text = form.text; dirty = true; }
  if (form.tags.join(" ") !== (r.tags || []).join(" ")) { fields.tags = form.tags; dirty = true; }
  if (dirty) jobs.push({ ep: "favorites/update", body: fields });
  if (!jobs.length) { toast("没有改动", "info", 1800); return; }
  var chain = Promise.resolve(null);
  jobs.forEach(function (job) {
    chain = chain.then(function () { return apiPost(job.ep, job.body); });
  });
  chain.then(function (d) { favApply(d, "已保存修改"); }).catch(function (e) { fail(e, "保存失败"); });
}

/* 工作台试听区的「存入收藏」。后端只认 temp_audio_dir 里的路径，
   临时音频只留 30 分钟，过期了会明确回一句「重新合成一次」。 */
function favSaveSynth(r) {
  if (!r || !r.path) { toast("先合成一条再收藏", "warn"); return; }
  apiPost("favorites/save-synth", {
    path: r.path, alias: "",
    character: r.character, emotion: r.emotion, text: r.text
  }).then(function (d) {
    favApply(d,
      d.duplicate ? "这条已经在收藏夹里了（指纹一致，没有重复存）" : "已存入收藏 · 聊天里 /收藏列表 就能看到",
      d.duplicate ? "warn" : "ok");
  }).catch(function (e) { fail(e, "收藏失败"); });
}

/* ---------- 导入 / 上传 ---------- */

function favImportRun() {
  var f = state.fav;
  if (!f.bundleData) { toast("先拖入一个收藏包 .zip", "warn"); return; }
  var go2 = function () {
    f.busy = true;
    renderFavorites();
    apiPost("favorites/import", { data: f.bundleData, mode: f.mode })
      .then(function (d) {
        var rep = d.report || {};
        f.busy = false;
        f.report = rep;
        f.bundleData = "";
        f.bundleName = "";
        f.picked = {};
        f.open = {};
        favApplyFull(d, "导入完成 · " + (rep.summary_text || ""),
          (Number(rep.invalid) || 0) ? "warn" : "ok");
      })
      .catch(function (e) { f.busy = false; renderFavorites(); fail(e, "导入收藏包失败"); });
  };
  if (f.mode !== "replace") { go2(); return; }
  confirmModal("确认用整库替换模式导入？", FAV_MODE_HINTS.replace + " 建议先点「导出全部」留一份。",
    { danger: true, okText: "我确认，替换" })
    .then(function (ok) { if (ok) go2(); });
}

function favUploadRun() {
  var f = state.fav;
  if (!f.upData) { toast("先选一个音频文件", "warn"); return; }
  f.uploading = true;
  renderFavorites();
  apiPost("favorites/upload", {
    data: f.upData,
    filename: f.upName,
    alias: f.upAlias,
    character: f.upChar,
    emotion: f.upEmo,
    text: f.upText
  }).then(function (d) {
    f.uploading = false;
    f.upData = "";
    f.upName = "";
    f.upAlias = "";
    f.upChar = "";
    f.upEmo = "";
    f.upText = "";
    favApplyFull(d,
      d.duplicate ? "这条音频已经在收藏夹里了（指纹一致，没有重复存）" : "已上传并收藏",
      d.duplicate ? "warn" : "ok");
  }).catch(function (e) { f.uploading = false; renderFavorites(); fail(e, "上传失败"); });
}

/* 收藏包的导入报告：这里的 added / updated / … 都是数字，和感情包 reportCard
   里那套数组结构不一样，所以单独写一张卡，别去复用。 */
function favReportCard(rep) {
  function num(k) { return Number(rep[k]) || 0; }
  return card({
    sub: true,
    kicker: "REPORT",
    title: "收藏包导入报告",
    tools: rep.summary_text ? [btn("复制摘要", { sm: true, kind: "ghost", onclick: function () { copyText(rep.summary_text, "摘要已复制"); } })] : null,
    body: [
      h("div", { class: "stat-grid" }, [
        stat(num("added"), "新增", num("added") ? "ok" : null),
        stat(num("updated"), "覆盖", num("updated") ? "accent" : null),
        stat(num("skipped"), "跳过", num("skipped") ? "warn" : null),
        stat(num("removed"), "清空", num("removed") ? "danger" : null),
        stat(num("evicted"), "超量淘汰", num("evicted") ? "warn" : null),
        stat(num("invalid"), "无效", num("invalid") ? "danger" : null)
      ]),
      note(rep.summary_text || "没有任何变化", num("invalid") ? "warn" : "ok"),
      kv([
        ["模式", rep.mode_label || rep.mode || "—"],
        ["库内总数", num("total") + " 条"]
      ])
    ]
  });
}

/* ---------- 二进制拖放区 ---------- */

/* dropZone() 走 readAsText，只能吃 JSON。收藏包是 zip、上传的是音频，都得走
   readAsDataURL 再把 base64 段切出来 —— 后端 _decode_upload 认的就是纯 base64。 */
function dropZoneBinary(opts) {
  opts = opts || {};
  var limit = Number(opts.limit) || 4 * 1024 * 1024;
  var picker = h("input", { type: "file", accept: opts.accept || null });
  var zone = h("div", { class: "drop", tabindex: "0", role: "button" }, [
    h("b", { text: opts.title || "把文件拖进来" }),
    h("span", { text: opts.desc || ("或点这里选文件 · 上限 " + fmtBytes(limit)) }),
    picker
  ]);
  function read(file) {
    if (!file) return;
    if (file.size > limit) {
      toast("文件 " + fmtBytes(file.size) + " 超过上限 " + fmtBytes(limit) + "，已拒绝", "danger");
      return;
    }
    var fr = new FileReader();
    fr.onload = function () {
      var s = String(fr.result || "");
      var i = s.indexOf("base64,");
      opts.onFile(i >= 0 ? s.slice(i + 7) : "", file.name, file.size);
    };
    fr.onerror = function () { toast("读取文件失败", "danger"); };
    try { fr.readAsDataURL(file); } catch (e) { fail(e, "读取文件失败"); }
  }
  zone.addEventListener("click", function (e) { if (e.target !== picker) picker.click(); });
  zone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); }
  });
  picker.addEventListener("change", function () {
    read(picker.files && picker.files[0]);
    picker.value = "";
  });
  ["dragenter", "dragover"].forEach(function (n) {
    zone.addEventListener(n, function (e) { e.preventDefault(); e.stopPropagation(); zone.dataset.over = "true"; });
  });
  ["dragleave", "dragend"].forEach(function (n) {
    zone.addEventListener(n, function (e) { e.preventDefault(); zone.dataset.over = "false"; });
  });
  zone.addEventListener("drop", function (e) {
    e.preventDefault();
    e.stopPropagation();
    zone.dataset.over = "false";
    var dt = e.dataTransfer;
    if (dt && dt.files && dt.files.length) read(dt.files[0]);
  });
  return zone;
}

/* ---------- 页面 ---------- */

/* base64 长度换回原始字节数：4 个字符 = 3 字节，末尾的 = 是补位。
   只用来给「已读入 xx」这行提示，不做校验。 */
function favB64Bytes(s) {
  var n = String(s || "").length;
  if (!n) return 0;
  var pad = 0;
  if (s.charAt(n - 1) === "=") pad += 1;
  if (n > 1 && s.charAt(n - 2) === "=") pad += 1;
  return Math.max(0, Math.floor(n / 4) * 3 - pad);
}

function renderFavorites() {
  var v = clear(viewNode("favorites"));
  var f = state.fav;
  var d = favData();
  var cfg = d.config || {};
  var upLimit = Number(d.max_upload_bytes) || 10 * 1024 * 1024;

  /* 整页重画会把上一轮的节点全丢掉，这四个引用必须跟着换新的，
     否则 favSyncStats / favSyncMeta / favRenderList 会往已经摘掉的旧节点上写。 */
  f.statBox = h("div", { class: "stat-grid" });
  f.metaNode = h("p", { class: "fav-meta" });
  f.listBox = h("div", { class: "fav-list" });
  f.exportPicked = btn("导出勾选", { sm: true, kind: "soft", onclick: function () { favExport(favPickedIds(), ""); } });
  f.resetBtn = btn("重置筛选", { sm: true, kind: "ghost", disabled: !favHasFilter(), onclick: favResetFilter });

  /* ===== 总开关关掉时的提示 ===== */
  /* 网页这一侧照样能用：后端 _vault_ready 不看 enabled，只有聊天里的指令会被拦。 */
  if (d.enabled === false) {
    v.appendChild(card({
      kicker: "OFF",
      title: "聊天里的收藏指令已关闭",
      body: [
        note("配置项「启用语音收藏」是关的，/语音收藏、/发收藏、/语音转文件 在聊天里不会响应。这个页面不受影响，照样能看、能改、能导出。", "warn"),
        h("div", { class: "btnrow" }, [
          btn("去配置页打开", { kind: "soft", onclick: function () { go("config"); } })
        ])
      ]
    }));
  }

  /* ===== 概览 ===== */
  v.appendChild(card({
    kicker: "VAULT",
    title: "语音收藏夹",
    desc: "在聊天里引用一条 bot 发过的语音，回一句 /语音收藏 就原样存进来 —— 逐字节复制，不重新编码，音质无损。",
    tools: [
      btn("刷新", { sm: true, kind: "ghost", onclick: function () { favFetch(false); } }),
      btn("导出全部", { sm: true, kind: "soft", onclick: function () { favExport(null, ""); } }),
      btn("清空", { sm: true, kind: "danger", onclick: function () { favClear(); } })
    ],
    body: [
      f.statBox,
      kv([
        ["收藏目录", d.directory || "—", "mono"],
        ["容量上限", (Number(cfg.limit) || 0) + " 条 / " + (Number(cfg.max_mb) || 0) + " MB（超了按时间从旧到新淘汰，置顶的不动）"],
        ["发文件方式", favUploadModeLabel(cfg.upload_mode)],
        ["支持格式", (d.suffixes || []).join(" "), "mono"]
      ])
    ]
  }));

  /* ===== 筛选 ===== */
  /* 这一块的节点在筛选变化时不重建，所以搜索框的焦点和光标位置都不会丢。 */
  v.appendChild(card({
    sub: true,
    kicker: "FILTER",
    title: "筛选",
    body: [
      h("div", { class: "grid-3" }, [
        field({ label: "关键词", control: input(f.q, null, {
          placeholder: "名字 / 台词 / 角色 / 感情 / 标签",
          oninput: function (e) { f.q = e.target.value; favDebounce(); }
        }) }),
        field({ label: "角色", control: select(favOpts(d.characters, "全部角色"), f.ch, function (e) {
          f.ch = e.target.value; favFetch(true);
        }) }),
        field({ label: "感情", control: select(favOpts(d.emotions, "全部感情"), f.emo, function (e) {
          f.emo = e.target.value; favFetch(true);
        }) })
      ]),
      h("div", { class: "grid-2" }, [
        field({
          label: "来源",
          desc: "「协议端回捞」是从聊天消息里重新拉回来的，可能经过一次转码；其余三种都是原始文件。",
          control: select(favSourceOpts(), f.src, function (e) { f.src = e.target.value; favFetch(true); })
        }),
        field({
          label: "置顶",
          control: switchBox(f.pinned, "只看置顶的收藏", function (e) { f.pinned = e.target.checked; favFetch(true); })
        })
      ])
    ]
  }));

  /* ===== 列表 ===== */
  v.appendChild(card({
    kicker: "VOICES",
    title: "收藏列表",
    desc: "点标题展开就能直接试听。序号和聊天里 /收藏列表 显示的一致，可以拿去 /发收藏。",
    tools: [
      f.exportPicked,
      btn("全选本页", { sm: true, kind: "ghost", onclick: function () {
        favRows().forEach(function (r) { f.picked[String(r.id)] = true; });
        favRenderList();
        favSyncMeta();
      } }),
      btn("清空勾选", { sm: true, kind: "ghost", onclick: function () {
        f.picked = {};
        favRenderList();
        favSyncMeta();
      } }),
      f.resetBtn
    ],
    body: [f.metaNode, f.listBox]
  }));

  if (f.report) v.appendChild(favReportCard(f.report));

  /* ===== 导入 / 上传 ===== */
  var bundleZone = dropZoneBinary({
    accept: ".zip",
    limit: upLimit,
    title: "把收藏包 .zip 拖进来",
    desc: "或点这里选文件 · 上限 " + fmtBytes(upLimit),
    onFile: function (b64, name, size) {
      f.bundleData = b64;
      f.bundleName = name || "";
      f.report = null;
      renderFavorites();
      toast("已读入 " + (name || "收藏包") + "（" + fmtBytes(size) + "）", "ok");
    }
  });

  var audioZone = dropZoneBinary({
    accept: (d.suffixes || []).join(","),
    limit: upLimit,
    title: "把音频文件拖进来",
    desc: "支持 " + ((d.suffixes || []).join(" ") || "常见音频格式") + " · 上限 " + fmtBytes(upLimit),
    onFile: function (b64, name, size) {
      f.upData = b64;
      f.upName = name || "";
      if (!f.upAlias) {
        var raw = String(name || "");
        var dot = raw.lastIndexOf(".");
        f.upAlias = dot > 0 ? raw.slice(0, dot) : raw;
      }
      renderFavorites();
      toast("已读入 " + (name || "音频") + "（" + fmtBytes(size) + "）", "ok");
    }
  });

  var importBtn = btn(f.busy ? "处理中…" : "导入收藏包", {
    kind: "primary",
    disabled: !!f.busy || !f.bundleData,
    onclick: function () { favImportRun(); }
  });
  var uploadBtn = btn(f.uploading ? "上传中…" : "上传并收藏", {
    kind: "primary",
    disabled: !!f.uploading || !f.upData,
    onclick: function () { favUploadRun(); }
  });

  /* 上传表单这四个输入框的 oninput 只写状态、不重渲染 —— 一重渲染就把
     正在打字的输入框连焦点带光标一起换掉了。按钮的 disabled 只跟文件有关，
     这四个字段全是选填，不用跟着刷。 */
  var upFields = [
    ["名字", f.upAlias, "留空就拿文件名当名字", function (e) { f.upAlias = e.target.value; }],
    ["角色", f.upChar, "选填，填了能按角色筛", function (e) { f.upChar = e.target.value; }],
    ["感情", f.upEmo, "选填，填了能按感情筛", function (e) { f.upEmo = e.target.value; }],
    ["台词", f.upText, "选填，填了关键词能搜到", function (e) { f.upText = e.target.value; }]
  ].map(function (row) {
    return field({
      label: row[0],
      desc: row[2],
      control: input(row[1], null, { placeholder: row[2], oninput: row[3] })
    });
  });

  v.appendChild(card({
    kicker: "IO",
    title: "导入 / 上传",
    desc: "收藏包就是一个 zip：里面是原始音频文件加一份索引，换机器、换服务器直接搬走。",
    body: [
      h("div", { class: "io-panel" }, [
        h("div", {}, [
          field({
            label: "收藏包（.zip）",
            tag: f.bundleName ? chip(shorten(f.bundleName, 28), "chip-mono chip-accent") : null,
            control: bundleZone,
            hint: f.bundleData ? ("已读入 " + fmtBytes(favB64Bytes(f.bundleData))) : "还没选文件"
          }),
          field({
            label: "合并模式",
            control: segment(favModeOpts(), f.mode, function (val) { f.mode = val; renderFavorites(); })
          }),
          note(FAV_MODE_HINTS[f.mode] || "", f.mode === "replace" ? "danger" : "info"),
          h("div", { class: "btnrow" }, [
            importBtn,
            f.bundleData ? btn("丢掉这个包", { kind: "ghost", onclick: function () {
              f.bundleData = ""; f.bundleName = ""; renderFavorites();
            } }) : null
          ].filter(Boolean))
        ]),
        h("div", {}, [
          field({
            label: "上传单个音频",
            tag: f.upName ? chip(shorten(f.upName, 28), "chip-mono chip-accent") : null,
            control: audioZone,
            hint: f.upData ? ("已读入 " + fmtBytes(favB64Bytes(f.upData))) : "还没选文件"
          }),
          h("div", { class: "grid-2" }, upFields),
          h("div", { class: "btnrow" }, [
            uploadBtn,
            f.upData ? btn("丢掉这个文件", { kind: "ghost", onclick: function () {
              f.upData = ""; f.upName = ""; renderFavorites();
            } }) : null
          ].filter(Boolean))
        ])
      ])
    ]
  }));

  /* 壳搭完了再一次性把列表和统计填进去。这里直接调 favFillList，
     不走 favRenderList —— 后者在引用对不上时会回头再调 renderFavorites。 */
  favFillList(f.listBox);
  favSyncStats();
  favSyncMeta();
}

/* ============================================================ 配置 */

function nl() { return String.fromCharCode(10); }

function cfgKey(parent, key) { return parent ? (parent + SEP + String(key)) : String(key); }

function cfgIsDirty(parent, key) {
  return Object.prototype.hasOwnProperty.call(state.cfg.dirty, cfgKey(parent, key));
}

function cfgGet(parent, f) {
  var k = cfgKey(parent, f.key);
  if (Object.prototype.hasOwnProperty.call(state.cfg.dirty, k)) return state.cfg.dirty[k];
  return f.value;
}

function cfgApply(parent, f, value) {
  var k = cfgKey(parent, f.key);
  var same;
  try { same = JSON.stringify(value) === JSON.stringify(f.value); }
  catch (e) { same = value === f.value; }
  if (same) delete state.cfg.dirty[k];
  else state.cfg.dirty[k] = value;
}

function cfgDirtyCount() {
  var n = 0;
  for (var k in state.cfg.dirty) if (Object.prototype.hasOwnProperty.call(state.cfg.dirty, k)) n++;
  return n;
}

/* dirty 表拍平成后端要的 values：顶层键直接给值，object 子键聚合成只含改动的 dict */
function cfgPayload() {
  var top = {};
  for (var k in state.cfg.dirty) {
    if (!Object.prototype.hasOwnProperty.call(state.cfg.dirty, k)) continue;
    var v = state.cfg.dirty[k];
    var i = k.indexOf(SEP);
    if (i < 0) { top[k] = v; continue; }
    var parent = k.slice(0, i);
    var child = k.slice(i + 1);
    if (!top[parent] || typeof top[parent] !== "object") top[parent] = {};
    top[parent][child] = v;
  }
  return top;
}

function cfgUpdateBar() {
  var bar = state.cfg.barNode;
  if (!bar || !bar.parentNode) return;
  var n = cfgDirtyCount();
  bar.hidden = n === 0;
  var txt = bar.textNode;
  if (txt) {
    clear(txt);
    txt.appendChild(D.createTextNode("已修改 "));
    txt.appendChild(h("b", { text: String(n) }));
    txt.appendChild(D.createTextNode(" 项，尚未保存"));
  }
}

function cfgRestartTag(key) {
  var list = (state.config && state.config.restart_required_keys) || [];
  for (var i = 0; i < list.length; i++) if (String(list[i]) === String(key)) return true;
  return false;
}

function cfgControl(parent, f, holder) {
  var type = String(f.type || "string");
  function mark() {
    holder.dataset.dirty = cfgIsDirty(parent, f.key) ? "true" : "false";
    cfgUpdateBar();
  }
  function apply(v) { cfgApply(parent, f, v); mark(); }
  var cur = cfgGet(parent, f);
  var text0 = (cur === null || cur === undefined) ? "" : String(cur);

  if (type === "bool") {
    var box = switchBox(!!cur, cur ? "已开启" : "已关闭", function (ev) {
      var on = !!ev.target.checked;
      apply(on);
      var lab = box.querySelector(".switch-text");
      if (lab) lab.textContent = on ? "已开启" : "已关闭";
    });
    return box;
  }

  if (type === "int" || type === "float") {
    return input(text0, function (ev) {
      var raw = String(ev.target.value || "").trim();
      if (!raw) { apply(f.value); ev.target.value = String(f.value); return; }
      var num = type === "int" ? parseInt(raw, 10) : parseFloat(raw);
      if (isNaN(num)) {
        toast((f.title || f.key) + "：只能填数字", "warn");
        apply(f.value);
        ev.target.value = String(f.value);
        return;
      }
      apply(num);
      ev.target.value = String(num);
    }, { type: "number", step: type === "int" ? "1" : "0.1", mono: true });
  }

  if (type === "list") {
    var arr0 = (cur && cur.length) ? cur : [];
    return textarea(arr0.join(nl()), function (ev) {
      var arr = String(ev.target.value || "").split(nl()).map(function (s) { return s.trim(); })
        .filter(function (s) { return s.length > 0; });
      apply(arr);
      ev.target.value = arr.join(nl());
    }, { rows: Math.min(9, Math.max(3, arr0.length + 1)), mono: true, placeholder: "每行一条" });
  }

  if (f.secret) {
    var masked = (state.config && state.config.masked) || "__astrbot_masked__";
    var raw0 = (f.value === null || f.value === undefined) ? "" : String(f.value);
    var isSet = raw0 === masked;
    var shown = (cur === f.value) ? "" : text0;
    var inp = input(shown, function (ev) {
      var t = String(ev.target.value || "");
      apply(t ? t : f.value);
    }, { type: "password", mono: true, placeholder: isSet ? "已配置 · 留空表示不改动" : "未配置" });
    if (!isSet) return inp;
    inp.classList.add("grow");
    return h("div", { class: "row-tight" }, [
      inp,
      btn("清空", { sm: true, kind: "ghost", title: "把这个密钥保存成空值", onclick: function () {
        apply("");
        inp.value = "";
        inp.placeholder = "保存后将被清空";
        toast("已标记清空，记得点保存", "warn");
      } })
    ]);
  }

  var options = f.options || [];
  if (options.length) {
    return select(options, text0, function (ev) { apply(String(ev.target.value)); });
  }

  var multiline = type === "text" || text0.indexOf(nl()) >= 0 || text0.length > 110;
  if (multiline) {
    return textarea(text0, function (ev) { apply(String(ev.target.value)); },
      { rows: text0.length > 400 ? 9 : 6, mono: false, placeholder: "留空则用默认值" });
  }

  var monoish = new RegExp("(url|path|regex|model|_id|key|token)", "i").test(String(f.key));
  return input(text0, function (ev) { apply(String(ev.target.value)); },
    { mono: monoish, placeholder: (f.default === null || f.default === undefined || f.default === "") ? null : String(f.default) });
}

function cfgFieldNode(parent, f) {
  var type = String(f.type || "string");
  var cur = type === "object" ? null : cfgGet(parent, f);
  var text0 = (cur === null || cur === undefined) ? "" : String(cur);
  var full = type === "object" || type === "list" || type === "text"
    || text0.indexOf(nl()) >= 0 || text0.length > 110;

  var holder = h("div", {
    class: "cfg-field",
    "data-dirty": (type !== "object" && cfgIsDirty(parent, f.key)) ? "true" : "false"
  });
  if (full) holder.setAttribute("data-span", "full");

  var lab = h("label", { class: "field-label" }, [h("span", { text: f.title || f.key })]);
  lab.appendChild(h("span", { class: "field-key", text: f.key }));
  if (f.secret) lab.appendChild(badge("密钥", "accent"));
  if (cfgRestartTag(f.key)) lab.appendChild(h("span", { class: "cfg-restart" }, badge("需重载插件", "warn")));
  holder.appendChild(lab);
  if (f.description) holder.appendChild(h("p", { class: "field-desc", text: f.description }));

  if (type === "object") {
    var kids = h("div", { class: "cfg-children" });
    var children = f.children || [];
    if (!children.length) kids.appendChild(dim("这一组没有可见子项"));
    children.forEach(function (child) { kids.appendChild(cfgFieldNode(f.key, child)); });
    holder.appendChild(kids);
    return holder;
  }

  holder.appendChild(cfgControl(parent, f, holder));

  var hints = [];
  if (f.hint) hints.push(String(f.hint));
  var dflt = f.default;
  if (type === "bool") hints.push("默认 " + (dflt ? "开" : "关"));
  else if (type === "list") hints.push("默认 " + (((dflt || []).length) ? (dflt || []).join(" / ") : "空"));
  else if (!f.secret && dflt !== null && dflt !== undefined && String(dflt) !== "") hints.push("默认 " + shorten(String(dflt), 52));
  if (options0(f).length) hints.push("可选 " + options0(f).join(" / "));
  if (hints.length) holder.appendChild(h("p", { class: "field-hint", text: hints.join("  ·  ") }));
  return holder;
}

function options0(f) { return f.options || []; }

/* 搜索命中：object 只要有一个子键命中就整块显示 */
function cfgMatch(f, q) {
  if (!q) return true;
  var hay = String(f.key || "") + " " + String(f.title || "") + " " + String(f.description || "") + " " + String(f.hint || "");
  if (hay.toLowerCase().indexOf(q) >= 0) return true;
  var children = f.children || [];
  for (var i = 0; i < children.length; i++) if (cfgMatch(children[i], q)) return true;
  return false;
}

function saveConfig() {
  if (state.cfg.saving) return;
  var n = cfgDirtyCount();
  if (!n) { toast("没有需要保存的修改", "warn"); return; }
  var values = cfgPayload();
  state.cfg.saving = true;
  apiPost("config/save", { values: values }).then(function (d) {
    d = d || {};
    var saved = d.saved || [];
    var rejected = d.rejected || [];
    state.cfg.needsReload = !!d.needs_reload;
    state.cfg.savedAt = d.saved_at || "";
    state.cfg.rejected = rejected;
    state.cfg.dirty = {};
    if (rejected.length) toast("已保存 " + saved.length + " 项，" + rejected.length + " 项被拒绝", "warn", 9000);
    else toast("已保存 " + saved.length + " 项配置", "ok");
    return LOADERS.config();
  }).then(function () {
    state.cfg.saving = false;
    state.loaded.config = true;
    renderConfig();
    refreshOverview();
  }).catch(function (e) {
    state.cfg.saving = false;
    fail(e, "保存失败");
    renderConfig();
  });
}

function renderConfig() {
  var v = clear(viewNode("config"));
  var d = state.config;
  if (!d) { v.appendChild(empty("配置还没加载", "点右上角「刷新」重试。")); return; }

  var groups = d.groups || [];
  var q = String(state.cfg.q || "").trim().toLowerCase();

  /* ---------- 头部：说明 + 搜索 ---------- */
  var qInput = input(state.cfg.q || "", null, {
    placeholder: "搜配置名 / 键名 / 说明…",
    oninput: function (ev) {
      state.cfg.q = ev.target.value;
      renderConfig();
      var node = $("cfg-q");
      if (node) { node.focus(); try { node.setSelectionRange(node.value.length, node.value.length); } catch (x) {} }
    }
  });
  qInput.id = "cfg-q";
  qInput.classList.add("grow");

  var jump = h("div", { class: "chips" });

  v.appendChild(card({
    kicker: "CONFIG",
    title: "插件配置",
    desc: "改完点底部「保存配置」。这里写的值和 AstrBot 插件管理页里的配置是同一份，保存即生效（少数项需要重载插件）。",
    tools: [btn("重新读取", { sm: true, kind: "ghost", onclick: function () { reloadTab("config"); } })],
    body: [
      h("div", { class: "row-tight" }, [qInput, badge(d.total + " 项", "mute")]),
      jump
    ]
  }));

  if (state.cfg.needsReload) {
    v.appendChild(note("刚保存的项目里包含「需重载插件」的配置（保活相关），去 AstrBot 插件管理页禁用再启用本插件才会生效。", "warn"));
  }
  if (state.cfg.rejected && state.cfg.rejected.length) {
    var rej = h("ul", { class: "diff-list" });
    state.cfg.rejected.forEach(function (item) {
      rej.appendChild(h("li", {}, [mono(String(item.key)), h("span", { class: "diff-why", text: String(item.reason || "") })]));
    });
    v.appendChild(card({
      kicker: "REJECTED",
      title: "上次保存被拒绝的项",
      desc: "这些键没有写进配置，检查一下取值范围。",
      tools: [btn("知道了", { sm: true, kind: "ghost", onclick: function () { state.cfg.rejected = []; renderConfig(); } })],
      body: rej
    }));
  }

  /* ---------- 分组 ---------- */
  var shown = 0;
  groups.forEach(function (g) {
    var fields = (g.fields || []).filter(function (f) { return cfgMatch(f, q); });
    if (!fields.length) return;
    shown += fields.length;

    var grid = h("div", { class: "cfg-fields" });
    fields.forEach(function (f) { grid.appendChild(cfgFieldNode("", f)); });

    var box = card({
      kicker: String(g.id || "").toUpperCase(),
      title: g.title || g.id,
      desc: g.description || "",
      body: grid,
      class: "cfg-group"
    });
    v.appendChild(box);

    jump.appendChild(h("button", {
      type: "button", class: "chip chip-btn", text: (g.title || g.id) + " " + fields.length,
      onclick: function () { try { box.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (x) { box.scrollIntoView(); } }
    }));
  });

  if (!shown) v.appendChild(empty("没有匹配的配置项", "换个关键词，或清空搜索框。"));

  /* ---------- 保存条 ---------- */
  var txt = h("span", { class: "savebar-text" });
  var bar = h("div", { class: "savebar", hidden: true }, [
    txt,
    h("span", { class: "grow" }),
    btn("放弃修改", { kind: "ghost", onclick: function () {
      if (!cfgDirtyCount()) return;
      state.cfg.dirty = {};
      renderConfig();
      toast("已放弃未保存的修改", "ok");
    } }),
    btn(state.cfg.saving ? "保存中…" : "保存配置", { kind: "primary", disabled: !!state.cfg.saving, onclick: saveConfig })
  ]);
  bar.textNode = txt;
  state.cfg.barNode = bar;
  v.appendChild(bar);
  cfgUpdateBar();

  if (state.cfg.savedAt) {
    v.appendChild(h("p", { class: "field-hint", text: "上次保存：" + state.cfg.savedAt }));
  }
}


/* ============================================================ 服务器 */

function srvItem(item) {
  var stateName = item.ok ? (item.busy ? "busy" : "ok") : "down";
  var sub = h("div", { class: "srv-sub" });
  sub.appendChild(badge(item.ok ? (item.busy ? "忙" : "在线") : "离线", item.ok ? (item.busy ? "warn" : "ok") : "danger"));
  if (item.latency_ms !== null && item.latency_ms !== undefined) sub.appendChild(chip(fmtMs(item.latency_ms), "chip-mono"));
  sub.appendChild(chip("角色 " + (item.character_count || 0)));
  var names = item.characters || [];
  names.slice(0, 6).forEach(function (n) { sub.appendChild(chip(n)); });
  if (names.length > 6) sub.appendChild(dim("+" + (names.length - 6)));

  var main = h("div", { class: "srv-main" }, [
    h("div", { class: "srv-url", title: item.url, text: item.url || "—" }),
    sub
  ]);
  if (item.error) main.appendChild(h("div", { class: "srv-err", text: shorten(String(item.error), 220) }));

  return h("div", { class: "srv" }, [
    h("i", { class: "srv-dot", "data-state": stateName }),
    main,
    btn("复制", { sm: true, kind: "ghost", onclick: function () { copyText(item.url, "服务器地址"); } })
  ]);
}

function renderServers() {
  var v = clear(viewNode("servers"));
  var d = state.servers;
  if (!d) { v.appendChild(empty("还没探测服务器", "点右上角「刷新」开始探测。")); return; }

  var servers = d.servers || [];
  var ka = d.keepalive || {};
  var down = servers.length - (d.online || 0);

  v.appendChild(card({
    kicker: "SERVERS",
    title: "TTS 服务器",
    desc: "逐个访问服务器判断可用性，顺便读回上面有哪些角色模型。单个探测超时 8 秒。",
    tools: [btn("重新探测", { sm: true, kind: "primary", onclick: function () { reloadTab("servers"); } })],
    body: [
      h("div", { class: "stat-grid" }, [
        stat(String(d.online || 0) + " / " + (d.total || 0), "在线 / 总数", down > 0 ? "warn" : "ok"),
        stat(String(d.queue_size || 0), "合成队列"),
        stat(String(ka.urls ? ka.urls.length : 0), "保活地址"),
        stat(ka.running ? "运行中" : "未运行", "保活任务", ka.enabled ? (ka.running ? "ok" : "warn") : null)
      ]),
      kv([["探测时间", d.checked_at || "—"]])
    ]
  }));

  var list = h("div", { class: "srv-list" });
  if (!servers.length) list.appendChild(empty("还没配置服务器", "去「配置 → 服务器」里填 tts_server_urls。"));
  servers.forEach(function (item) { list.appendChild(srvItem(item)); });
  v.appendChild(card({ kicker: "ENDPOINTS", title: "地址列表", body: list, sub: true }));

  /* ---------- 角色名对不上 ---------- */
  var mis = d.mismatch || {};
  var missing = mis.missing_on_server || [];
  var extra = mis.not_registered_locally || [];
  if (missing.length || extra.length) {
    var body = [];
    if (missing.length) {
      body.push(note("这些角色在 emotions.json 里登记了，但服务器上没有同名模型，合成时会直接失败。", "danger"));
      var c1 = h("div", { class: "chips" });
      missing.forEach(function (n) { c1.appendChild(chip(n, "chip-danger")); });
      body.push(c1);
    }
    if (extra.length) {
      body.push(note("服务器上还有这些角色没登记到本地感情库，可以在「感情库」里补上参考音频。", "warn"));
      var c2 = h("div", { class: "chips" });
      extra.forEach(function (n) { c2.appendChild(chip(n)); });
      body.push(c2);
    }
    v.appendChild(card({ kicker: "MISMATCH", title: "角色名对不上", body: body }));
  } else if (servers.length && d.online) {
    v.appendChild(note("本地感情库里的角色都能在服务器上找到，没有对不上的。", "ok"));
  }

  /* ---------- 保活 ---------- */
  var kaUrls = h("div", { class: "chips" });
  (ka.urls || []).forEach(function (u) { kaUrls.appendChild(chip(shorten(u, 58), "chip-mono")); });
  if (!(ka.urls || []).length) kaUrls.appendChild(dim("没有保活地址"));
  v.appendChild(card({
    kicker: "KEEPALIVE",
    title: "HuggingFace Space 保活",
    desc: "免费 Space 闲置会睡眠，醒来那一下要等几十秒。保活任务按间隔空跑一次把它叫醒。",
    body: [
      kv([
        ["开关", ka.enabled ? "已开启" : "已关闭"],
        ["任务状态", ka.running ? "运行中" : "未运行"],
        ["间隔", (ka.interval_minutes || 0) + " 分钟"]
      ]),
      kaUrls,
      note("保活这三个键改完需要重载插件才生效（在插件管理页禁用再启用即可）。", "warn")
    ],
    sub: true
  }));
}

/* ============================================================ 会话 */

function toggleSession(target, scope, kind, enabled) {
  if (state.busy.sessions) return;
  state.busy.sessions = true;
  renderSessions();
  apiPost("sessions/toggle", { target: target, scope: scope, kind: kind, enabled: enabled }).then(function (d) {
    state.sessions = d;
    state.busy.sessions = false;
    renderSessions();
    refreshOverview();
    toast((scope === "group" ? "群 " : "") + shorten(String(target), 28) + (enabled ? " 已开启" : " 已关闭"), "ok");
  }).catch(function (e) {
    state.busy.sessions = false;
    renderSessions();
    fail(e, "切换失败");
  });
}

function openSessionForm(scope) {
  var isGroup = scope === "group";
  var st = { kind: "tts", enabled: true };
  var target = input("", null, { mono: true, placeholder: isGroup ? "123456789" : "platform:MessageType:id" });
  var rows = [
    field({
      label: isGroup ? "群号" : "会话 ID",
      control: target,
      desc: isGroup ? "只填数字群号。" : "完整会话 ID，形如 aiocqhttp:GroupMessage:123456789，可从下面列表复制。"
    })
  ];
  if (!isGroup) {
    rows.push(field({
      label: "开关类型",
      desc: "普通配音跟随默认或指定音色；自动情感（W）按文本选情感。关闭单独设置后仍可能继承群配置。",
      control: segment([{ value: "tts", label: "自动配音" }, { value: "w", label: "W 模式" }], st.kind, function (v) {
        st.kind = v;
        toast(v === "w" ? "将切换 W 模式" : "将切换自动配音", null, 1400);
      })
    }));
  }
  rows.push(field({
    label: "目标状态",
    control: segment([{ value: "on", label: "开启" }, { value: "off", label: "关闭" }], "on", function (v) { st.enabled = v === "on"; })
  }));

  formModal({
    kicker: isGroup ? "GROUP" : "SESSION",
    title: isGroup ? "手动开关某个群" : "手动开关某个会话",
    okText: "应用",
    body: rows,
    onOk: function () {
      var value = String(target.value || "").trim();
      if (!value) { toast("先填目标", "warn"); return false; }
      toggleSession(value, isGroup ? "group" : "session", st.kind, st.enabled);
      return true;
    }
  });
}

function renderSessions() {
  var v = clear(viewNode("sessions"));
  var d = state.sessions;
  if (!d) { v.appendChild(empty("会话状态还没加载", "点右上角「刷新」重试。")); return; }
  var rows = d.sessions || [], rules = d.group_rules || [];
  var groups = d.groups || {}, counts = d.counts || {};
  var busy = !!state.busy.sessions;
  function sourceBadges(sources) {
    return h("div", { class: "chips" }, (sources || []).map(function (s) { return badge(s, "mute"); }));
  }
  function statusBadge(status) {
    return badge(status.blocked ? "已禁用" : status.enabled ? "已开启" : "未开启", status.blocked ? "danger" : status.enabled ? "ok" : "mute");
  }
  v.appendChild(card({
    kicker: "SESSIONS", title: "会话与生效来源",
    desc: "配置和指令都能开启配音。群设置覆盖整群；/tts-llm、/tts-w 设置当前会话。这里同时显示群规则和已识别会话。",
    tools: [
      btn("指定会话", { sm: true, kind: "soft", onclick: function () { openSessionForm("session"); } }),
      btn("指定群", { sm: true, kind: "soft", onclick: function () { openSessionForm("group"); } }),
      btn("刷新", { sm: true, kind: "ghost", onclick: function () { reloadTab("sessions"); } })
    ],
    body: [h("div", { class: "stat-grid" }, [
      stat(String(counts.effective || 0), "实际开启的已知会话", "ok"),
      stat(String(counts.groups_enabled || 0), "已知开启群", "accent"),
      stat(String((groups.whitelist || []).length), "配置白名单群"),
      stat(String((groups.blacklist || []).length), "配置禁用群")
    ]), kv([
      ["群聊默认", d.group_default ? "全部群默认开启（含尚未识别的群）" : "按白名单或单独开关开启"],
      ["状态持久化", d.persistence ? "已开启，重启保留开关和会话记录" : "已关闭，重启清空单独开关和会话记录；配置仍保留"],
      ["配音触发", "已开启表示允许自动配音；每次是否生成仍由触发频率、LLM 回复和音色配置决定。"]
    ])]
  }));

  var gt = table(["群号", "群级配音", "生效来源", { label: "群开关", class: "acts" }]);
  if (!rules.length) gt.body.appendChild(h("tr", {}, h("td", { colspan: 4 }, empty(
    d.group_default ? "全部群默认开启" : "尚未配置指定群",
    d.group_default ? "群聊触发 LLM 后会自动记录群号；无需先发开启指令。" : "在配置中添加群白名单，或用「指定群」及 /ttg 开启。"
  ))));
  rules.forEach(function (g) {
    gt.body.appendChild(h("tr", {}, [
      h("td", { class: "mono", text: g.group_id }), h("td", {}, statusBadge(g)),
      h("td", {}, sourceBadges(g.sources)), h("td", { class: "acts" }, btn(g.enabled ? "关闭群配音" : "开启群配音", {
        sm: true, kind: "ghost", disabled: busy || g.blocked,
        title: g.blocked ? "请先从配置群黑名单移除" : "等价于 /ttg 或 /ttg-q，只调整群级配音",
        onclick: function () { toggleSession(g.group_id, "group", "tts", !g.enabled); }
      }))
    ]));
  });
  v.appendChild(card({ kicker: "GROUP RULES", title: "群配置与开关 · " + rules.length,
    desc: "白名单、配置禁用群和手动群开关直接显示，无需先聊天。群级关闭后，会话单独开启仍有效；配置黑名单优先禁用。", body: gt, sub: true }));

  var t = table(["会话 / 最近识别", "实际自动配音", "生效来源", "单独普通配音", "单独自动情感", "会话音色", { label: "操作", class: "acts" }]);
  if (!rows.length) t.body.appendChild(h("tr", {}, h("td", { colspan: 7 }, empty("尚无已识别会话",
    "上方群配置已可生效。让 bot 正常回复一次，刷新后会自动出现在这里，无需 /tts-llm。旧版未保存的历史会话无法补回。"
  ))));
  rows.forEach(function (r) {
    var e = r.effective || { enabled: r.tts_active || r.w_active };
    var normalVoice = r.character ? r.character + " · " + (r.emotion || "默认情感") : "跟随默认";
    var wVoice = r.w_character || "跟随默认角色";
    function sessionSwitch(kind, checked) {
      var box = switchBox(checked, checked ? "已设定" : "未设定", function (ev) {
        if (busy || e.blocked) { ev.target.checked = checked; return; }
        toggleSession(r.session_id, "session", kind, !!ev.target.checked);
      });
      var control = box.querySelector("input");
      if (control) control.disabled = busy || e.blocked;
      return box;
    }
    t.body.appendChild(h("tr", {}, [
      h("td", {}, [h("span", { class: "cell-path", title: r.session_id, text: shorten(r.session_id, 42) }),
        h("div", { class: "tiny dim", text: r.last_seen ? fmtTime(r.last_seen * 1000) : "等待真实会话识别" })]),
      h("td", {}, statusBadge(e)), h("td", {}, sourceBadges(e.sources)),
      h("td", {}, sessionSwitch("tts", r.tts_active)), h("td", {}, sessionSwitch("w", r.w_active)),
      h("td", {}, [h("div", { text: "普通：" + normalVoice }), h("div", { class: "tiny dim", text: "自动情感：" + wVoice })]),
      h("td", { class: "acts" }, btn("复制 ID", { sm: true, kind: "ghost", onclick: function () { copyText(r.session_id, "会话 ID"); } }))
    ]));
  });
  v.appendChild(card({ kicker: "OBSERVED", title: "已识别与已设置会话 · " + rows.length,
    desc: "单独开关「未设定」时仍可继承群设置，以「实际自动配音」为准。普通与自动情感模式互斥。最近会话最多保留 " + (d.known_limit || 500) + " 条，单独设置另行保留。", body: t, sub: true }));
}

/* ============================================================ 指令表 */

function renderCommands() {
  var v = clear(viewNode("commands"));
  var d = state.commands;
  if (!d) { v.appendChild(empty("指令表还没加载", "点右上角「刷新」重试。")); return; }

  if (!state.cmd) state.cmd = { q: "" };
  var q = String(state.cmd.q || "").trim().toLowerCase();

  var qInput = input(state.cmd.q || "", null, {
    placeholder: "搜指令 / 说明…",
    oninput: function (ev) {
      state.cmd.q = ev.target.value;
      renderCommands();
      var node = $("cmd-q");
      if (node) { node.focus(); try { node.setSelectionRange(node.value.length, node.value.length); } catch (x) {} }
    }
  });
  qInput.id = "cmd-q";
  qInput.classList.add("grow");

  v.appendChild(card({
    kicker: "COMMANDS",
    title: "指令速查",
    desc: "全部指令在聊天里直接发。方括号里的参数可以省略，省略时用配置里的默认值。",
    body: h("div", { class: "row-tight" }, [qInput, badge(d.total + " 条", "mute")])
  }));

  var shown = 0;
  (d.groups || []).forEach(function (g) {
    var items = (g.items || []).filter(function (it) {
      if (!q) return true;
      return (String(it.usage) + " " + String(it.desc) + " " + String(it.alias || "")).toLowerCase().indexOf(q) >= 0;
    });
    if (!items.length) return;
    shown += items.length;
    var t = table(["指令", "说明", { label: "", class: "acts" }]);
    items.forEach(function (it) {
      var head = String(it.usage).split(" ")[0];
      t.body.appendChild(h("tr", {}, [
        h("td", {}, h("span", { class: "cell-path", text: it.usage })),
        h("td", {}, h("span", { text: it.desc || "" })),
        h("td", { class: "acts" }, btn("复制", { sm: true, kind: "ghost", title: "复制 " + head, onclick: function () { copyText(head, "指令"); } }))
      ]));
    });
    v.appendChild(card({ kicker: String(g.group), title: g.group + " · " + items.length + " 条", body: t, sub: true }));
  });

  if (!shown) v.appendChild(empty("没有匹配的指令", "换个关键词试试。"));
}

/* ============================================================ 日志 */

/* 级别 -> badge 色调。DEBUG 用 mute（CSS 里没有这条规则，会退化成默认灰，正合适）。 */
var LOG_TONES = { DEBUG: "mute", INFO: null, WARNING: "warn", ERROR: "danger", CRITICAL: "danger" };
var SYNTH_TONES = { ok: "ok", failed: "danger", skipped: "warn", pending: "accent" };
var LOG_AUTO_MS = 5000;

/* 浏览器里 setTimeout 返回数字，unref 不存在；Node 下跑 harness 时返回 Timeout，
   unref 掉可以让进程该退出就退出，不被这里的后台轮询拖着不放。 */
function softTimer(fn, ms) {
  var t = setTimeout(fn, ms);
  if (t && typeof t.unref === "function") { try { t.unref(); } catch (e) {} }
  return t;
}

function logQuery() {
  var u = state.logsUI;
  var q = { limit: 150 };
  if (u.level) q.level = u.level;
  if (u.tag) q.tag = u.tag;
  var s = String(u.q || "").trim();
  if (s) q.search = s;
  return q;
}

function synthQuery() {
  var u = state.logsUI;
  var q = { limit: 60, stats: u.stats || 60 };
  if (u.status) q.status = u.status;
  if (u.source) q.source = u.source;
  if (u.character) q.character = u.character;
  var s = String(u.sq || "").trim();
  if (s) q.search = s;
  return q;
}

/* 两个接口各自兜错：老版本插件没挂 run_log 时，另一半照样能画出来。 */
function softGet(ep, params) {
  return apiGet(ep, params).catch(function (e) {
    return { error: e && e.message ? e.message : String(e) };
  });
}

function fetchLogs() {
  var u = state.logsUI;
  var seq = ++u.seq;
  u.loading = true;
  return Promise.all([softGet("logs", logQuery()), softGet("logs/synths", synthQuery())])
    .then(function (both) {
      if (seq !== u.seq) return null;   /* 有更新的请求在飞，这份结果作废 */
      u.loading = false;
      state.logs = both[0] || {};
      state.synths = both[1] || {};
      return state.logs;
    });
}

/* 日志页是全站唯一会自己定时重画的分区，所以重画一律走保位版本：
   否则开着自动刷新往下翻记录，每 5 秒就被弹回页首一次。 */
function renderLogsKeep() { keepScroll(viewNode("logs"), renderLogs); }

function reloadLogs(silent) {
  return fetchLogs().then(function () {
    renderLogsKeep();
    if (!silent) toast("日志已刷新", "ok", 1600);
  }, function (e) { fail(e, "刷新失败"); });
}

function setLogFilter(key, value) {
  state.logsUI[key] = value;
  return fetchLogs().then(function () { renderLogsKeep(); });
}

/* 重渲染会换掉输入框节点，所以刷新前记住焦点、刷新后按 id 找回去。 */
function focusedLogSearchId() {
  try {
    var a = D.activeElement;
    if (a && (a.id === "log-q" || a.id === "log-sq")) return a.id;
  } catch (e) {}
  return "";
}

function refocusLogSearch(id) {
  if (!id) return;
  try {
    var node = $(id);
    if (!node) return;
    node.focus();
    try { node.setSelectionRange(node.value.length, node.value.length); } catch (x) {}
  } catch (e) {}
}

function logsSearchChanged() {
  var u = state.logsUI;
  u.lastKey = Date.now();
  if (u.debounce) { clearTimeout(u.debounce); u.debounce = null; }
  u.debounce = softTimer(function () {
    u.debounce = null;
    if (state.tab !== "logs") return;
    var focus = focusedLogSearchId();
    fetchLogs().then(function () { renderLogsKeep(); refocusLogSearch(focus); });
  }, 420);
}

function logsAutoStop() {
  var u = state.logsUI;
  if (u.timer) { clearTimeout(u.timer); u.timer = null; }
}

function logsAutoArm() {
  var u = state.logsUI;
  if (u.timer) return;
  if (!u.auto || state.tab !== "logs") return;
  u.timer = softTimer(logsAutoTick, LOG_AUTO_MS);
}

function logsAutoTick() {
  var u = state.logsUI;
  u.timer = null;
  if (!u.auto || state.tab !== "logs") return;   /* 离开日志页就自然停掉 */
  if (Date.now() - u.lastKey < 3000) { logsAutoArm(); return; }   /* 正在打字，让这一轮 */
  var focus = focusedLogSearchId();
  fetchLogs().then(function () { renderLogsKeep(); refocusLogSearch(focus); });
}

function clearLogs(scope) {
  var label = scope === "logs" ? "运行日志" : (scope === "synths" ? "合成记录" : "全部日志");
  confirmModal(
    "清空" + label + "？",
    "只清内存里的缓冲，配置、感情库和已经发出去的音频都不动。清完之前的记录就找不回来了。",
    { danger: true, okText: "清空" }
  ).then(function (ok) {
    if (!ok) return;
    apiPost("logs/clear", { scope: scope }).then(function (d) {
      var dr = d.dropped || {};
      toast("已清 " + (d.total || 0) + " 条（日志 " + (dr.logs || 0) + " / 合成 " + (dr.synths || 0) + "）", "ok");
      state.logsUI.expanded = {};
      state.logsUI.recNodes = {};
      fetchLogs().then(function () { renderLogs(); scrollTopSafe(); refreshOverview(); });
    }).catch(function (e) { fail(e, "清空失败"); });
  });
}

function downloadLogs(kind) {
  var q = kind === "synths" ? synthQuery() : logQuery();
  q.limit = 0;          /* 0 = 不限条数，导出整个缓冲 */
  delete q.stats;
  q.kind = kind;
  var name = "genie-tts-" + kind + ".txt";
  try {
    var task = SDK.download("logs/export", q, name);
    if (task && typeof task.then === "function") task.then(function () { toast("已下载 " + name, "ok"); }, function (e) { fail(e, "下载失败"); });
    else toast("已下载 " + name, "ok");
  } catch (e) { fail(e, "下载失败"); }
}

/* 复制走本地拼装，不再打一次接口 —— 复制的就是眼前看到的这一屏。 */
function copyLogText() {
  var u = state.logsUI;
  var TAB = String.fromCharCode(9);
  var lines = [];
  if (u.sub === "logs") {
    var items = (state.logs && state.logs.items) || [];
    if (!items.length) { toast("当前没有可复制的日志", "warn"); return; }
    items.forEach(function (it) {
      lines.push(
        String(it.date || "") + " " + String(it.time || "") + "  " + String(it.level || "") +
        "  " + String(it.tag_label || "") + "  " + String(it.message || "") +
        "  (" + String(it.source || "") + ")"
      );
    });
  } else if (u.sub === "stats") {
    var rows = (state.synths && state.synths.emotions) || [];
    if (!rows.length) { toast("还没有情感统计可复制", "warn"); return; }
    lines.push(["角色", "情感", "次数", "成功", "失败", "跳过", "失败率%", "平均耗时ms", "平均字数", "最近一次"].join(TAB));
    rows.forEach(function (r) {
      lines.push([
        r.character || "", r.emotion || "", r.total || 0, r.ok || 0, r.failed || 0,
        r.skipped || 0, r.fail_rate || 0, r.avg_elapsed_ms || 0, r.avg_chars || 0, r.last_time || ""
      ].join(TAB));
    });
  } else {
    var recs = (state.synths && state.synths.items) || [];
    if (!recs.length) { toast("还没有合成记录可复制", "warn"); return; }
    recs.forEach(function (r) {
      lines.push(
        "#" + String(r.id) + " " + String(r.date || "") + " " + String(r.time || "") +
        " [" + String(r.status_label || "") + "] " + String(r.source_label || "") + " " +
        String(r.character || "") + " / " + String(r.emotion || "") +
        (r.emotion_source ? " (" + String(r.emotion_source) + ")" : "") + " " + fmtMs(r.elapsed_ms)
      );
      if (r.llm_text) lines.push("  LLM: " + String(r.llm_text));
      if (r.tts_text) lines.push("  TTS: " + String(r.tts_text));
      if (r.reason) lines.push("  原因: " + String(r.reason));
    });
  }
  copyText(lines.join(nl()), "已复制 " + lines.length + " 行");
}

function logQuote(label, text, tone) {
  var v = text === null || text === undefined ? "" : String(text);
  if (!v) return null;
  return h("div", { class: "log-quote", "data-tone": tone || null }, [
    h("div", { class: "log-quote-lab" }, [
      h("span", { text: label + " · " + v.length + " 字" }),
      h("span", { class: "grow" }),
      btn("复制", { kind: "ghost", sm: true, title: "复制" + label, onclick: function () { copyText(v, "已复制" + label); } })
    ]),
    h("div", { class: "log-quote-text", text: v })
  ]);
}

/* ---------- 合成记录 ---------- */

/* 展开 / 折叠只换这一条记录的节点。
   以前是 renderLogs() 整页重画：clear(view) 一清，文档立刻变短，浏览器把
   scrollTop 夹到新的最大值，内容补回来时视口已经跑到别的地方了 —— 用户看到的
   就是「点一下展开，页面自己往上跳」。局部替换既没有这个问题，也顺手把焦点
   留在点过的那一行上（键盘操作能接着按空格）。 */
function toggleSynthRec(id) {
  var u = state.logsUI;
  var key = String(id);
  u.expanded[id] = !u.expanded[id];

  var items = (state.synths && state.synths.items) || [];
  var rec = null;
  for (var i = 0; i < items.length; i++) {
    if (String(items[i].id) === key) { rec = items[i]; break; }
  }
  var old = u.recNodes[key];
  if (!rec || !old || !old.parentNode || !attachedSafe(old)) {
    renderLogsKeep();                                 /* 兜底：数据或节点对不上就整页重画 */
    return;
  }

  var before = 0;
  var measured = false;
  try {
    if (typeof old.getBoundingClientRect === "function") { before = old.getBoundingClientRect().top; measured = true; }
  } catch (e1) {}

  var next = synthRec(rec);
  try {
    old.parentNode.insertBefore(next, old);
    old.parentNode.removeChild(old);
  } catch (e2) { renderLogsKeep(); return; }

  /* 折叠一条很高的记录时文档会变短，浏览器可能还是会夹掉 scrollTop；
     按同一行在视口里的位置回填，点过的那行就钉在原地不动。 */
  if (measured) {
    try {
      var delta = next.getBoundingClientRect().top - before;
      if (delta) scrollToSafe(scrollYSafe() + delta);
    } catch (e3) {}
  }
  try {
    var nh = next.querySelector(".log-rec-head");
    if (nh && typeof nh.focus === "function") {
      try { nh.focus({ preventScroll: true }); } catch (e4) { nh.focus(); }
    }
  } catch (e5) {}
}

var logAudioSeq = 0, logAudioUrl = "", logAudioElement = null;
function logAudioLabel(info) {
  return ({ cleared: "已清理", expired: "记录已过期", not_generated: "未生成音频", unavailable: "音频不可用" })[info && info.state] || "音频不可用";
}
function stopLogAudio() {
  logAudioSeq++;
  if (logAudioElement) { try { logAudioElement.pause(); logAudioElement.removeAttribute("src"); logAudioElement.load(); } catch (e) {} }
  logAudioElement = null;
  if (logAudioUrl) { URL.revokeObjectURL(logAudioUrl); logAudioUrl = ""; }
  var dock = $("log-audio-dock");
  if (dock) { clear(dock); dock.hidden = true; }
}
function logAudioControl(info) {
  if (!info) return null;
  var box = h("div", { class: "log-audio-control" });
  if (info.state !== "available") { append(box, badge(logAudioLabel(info), "mute")); return box; }
  append(box, btn("试听语音", { sm: true, kind: "soft", title: "播放这次合成的原文件，不重新合成", onclick: function () { playLogAudio(info, box); } }));
  return box;
}
function playLogAudio(info, control) {
  stopLogAudio();
  var seq = logAudioSeq;
  var dock = $("log-audio-dock");
  if (!dock) {
    dock = h("section", { id: "log-audio-dock", class: "log-audio-dock", "aria-label": "日志语音试听" });
    D.body.appendChild(dock);
  }
  dock.hidden = false;
  var label = h("span", { class: "tiny", text: "正在读取语音 #" + info.id + "…" });
  var playerBox = h("div", { class: "log-audio-player" });
  append(dock, [label, playerBox, btn("关闭试听", { sm: true, kind: "ghost", onclick: stopLogAudio })]);
  var chunks = [], total = 0;
  function read(offset) {
    if (seq !== logAudioSeq) return Promise.resolve();
    return apiGet("logs/audio", { id: info.id, instance: info.instance, offset: offset }).then(function (d) {
      if (seq !== logAudioSeq) return;
      if (d.state !== "available") {
        info.state = d.state;
        clear(control); append(control, badge(logAudioLabel(d), "mute"));
        label.textContent = "语音 #" + info.id + " · " + logAudioLabel(d);
        chunks = [];
        return;
      }
      if (d.bytes > 256 * 1024 * 1024) throw new Error("文件超过浏览器试听上限（256 MB）");
      var raw = atob(d.audio_base64 || ""), bytes = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      if (!bytes.length || d.offset !== offset || d.next_offset !== offset + bytes.length) throw new Error("音频读取不完整，请重试");
      if (total && total !== d.bytes) throw new Error("音频文件已变化，请刷新后重试");
      total = d.bytes; chunks.push(bytes);
      label.textContent = "语音 #" + info.id + " · 已读取 " + fmtBytes(d.next_offset) + " / " + fmtBytes(total);
      if (!d.done) return read(d.next_offset);
      if (d.next_offset !== total) throw new Error("音频读取不完整，请重试");
      logAudioUrl = URL.createObjectURL(new Blob(chunks, { type: d.mime || "audio/wav" }));
      chunks = [];
      logAudioElement = h("audio", { controls: true, preload: "metadata", src: logAudioUrl });
      append(playerBox, logAudioElement);
      label.textContent = "语音 #" + info.id + " · 原始音频";
      var played = logAudioElement.play();
      if (played && played.catch) played.catch(function () { if (seq === logAudioSeq) label.textContent = "语音 #" + info.id + " · 点击播放键试听"; });
    });
  }
  read(0).catch(function (e) {
    if (seq !== logAudioSeq) return;
    label.textContent = "试听失败：" + (e.message || String(e));
    chunks = [];
  });
}

function synthRec(r) {
  var u = state.logsUI;
  var open = !!u.expanded[r.id];
  var head = h("button", {
    type: "button", class: "log-rec-head", "aria-expanded": open ? "true" : "false",
    onclick: function () { toggleSynthRec(r.id); }
  }, [
    h("span", { class: "log-rec-time mono", text: String(r.date || "") + " " + String(r.time || "") }),
    badge(r.status_label || r.status || "—", SYNTH_TONES[r.status] || null),
    badge(r.source_label || r.source || "—", "mute"),
    h("span", { class: "log-rec-voice", text: (r.character || "—") + " · " + (r.emotion || "—") }),
    r.emotion_source ? badge(r.emotion_source, "accent") : null,
    h("span", { class: "log-rec-el mono", text: fmtMs(r.elapsed_ms) }),
    h("span", { class: "log-rec-caret", "aria-hidden": "true", text: open ? "▾" : "▸" })
  ]);
  var wrap = h("div", { class: "log-rec", "data-status": r.status || null, "data-rec": String(r.id) }, head);
  u.recNodes[String(r.id)] = wrap;
  append(wrap, logAudioControl(r.audio));
  if (!open) {
    var brief = r.llm_text || r.tts_text || r.display_text || r.reason || "";
    if (brief) wrap.appendChild(h("p", { class: "log-rec-text", title: brief, text: shorten(brief, 170) }));
    return wrap;
  }
  var body = h("div", { class: "log-rec-body" });
  body.appendChild(kv([
    ["会话", r.session],
    r.group ? ["群组", r.group] : null,
    ["语言", r.language],
    ["工作流", r.workflow],
    ["情感来源", r.emotion_source],
    r.candidates && r.candidates.length ? ["候选情感", r.candidates.join(" / ")] : null,
    ["参考音频", r.ref_audio, "mono"],
    ["输出方式", r.output_mode],
    r.audio_path ? ["音频文件", r.audio_path, "mono"] : null,
    r.audio_bytes ? ["体积 · 时长", fmtBytes(r.audio_bytes) + " · " + fmtSec(r.audio_seconds)] : null,
    ["分段 · 字数", String(r.chunks || 0) + " 块 · " + String(r.text_chars || 0) + " 字"],
    r.retries ? ["重试", String(r.retries) + " 次"] : null,
    r.translated ? ["翻译", "已翻译"] : null,
    r.truncated ? ["截断", "有截断"] : null,
    r.reason ? ["原因", r.reason] : null
  ]));
  append(body, [
    logQuote("LLM 原文", r.llm_text),
    logQuote("送进 TTS 的文本", r.tts_text, "accent"),
    logQuote("译文", r.translated_text),
    logQuote("参考文本", r.ref_text)
  ]);
  wrap.appendChild(body);
  return wrap;
}

function renderSynthList(v, S, sf) {
  var u = state.logsUI;
  var items = S.items || [];

  var sqInput = input(u.sq || "", null, {
    placeholder: "搜原文 / 角色 / 情感 / 会话 / 失败原因…",
    oninput: function (ev) { u.sq = ev.target.value; logsSearchChanged(); }
  });
  sqInput.id = "log-sq";
  sqInput.classList.add("grow");

  var statusOpts = [{ value: "", label: "全部状态" }, { value: "issue", label: "只看问题（失败 + 跳过）" }];
  (sf.statuses || []).forEach(function (x) { statusOpts.push({ value: x.key, label: (x.label || x.key) + " " + x.count }); });
  var sourceOpts = [{ value: "", label: "全部来源" }];
  (sf.sources || []).forEach(function (x) { sourceOpts.push({ value: x.key, label: (x.label || x.key) + " " + x.count }); });
  var charOpts = [{ value: "", label: "全部角色" }];
  (sf.characters || []).forEach(function (x) { charOpts.push({ value: x.key, label: x.key + " " + x.count }); });

  v.appendChild(card({
    kicker: "FILTER",
    title: "筛合成记录",
    body: h("div", { class: "row-tight" }, [
      sqInput,
      select(statusOpts, u.status, function (ev) { setLogFilter("status", ev.target.value); }),
      select(sourceOpts, u.source, function (ev) { setLogFilter("source", ev.target.value); }),
      select(charOpts, u.character, function (ev) { setLogFilter("character", ev.target.value); })
    ]),
    sub: true
  }));

  if (sf.full_text === false) {
    v.appendChild(note("当前只留文本摘要（run_log_full_text 关着），长句会被截到 160 字。想核对完整原文，去「配置 → 日志与诊断」把它打开。", "warn"));
  }

  if (!items.length) {
    v.appendChild(empty("还没有合成记录", "让 bot 说一句话，或者在工作台点一次合成，这里就会出现记录。"));
    return;
  }

  var box = h("div", { class: "log-recs" });
  items.forEach(function (r) { box.appendChild(synthRec(r)); });
  v.appendChild(card({
    kicker: "RECORDS",
    title: "合成记录 · " + items.length + " 条",
    desc: "点一行展开：LLM 原话、真正送进 TTS 的文本、命中的情感和来源、参考音频、耗时与失败原因都在里面。",
    body: box,
    sub: true
  }));
  v.appendChild(h("p", {
    class: "log-foot tiny dim",
    text: "当前展示 " + items.length + " 条 · 缓冲 " + (sf.size || 0) + " / " + (sf.capacity || 0) +
          " · 累计 " + (sf.total || 0) + " 条 · 生成于 " + (S.generated_at || "—")
  }));
}

/* ---------- 运行日志 ---------- */

/* 一条日志正文里真正值得挑出来的只有几类东西：方括号 ID（会话、[emotion=…]）、
   引号里的原话、成败关键词、标识符与文件路径、全大写缩写、带单位的数字。
   分组顺序就是优先级（先具体后宽泛），命中哪一组就套那一组的 class。
   本文件统一用 new RegExp 拼字符串，不用正则字面量，和上面几处保持一致。 */
var LOG_TOKEN_CLASSES = [
  "lt-id", "lt-quote", "lt-bad", "lt-good", "lt-warn", "lt-code", "lt-abbr", "lt-word", "lt-num"
];
var LOG_TOKEN_RE = new RegExp(
  "(\\[[^\\[\\]\\n]{1,160}\\])" +                                    /* [aiocqhttp:GroupMessage:123] [emotion=happy] */
  "|(\u300c[^\u300d\\n]{0,300}\u300d|\u201c[^\u201d\\n]{0,300}\u201d)" +   /* 「原话」 “原话” */
  "|(失败|错误|异常|超时|放弃|不可用|无法|拒绝)" +
  "|(成功|完成|已发送|命中|就绪|已加载|已注册)" +
  "|(跳过|重试|回退|截断|忽略|降级|警告)" +
  "|([A-Za-z_][A-Za-z0-9_]*(?:[._/-][A-Za-z0-9_]+)+(?::\\d+)?)" +    /* tts_engine.py:441  emotions.json  aino/normal  Worker-1 */
  "|([A-Z][A-Z0-9]{1,7}(?![a-z]))" +                                 /* TTS LLM WARNING */
  "|([A-Za-z][A-Za-z0-9]{1,})" +                                     /* 普通拉丁词：只换等宽字体，不换颜色 */
  "|(\\d+(?:\\.\\d+)?(?:ms|s|KB|MB|B|%|字|块|条|次|个|秒)?)",
  "g"
);

/* 正文长度与 token 数的上限。日志页一屏可能上百行，正文又可能是一整段 LLM 输出，
   分词是纯前端开销，超了就退回纯文本 —— 宁可不好看，也别把页面卡住。 */
var LOG_PAINT_MAX_LEN = 1200;
var LOG_PAINT_MAX_TOKENS = 400;

/* 把一条正文切成若干节点。
   铁律：所有片段拼回去必须与原文逐字符相同。日志的复制、下载、搜索全靠
   textContent，这里多插一个字符或少一个空格，导出的文本就跟真实日志不一致了。 */
function paintLogText(text) {
  var s = text === null || text === undefined ? "" : String(text);
  var out = [];
  if (!s) return out;
  if (!state.logsUI.paint || s.length > LOG_PAINT_MAX_LEN) {
    out.push(D.createTextNode(s));
    return out;
  }
  LOG_TOKEN_RE.lastIndex = 0;
  var last = 0;
  var count = 0;
  var m;
  while ((m = LOG_TOKEN_RE.exec(s)) !== null) {
    if (m[0] === "") { LOG_TOKEN_RE.lastIndex++; continue; }   /* 空匹配理论上不会有，兜一层死循环 */
    if (++count > LOG_PAINT_MAX_TOKENS) break;
    if (m.index > last) out.push(D.createTextNode(s.slice(last, m.index)));
    var cls = "lt-word";
    for (var g = 0; g < LOG_TOKEN_CLASSES.length; g++) {
      if (m[g + 1] !== undefined) { cls = LOG_TOKEN_CLASSES[g]; break; }
    }
    out.push(h("span", { class: cls, text: m[0] }));
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push(D.createTextNode(s.slice(last)));
  return out;
}

function renderLogList(v, L, lf) {
  var u = state.logsUI;
  var items = L.items || [];
  var dict = L.dictionaries || {};

  var qInput = input(u.q || "", null, {
    placeholder: "搜正文 / 会话 / 分类…",
    oninput: function (ev) { u.q = ev.target.value; logsSearchChanged(); }
  });
  qInput.id = "log-q";
  qInput.classList.add("grow");

  var levelOpts = [{ value: "", label: "全部级别" }];
  if (dict.issue_level) levelOpts.push({ value: dict.issue_level, label: "只看问题 " + (lf.issues || 0) });
  (lf.levels || []).forEach(function (x) { levelOpts.push({ value: x.key, label: x.key + " " + x.count }); });
  var tagOpts = [{ value: "", label: "全部分类" }];
  (lf.tags || []).forEach(function (x) { tagOpts.push({ value: x.key, label: (x.label || x.key) + " " + x.count }); });

  v.appendChild(card({
    kicker: "FILTER",
    title: "筛运行日志",
    body: h("div", { class: "row-tight" }, [
      qInput,
      select(levelOpts, u.level, function (ev) { setLogFilter("level", ev.target.value); }),
      select(tagOpts, u.tag, function (ev) { setLogFilter("tag", ev.target.value); }),
      /* 着色纯前端，关掉就退回纯文本 —— 想整段复制、或者觉得花，随手关掉。 */
      switchBox(u.paint, "着色", function (ev) {
        u.paint = !!ev.target.checked;
        savePrefs();
        renderLogsKeep();
      })
    ]),
    sub: true
  }));

  if (!items.length) {
    v.appendChild(empty("没有匹配的日志", "换个级别或关键词试试，也可能是插件刚重载、还没打出日志。"));
    return;
  }

  /* data-tag 决定这一行的左边条与分类字的色相（14 个分类各一个 hue，见 style.css）；
     log-dot 是分类前那颗小圆点，扫一眼就能按颜色成组，不用逐行读分类名。 */
  var box = h("div", { class: "log-lines" });
  items.forEach(function (it) {
    box.appendChild(h("div", { class: "log-line", "data-level": it.level || null, "data-tag": it.tag || null }, [
      h("span", { class: "log-time mono", text: it.time || "" }),
      badge(it.level || "INFO", LOG_TONES[it.level] || null),
      h("i", { class: "log-dot", "aria-hidden": "true" }),
      h("span", { class: "log-tag", text: it.tag_label || it.tag || "" }),
      h("span", {
        class: "log-msg",
        title: String(it.message || "") + (it.session ? "  ·  会话 " + it.session : "")
      }, paintLogText(it.message)),
      h("span", { class: "log-src mono", text: it.source || "" }),
      logAudioControl(it.audio)
    ]));
  });
  v.appendChild(card({
    kicker: "LINES",
    title: "运行日志 · " + items.length + " 行",
    desc: "只收本插件自己打的日志，宿主和其它插件的不会混进来。分类是按正文自动判的。",
    body: box,
    sub: true
  }));
  v.appendChild(h("p", {
    class: "log-foot tiny dim",
    text: "当前展示 " + items.length + " 条 · 缓冲 " + (lf.size || 0) + " / " + (lf.capacity || 0) +
          " · 累计 " + (lf.total || 0) + " 条 · 生成于 " + (L.generated_at || "—")
  }));
}

/* ---------- 情感统计 ---------- */

function renderEmotionStats(v, S) {
  var u = state.logsUI;
  var rows = S.emotions || [];

  v.appendChild(card({
    kicker: "FILTER",
    title: "统计范围",
    body: h("div", { class: "row-tight" }, [
      dim("取前"),
      select(
        [{ value: 20, label: "20 组" }, { value: 60, label: "60 组" }, { value: 120, label: "120 组" }, { value: 200, label: "200 组" }],
        u.stats,
        function (ev) { setLogFilter("stats", Number(ev.target.value) || 60); }
      ),
      dim("按失败率倒序排，失败率相同的看次数。")
    ]),
    sub: true
  }));

  if (!rows.length) {
    v.appendChild(empty("还没有情感统计", "合成记录攒够几条之后，这里会按「角色 · 情感」聚合出各自的失败率。"));
    return;
  }

  var t = table(["角色 · 情感", "次数", "成功", "失败", "跳过", "失败率", "平均耗时", "平均字数", "最近一次", "情感来源"]);
  rows.forEach(function (r) {
    var fr = Number(r.fail_rate) || 0;
    var tone = fr >= 50 ? "danger" : (fr > 0 ? "warn" : "ok");
    var lastTitle = r.last_reason ? (String(r.last_status || "") + "：" + r.last_reason) : String(r.last_status || "");
    t.body.appendChild(h("tr", {}, [
      h("td", {}, h("span", { class: "cell-text", text: (r.character || "—") + " · " + (r.emotion || "—") })),
      h("td", {}, h("span", { class: "mono", text: String(r.total || 0) })),
      h("td", {}, h("span", { class: "mono", text: String(r.ok || 0) })),
      h("td", {}, h("span", { class: "mono", text: String(r.failed || 0) })),
      h("td", {}, h("span", { class: "mono", text: String(r.skipped || 0) })),
      h("td", {}, badge(fr + "%", tone)),
      h("td", {}, h("span", { class: "mono", text: fmtMs(r.avg_elapsed_ms) })),
      h("td", {}, h("span", { class: "mono", text: String(r.avg_chars || 0) })),
      h("td", {}, h("span", { class: "nowrap", title: lastTitle, text: r.last_time || "—" })),
      h("td", {}, h("span", { class: "cell-text", text: r.emotion_source_summary || r.source_summary || "—" }))
    ]));
  });
  v.appendChild(card({
    kicker: "EMOTIONS",
    title: "情感成绩单 · " + rows.length + " 组",
    desc: "失败率高的排最前面。想知道「哪些情感不好」，直接看这张表的头几行。",
    body: t,
    sub: true
  }));
  v.appendChild(note("失败率 =（失败 + 跳过）/ 已结束次数。跳过多半是文本被清洗空了、或者这个会话没开配音；失败才是合成真炸了 —— 回到「合成记录」点开对应那条看「原因」。", "info"));
}

/* ---------- 入口 ---------- */

function renderLogs() {
  var v = clear(viewNode("logs"));
  var u = state.logsUI;
  u.recNodes = {};                    /* 上一轮的记录节点已经被 clear 摘掉了，别留悬空引用 */
  var L = state.logs || {};
  var S = state.synths || {};
  var lf = L.facets || {};
  var sf = S.facets || {};

  var done = (Number(sf.ok) || 0) + (Number(sf.failed) || 0) + (Number(sf.skipped) || 0);
  var rate = Number(sf.success_rate) || 0;
  var rateTone = done > 0 ? (rate >= 90 ? "ok" : (rate >= 60 ? "warn" : "danger")) : null;
  var bad = (Number(sf.failed) || 0) + (Number(sf.skipped) || 0);
  var issues = Number(lf.issues) || 0;

  var tools = [
    u.loading ? h("span", { class: "spinner" }) : null,
    switchBox(u.auto, "自动刷新", function (ev) {
      u.auto = !!ev.target.checked;
      if (u.auto) { logsAutoArm(); toast("自动刷新已开，每 5 秒拉一次", "ok", 1600); }
      else { logsAutoStop(); toast("自动刷新已关", "ok", 1600); }
    }),
    btn("刷新", { kind: "soft", sm: true, onclick: function () { reloadLogs(false); } }),
    btn("复制", { kind: "ghost", sm: true, title: "把当前这一屏复制成文本", onclick: copyLogText }),
    btn("下载", { kind: "ghost", sm: true, title: "按当前筛选导出整个缓冲", onclick: function () { downloadLogs(u.sub === "logs" ? "logs" : "synths"); } }),
    btn("清空", { kind: "danger", sm: true, title: "清掉内存里的日志与合成记录", onclick: function () { clearLogs("all"); } })
  ];

  v.appendChild(card({
    kicker: "LOGS",
    title: "运行日志与合成追踪",
    desc: "每次配音都留一条记录：LLM 原话、真正送进 TTS 的文本、命中的情感和它是谁定的、耗时与失败原因。可直接试听这次生成的原音频，文件过期显示「已清理」。想知道「哪些情感不好」就翻到情感统计。",
    tools: tools,
    body: [
      h("div", { class: "stat-grid" }, [
        stat(String(sf.total || 0), "合成总数", null),
        stat(rate + "%", "成功率", rateTone),
        stat(String(bad), "失败 + 跳过", bad > 0 ? "danger" : null),
        stat(fmtMs(sf.avg_elapsed_ms), "平均耗时", null),
        stat(String(lf.size || 0) + " / " + String(lf.capacity || 0), "日志缓冲", null),
        stat(String(issues), "警告以上", issues > 0 ? "warn" : null)
      ]),
      /* 换子视图是换内容，不是换位置：明确回到页首，别把视口留在上一张长表的中段。 */
      segment([
        { value: "synths", label: "合成记录 · " + String(S.total || 0) },
        { value: "logs", label: "运行日志 · " + String(L.total || 0) },
        { value: "stats", label: "情感统计 · " + String((S.emotions || []).length) }
      ], u.sub, function (val) {
        if (val === u.sub) return;
        u.sub = val;
        renderLogs();
        scrollTopSafe();
      })
    ]
  }));

  if (L.error) v.appendChild(note("运行日志读取失败：" + L.error, "danger"));
  if (S.error) v.appendChild(note("合成记录读取失败：" + S.error, "danger"));
  if (L.enabled === false || S.enabled === false) {
    v.appendChild(note("日志采集已关闭，新的动作不会再被记下来。去「配置 → 日志与诊断」打开 enable_run_log。", "warn"));
  }
  if (L.attached === false && !L.error) {
    v.appendChild(note("日志采集器没挂上 AstrBot 的 logger，运行日志会一直是空的；合成记录不受影响。重载一次插件试试。", "warn"));
  }

  if (u.sub === "logs") renderLogList(v, L, lf);
  else if (u.sub === "stats") renderEmotionStats(v, S);
  else renderSynthList(v, S, sf);

  logsAutoArm();
}

/* ============================================================ 关于 */

var SWATCH = {
  moonlit:  ["#0a0e1a", "#2b3f80", "#8fb8ff", "#c6b0ff"],
  sakura:   ["#fff8fa", "#ffd5e2", "#e0709a", "#c184cf"],
  twilight: ["#150f1f", "#5c326e", "#f0a55e", "#e8708f"],
  aoi:      ["#0b1417", "#12595a", "#5fd3c4", "#6fa8ff"],
  usuyuki:  ["#f7f9fd", "#d5e6f8", "#4a86c8", "#74a8cd"],
  hiyo:     ["#0d0a0c", "#6d1a32", "#e8556f", "#f0955f"]
};

var ENDPOINTS = [
  ["总览", "GET", "overview", "统计、限额、主题清单"],
  ["感情", "GET", "emotions", "角色与感情全表"],
  ["感情", "POST", "emotions/upsert", "新增或改写一条感情"],
  ["感情", "POST", "emotions/delete", "批量删除感情"],
  ["感情", "POST", "emotions/copy", "复制 / 移动到别的角色"],
  ["感情", "POST", "emotions/rename-character", "重命名角色"],
  ["感情包", "GET", "emotions/export", "下载感情包 JSON"],
  ["感情包", "POST", "emotions/export-preview", "导出前先看文本"],
  ["感情包", "POST", "emotions/import", "导入感情包（支持试运行）"],
  ["感情包", "GET", "packs", "服务端快照列表"],
  ["感情包", "POST", "packs/save", "保存一份快照"],
  ["感情包", "POST", "packs/delete", "删除快照"],
  ["感情包", "POST", "packs/restore", "从快照恢复"],
  ["感情包", "GET", "packs/download", "下载快照文件"],
  ["收藏", "GET", "favorites", "读取整个音库"],
  ["收藏", "GET", "favorites/audio", "浏览器里试听原始音频"],
  ["收藏", "POST", "favorites/rename", "改名"],
  ["收藏", "POST", "favorites/update", "改备注 / 标签 / 置顶"],
  ["收藏", "POST", "favorites/delete", "删掉一条"],
  ["收藏", "POST", "favorites/clear", "清空音库"],
  ["收藏", "GET", "favorites/download", "下载单条音频"],
  ["收藏", "GET", "favorites/export", "导出收藏包 zip"],
  ["收藏", "POST", "favorites/import", "导入收藏包 zip"],
  ["收藏", "POST", "favorites/upload", "拖放上传音频入库"],
  ["收藏", "POST", "favorites/save-synth", "把试听结果存进收藏"],
  ["合成", "POST", "synthesize", "工作台试听合成"],
  ["合成", "POST", "preview", "分段与停顿预览"],
  ["服务器", "GET", "servers", "探测所有 TTS 服务器"],
  ["配置", "GET", "config", "分组后的配置表"],
  ["配置", "POST", "config/save", "写回配置并落盘"],
  ["会话", "GET", "sessions", "活跃会话与群名单"],
  ["会话", "POST", "sessions/toggle", "切换会话 / 群开关"],
  ["日志", "GET", "logs", "运行日志（支持级别 / 分类 / 关键词）"],
  ["日志", "GET", "logs/audio", "试听合成记录的原始音频"],
  ["日志", "GET", "logs/synths", "合成记录与情感统计"],
  ["日志", "POST", "logs/clear", "清空日志缓冲"],
  ["日志", "GET", "logs/export", "导出日志纯文本"],
  ["其它", "GET", "commands", "指令速查表"],
  ["其它", "GET", "prefs", "读取界面偏好"],
  ["其它", "POST", "prefs/save", "保存界面偏好"]
];

function themeGallery() {
  var box = h("div", { class: "gallery" });
  themeList().forEach(function (t) {
    var sw = h("div", { class: "gal-swatch" });
    var colors = SWATCH[t.id] || SWATCH.moonlit;
    colors.forEach(function (c) {
      var i = h("i");
      i.style.background = c;
      sw.appendChild(i);
    });
    box.appendChild(h("button", {
      type: "button",
      class: "gal",
      "aria-pressed": state.prefs.theme === t.id ? "true" : "false",
      onclick: function () { applyTheme(t.id, true); toast("主题已切到「" + t.name + "」", "ok", 1800); }
    }, [
      sw,
      h("div", { class: "gal-name", text: t.name + (t.dark ? " · 暗" : " · 亮") }),
      h("div", { class: "gal-hint", text: t.hint || "" })
    ]));
  });
  return box;
}

function renderAbout() {
  var v = clear(viewNode("about"));
  var o = state.overview;
  if (!o) { v.appendChild(empty("还在连 AstrBot", "稍等一下，或点右上角刷新。")); return; }

  var p = o.plugin || {};
  var c = o.counts || {};
  var lim = o.limits || {};
  var tg = o.toggles || {};
  var df = o.defaults || {};
  var ss = o.session || {};

  /* ---------- Hero ---------- */
  var chips = h("div", { class: "hero-chips" }, [
    chip("v" + (p.version || "?"), "chip-accent"),
    chip(c.endpoints + " 个接口", "chip-mono"),
    chip(c.themes + " 套主题"),
    chip(c.commands + " 条指令")
  ]);
  var hero = h("div", { class: "hero" }, [
    h("div", { class: "hero-copy" }, [
      h("span", { class: "hero-slug", text: p.name || "astrbot_plugin_genie_tts_llm" }),
      h("h1", { class: "hero-title" }, [
        h("span", { text: p.display_name || "Genie TTS LLM" }),
        h("small", { text: "  语音合成工作台" })
      ]),
      h("p", { class: "hero-line", text: "把 Genie TTS 接到 AstrBot：登记参考音频、让 LLM 自己挑感情、按句切块补停顿，再把成品语音发回聊天。这个页面是它的可视化控制台。" })
    ]),
    chips
  ]);
  var repo = String(p.repo || "");
  var repoRow = h("div", { class: "row-tight" }, [
    h("a", { class: "chip chip-link", href: repo, target: "_blank", rel: "noreferrer noopener", text: "GitHub 仓库" }),
    btn("复制仓库地址", { sm: true, kind: "ghost", onclick: function () { copyText(repo, "仓库地址"); } }),
    dim(repo)
  ]);
  v.appendChild(card({ body: [hero, rule(), repoRow] }));

  /* ---------- 主题画廊 ---------- */
  v.appendChild(card({
    kicker: "THEMES",
    title: "主题",
    desc: "六套 galgame 风格配色，选中会记在服务器上，下次打开还是它。密度开关在右上角。",
    tools: [segment(
      [{ value: "comfortable", label: "宽松" }, { value: "compact", label: "紧凑" }],
      state.prefs.density,
      function (val) { applyDensity(val, true); renderAbout(); }
    )],
    body: themeGallery()
  }));

  /* ---------- 三列信息 ---------- */
  function onoff(flag) { return flag ? "开" : "关"; }
  var limitsCard = card({
    kicker: "LIMITS",
    title: "限额与默认值",
    body: kv([
      ["自动配音字数上限", lim.max_text_length + " 字"],
      ["工作台单次上限", lim.synth_text_limit + " 字"],
      ["单次超时", lim.timeout_seconds + " 秒"],
      ["失败重试", lim.max_retries + " 次"],
      ["块间停顿", lim.chunk_gap_ms + " ms（最大 " + lim.max_chunk_gap_ms + "）"],
      ["自定义停顿上限", lim.max_custom_pause_ms + " ms"],
      ["每块句数", String(lim.sentences_per_chunk)],
      ["采样率", lim.sample_rate + " Hz"]
    ]),
    sub: true
  });
  var defaultsCard = card({
    kicker: "DEFAULTS",
    title: "默认音色与开关",
    body: kv([
      ["默认角色", df.character || "未设置"],
      ["默认感情", df.emotion || "未设置"],
      ["默认语言", df.language || "jp"],
      ["触发方式", df.trigger_mode || "always"],
      ["自动配音输出", df.auto_output_mode || "—"],
      ["工具调用输出", df.tool_output_mode || "—"],
      ["分句切块 / 自定义停顿", onoff(tg.sentence_splitting) + " / " + onoff(tg.custom_pause_marker)],
      ["翻译 / 文本清洗", onoff(tg.translation) + " / " + onoff(tg.text_cleaning)],
      ["泄漏拦截 / 失败提示", onoff(tg.leak_guard) + " / " + onoff(tg.failure_notice)]
    ]),
    sub: true
  });
  var sessionCard = card({
    kicker: "SESSION",
    title: "当前会话规模",
    body: kv([
      ["单独普通配音会话", String(ss.active_sessions || 0)],
      ["W 模式会话", String(ss.w_active_sessions || 0)],
      ["已知开启群", String(ss.active_groups || 0)],
      ["单独关闭群", String(ss.inactive_groups || 0)],
      ["记住音色的会话", String(ss.session_emotions || 0)],
      ["状态持久化", onoff(tg.state_persistence)],
      ["群聊默认开启", onoff(tg.group_default)],
      ["Space 保活", onoff(tg.keepalive)]
    ]),
    sub: true
  });
  v.appendChild(h("div", { class: "grid-3" }, [limitsCard, defaultsCard, sessionCard]));

  /* ---------- 接口清单 ---------- */
  var t = table(["分类", "方法", "接口", "用途"]);
  ENDPOINTS.forEach(function (row) {
    t.body.appendChild(h("tr", {}, [
      h("td", {}, chip(row[0])),
      h("td", {}, h("span", { class: "cell-path", text: row[1] })),
      h("td", {}, h("span", { class: "cell-path", text: row[2] })),
      h("td", {}, h("span", { text: row[3] }))
    ]));
  });
  v.appendChild(card({
    kicker: "API",
    title: "接口清单 · " + ENDPOINTS.length + " 条",
    desc: "全部挂在 /api/plug/astrbot_plugin_genie_tts_llm/ 下，沿用 AstrBot 自己的鉴权，未登录访问不到。",
    body: t,
    sub: true
  }));

  /* ---------- 安全须知 ---------- */
  v.appendChild(card({
    kicker: "SECURITY",
    title: "安全须知",
    body: [
      note("这个页面跑在 sandbox iframe 里，没有 same-origin 权限：所有请求都经父窗口的桥转发，读不到 Dashboard 的 cookie，也没法直连外部地址。", "ok"),
      note("配置里的密钥（api_key / token 之类）读出来永远是掩码，不会明文回传到浏览器。想改就直接填新值，留空表示不动。", "ok"),
      note("参考音频路径只允许相对路径且不能含 ..，避免读到 Space 工作目录外的文件。导入感情包时同样会逐条校验，不合法的会被记成 invalid。", "warn"),
      note("「感情包 → 导入」的 replace 模式会先清空整个感情库再写入，务必先勾「试运行」看清报告。旁边的「保存当前」随时能存一份快照兜底。", "danger")
    ],
    sub: true
  }));
}

/* ============================================================ 启动 */

if (D.readyState === "loading") D.addEventListener("DOMContentLoaded", init);
else init();

})();
