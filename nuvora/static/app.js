"use strict";
const $ = (id) => document.getElementById(id);
const S = {data: null, thread: "", csrf: "", busy: false, streaming: false, dirty: false, clearKey: false, messages: [], historyEpoch: 0};
const presets = {glm:"https://open.bigmodel.cn/api/paas/v4",deepseek:"https://api.deepseek.com",openai:"https://api.openai.com/v1",ollama:"http://localhost:11434/v1"};
const toolNames = {web_search:"联网搜索",web_fetch:"阅读网页",list_dir:"查看目录",read_file:"读取文件",write_file:"保存文件",run_python:"运行 Python",remember:"保存记忆",recall:"检索记忆",forget:"删除记忆",current_time:"查询时间",system_info:"查看环境"};
let toastTimer, searchTimer;
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name); svg.append(use); return svg;
}
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function toast(text) {
  $("toast").textContent = text; $("toast").classList.remove("hidden");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 4200);
}
async function api(path, body) {
  const options = {credentials:"same-origin",cache:"no-store"};
  if (body !== undefined) {
    options.method = "POST"; options.headers = {"Content-Type":"application/json","X-Nuvora-CSRF":S.csrf};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败，请重试。");
  return data;
}
function settingsPayload() {
  return {
    model:{base_url:$("base-url").value,model:$("model-name").value,api_key:$("api-key").value,temperature:Number($("temperature").value)},
    agent:{max_iterations:Number($("max-iterations").value)},
    tools:{web_enabled:$("web-enabled").checked,files_enabled:$("files-enabled").checked,python_enabled:$("python-enabled").checked,python_timeout:Number($("python-timeout").value),web_search_max_results:Number($("search-results").value)},
    memory:{enabled:$("memory-enabled").checked},cli:{stream:$("stream-enabled").checked},clear_api_key:S.clearKey
  };
}
function showSettings() {
  if (innerWidth >= 1200) document.body.classList.remove("settings-hidden");
  else document.body.classList.add("settings-open");
}
function closePanels() {
  document.body.classList.remove("settings-open","menu-open");
}
function fillSettings(data) {
  const c = data.config;
  $("base-url").value = c.model.base_url; $("model-name").value = c.model.model;
  $("api-key").value = ""; S.clearKey = false;
  $("key-state").textContent = c.model.api_key_configured ? "已保存" : "未设置";
  $("api-key").placeholder = c.model.api_key_configured ? "已保存，留空保留原密钥" : "输入 API Key";
  $("clear-key").classList.toggle("hidden", !c.model.api_key_configured);
  $("key-hint").textContent = "密钥仅保存在本地配置中";
  $("temperature").value = c.model.temperature; $("temperature-value").textContent = String(c.model.temperature);
  $("max-iterations").value = c.agent.max_iterations; $("python-timeout").value = c.tools.python_timeout;
  $("search-results").value = c.tools.web_search_max_results;
  $("web-enabled").checked = c.tools.web_enabled; $("files-enabled").checked = c.tools.files_enabled;
  $("python-enabled").checked = c.tools.python_enabled; $("memory-enabled").checked = c.memory.enabled;
  $("stream-enabled").checked = c.cli.stream;
  $("provider").value = Object.keys(presets).find(k => presets[k] === c.model.base_url) || "custom";
  const warnings = [data.config_warning];
  if (data.env_overrides.length) warnings.push("部分设置由环境变量覆盖：" + data.env_overrides.join("、"));
  $("config-warning").textContent = warnings.filter(Boolean).join(" ");
  $("config-warning").classList.toggle("hidden", !warnings.some(Boolean));
  $("python-status").textContent = data.python.available ? "系统隔离已就绪" : "隔离不可用，执行已阻止";
  $("python-status").title = data.python.detail;
}
function updateOverview(data) {
  S.data = data; S.csrf = data.csrf_token;
  $("version").textContent = "v" + data.version;
  $("memory-count").textContent = data.memory_count;
  $("current-model").textContent = data.config.model.model || "尚未配置模型";
  $("current-model").classList.toggle("ready", Boolean(data.config.model.model));
  $("setup-note").classList.toggle("hidden", Boolean(data.config.model.model));
  const c = data.config;
  const groups = [c.tools.web_enabled && "联网",c.tools.files_enabled && "文件",c.tools.python_enabled && data.python.available && "Python",c.memory.enabled && "记忆"].filter(Boolean);
  $("tool-summary").textContent = groups.length ? groups.join(" · ") : "时间与环境工具";
  renderSessions(data.sessions);
}
function markDirty() {
  S.dirty = true;
  $("saved-status").textContent = "有未保存的更改";
  $("saved-status").className = "saved-status dirty";
}
function setBusy(busy) {
  S.busy = busy;
  $("send-button").classList.toggle("hidden", busy);
  $("stop-button").classList.toggle("hidden", !busy);
  $("message-input").disabled = busy;
  $("new-chat").disabled = busy;
  $("stop-button").disabled = false; $("stop-button").textContent = "停止生成";
  for (const field of $("settings-form").querySelectorAll("input,select,button")) field.disabled = busy;
  for (const button of $("session-list").querySelectorAll("button")) button.disabled = busy;
}
function renderSessions(sessions) {
  $("session-count").textContent = sessions.length;
  $("session-list").replaceChildren();
  for (const item of sessions) {
    const button = el("button", "session-item" + (item.id === S.thread ? " active" : ""));
    button.append(icon("chat"),el("span","",item.title)); button.title = item.title;
    button.disabled = S.busy;
    button.addEventListener("click",() => openSession(item.id));
    $("session-list").append(button);
  }
}
async function openSession(id) {
  if (S.busy) return;
  const epoch = ++S.historyEpoch;
  try {
    const data = await api("/api/session?id=" + encodeURIComponent(id));
    if (epoch !== S.historyEpoch) return;
    S.thread = id; S.messages = data.messages;
    switchView("chat"); renderMessages(S.messages); renderSessions(S.data.sessions);
    closePanels(); $("message-input").focus();
  } catch (error) { toast(error.message); }
}
async function newChat() {
  if (S.busy) return;
  try {
    const item = await api("/api/session",{});
    S.thread = item.id; S.messages = []; S.historyEpoch++;
    const data = await api("/api/state"); updateOverview(data);
    switchView("chat"); renderMessages([]); closePanels(); $("message-input").focus();
  } catch (error) { toast(error.message); }
}
function switchView(name) {
  for (const view of document.querySelectorAll(".view")) view.classList.toggle("active",view.id === "view-" + name);
  for (const button of document.querySelectorAll(".nav-item")) button.classList.toggle("active",button.dataset.view === name);
  $("view-title").textContent = {chat:"智能对话",memory:"长期记忆",workspace:"文件工作区"}[name];
  document.body.classList.remove("menu-open");
  if (name === "memory") refreshMemories();
  if (name === "workspace") refreshFiles();
}
function inline(parent, text) {
  const regex = /(\*\*[^*]+\*\*|\x60[^\x60]+\x60|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g;
  let offset = 0, match;
  while ((match = regex.exec(text))) {
    parent.append(document.createTextNode(text.slice(offset,match.index)));
    const token = match[0];
    if (token.startsWith("**")) parent.append(el("strong","",token.slice(2,-2)));
    else if (token.charCodeAt(0) === 96) parent.append(el("code","",token.slice(1,-1)));
    else {
      const parsed = /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(token);
      const a = el("a","",parsed[1]); a.href = parsed[2]; a.target = "_blank"; a.rel = "noopener noreferrer"; parent.append(a);
    }
    offset = regex.lastIndex;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}
function markdown(parent, text) {
  parent.replaceChildren();
  const lines = String(text || "").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\x60{3}/.test(line)) {
      const language = line.slice(3).trim(); const code = [];
      i++; while (i < lines.length && !/^\x60{3}/.test(lines[i])) code.push(lines[i++]);
      i++;
      const block = el("div","code-block"), header = el("div","code-heading"), pre = el("pre"), copy = el("button","","复制");
      copy.addEventListener("click",() => copyText(code.join("\n")));
      header.append(el("span","",language || "代码"),copy); pre.append(el("code","",code.join("\n"))); block.append(header,pre); parent.append(block); continue;
    }
    if (!line.trim()) { i++; continue; }
    if (line.includes("|") && i+1 < lines.length && /^\s*\|?\s*:?-{3,}.*\|.*$/.test(lines[i+1])) {
      const wrap = el("div","table-wrap"), table = el("table"), head = el("tr");
      const cells = value => value.trim().replace(/^\||\|$/g,"").split("|").map(t=>t.trim());
      for (const value of cells(line)) { const th = el("th"); inline(th,value); head.append(th); }
      table.append(head); i+=2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        const row = el("tr"); for (const value of cells(lines[i++])) { const td = el("td"); inline(td,value); row.append(td); } table.append(row);
      }
      wrap.append(table); parent.append(wrap); continue;
    }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) { const h = el("h" + Math.min(heading[1].length+1,4)); inline(h,heading[2]); parent.append(h); i++; continue; }
    const listItem = /^\s*(?:[-*]|\d+\.)\s+(.+)$/.exec(line);
    if (listItem) {
      const list = el(/^\s*\d+\./.test(line) ? "ol" : "ul");
      while (i < lines.length) {
        const item = /^\s*(?:[-*]|\d+\.)\s+(.+)$/.exec(lines[i]);
        if (!item) break; const li = el("li"); inline(li,item[1]); list.append(li); i++;
      }
      parent.append(list); continue;
    }
    const paragraph = el("p"); inline(paragraph,line); parent.append(paragraph); i++;
  }
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("已复制"); }
  catch (_error) { toast("复制失败，请选中文本复制。"); }
}
function addTool(parent, call, result) {
  const details = el("details","tool-call" + (result ? " finished" : ""));
  details.dataset.call = call.id;
  const summary = el("summary");
  summary.append(icon("code"),el("span","",toolNames[call.name] || call.name),el("span","",result ? "已完成" : "调用中"));
  const pre = el("pre","",result ? result.text : JSON.stringify(call.args || {},null,2));
  details.append(summary,pre); parent.append(details);
  return details;
}
function makeMessage(message, results) {
  const node = el("article","message " + message.role);
  const avatar = el("div","avatar");
  avatar.append(message.role === "user" ? document.createTextNode("你") : icon("spark"));
  const body = el("div","message-body"), meta = el("div","message-meta"), copy = el("button","icon-button message-copy");
  copy.setAttribute("aria-label","复制消息"); copy.append(icon("copy"));
  copy.addEventListener("click",() => copyText(message.text));
  meta.append(el("span","",message.role === "user" ? "你" : "NUVORA"),copy);
  const text = el("div","message-text");
  if (message.role === "user") text.append(el("p","",message.text));
  else markdown(text,message.text);
  body.append(meta,text);
  for (const call of message.calls || []) addTool(body,call,results && results.get(call.id));
  node.append(avatar,body);
  return {node,body,text};
}
function renderMessages(messages) {
  $("welcome").classList.toggle("hidden", Boolean(messages.length));
  $("messages").replaceChildren();
  const results = new Map(messages.filter(m=>m.role==="tool").map(m=>[m.call_id,m]));
  for (const message of messages) {
    if (message.role !== "tool") $("messages").append(makeMessage(message,results).node);
  }
  scrollChat();
}
function scrollChat() { $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight; }
async function sendMessage(event) {
  event.preventDefault();
  if (S.busy) return;
  if (!S.data.config.model.model) { showSettings(); toast("先选择或填写模型并保存。"); return; }
  if (S.dirty) { showSettings(); toast("先保存右侧的设置，让新配置生效。"); return; }
  const text = $("message-input").value.trim(); if (!text) return;
  const prompt = {role:"user",text}, answer = {role:"assistant",text:"",calls:[]};
  const before = S.messages.slice();
  S.messages.push(prompt); renderMessages(S.messages);
  const live = makeMessage(answer);
  const thinking = el("div","thinking"); thinking.append(el("span"),el("span"),el("span")); live.body.append(thinking);
  $("messages").append(live.node); $("message-input").value = "";
  setBusy(true); S.streaming = true; scrollChat();
  let terminal = false;
  try {
    const response = await fetch("/api/chat",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-Nuvora-CSRF":S.csrf},body:JSON.stringify({thread_id:S.thread,text})});
    if (!response.ok) { const error = await response.json(); throw new Error(error.error || "对话请求失败。"); }
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
    function consume(frame) {
      const lines = frame.split("\n");
      const type = lines.find(line=>line.startsWith("event: "));
      const value = lines.filter(line=>line.startsWith("data: ")).map(line=>line.slice(6)).join("\n");
      if (!type || !value) return;
      const event = type.slice(7), data = JSON.parse(value);
      if (event === "token") { thinking.remove(); answer.text += data.text; markdown(live.text,answer.text); scrollChat(); }
      if (event === "tool_start") { thinking.remove(); answer.calls.push(data); addTool(live.body,data,null); scrollChat(); }
      if (event === "tool_result") {
        for (const details of live.body.querySelectorAll(".tool-call")) if (details.dataset.call === data.id) {
          details.classList.add("finished"); details.querySelector("summary span:last-child").textContent = "已完成";
          details.querySelector("pre").textContent = data.text;
        }
        scrollChat();
      }
      if (event === "done" || event === "stopped") {
        terminal = true; S.messages = data.messages; renderMessages(S.messages);
        if (event === "stopped") toast("已停止生成。已开始的工具可能已经完成。");
      }
      if (event === "error") { terminal = true; throw new Error(data.message); }
    }
    try {
      while (true) {
        const chunk = await reader.read();
        buffer += decoder.decode(chunk.value || new Uint8Array(),{stream:!chunk.done});
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) { const frame = buffer.slice(0,boundary); buffer = buffer.slice(boundary+2); consume(frame); }
        if (chunk.done) break;
      }
      if (!terminal) throw new Error("连接中断，正在重新读取已保存的对话。");
    } finally { reader.releaseLock(); }
  } catch (error) {
    toast(error.message);
    try { const history = await api("/api/session?id=" + encodeURIComponent(S.thread)); S.messages = history.messages; }
    catch (_error) { S.messages = before; }
    renderMessages(S.messages);
    if (!S.messages.some(m=>m.role==="user"&&m.text===text)) $("message-input").value = text;
  } finally {
    S.streaming = false; setBusy(false);
    try { const data = await api("/api/state"); updateOverview(data); if (data.busy) setBusy(true); } catch (_error) {}
    $("message-input").focus();
  }
}
async function refreshMemories() {
  try {
    const data = await api("/api/memory?q=" + encodeURIComponent($("memory-search").value));
    $("memory-list").replaceChildren();
    if (!data.items.length) $("memory-list").append(el("p","empty-state","还没有相关记忆，写下第一条吧。"));
    for (const item of data.items) {
      const card = el("article","memory-card"), header = el("header"), button = el("button","icon-button");
      button.setAttribute("aria-label","删除记忆 " + item.id); button.append(icon("trash"));
      button.addEventListener("click",async() => {
        if (!confirm("删除这条记忆？")) return;
        try { await api("/api/memory",{action:"delete",id:item.id}); await refreshMemories(); updateOverview(await api("/api/state")); toast("记忆已删除"); }
        catch (error) { toast(error.message); }
      });
      header.append(el("span","","#" + item.id + " · " + item.created.replace("T"," ").slice(0,16)),button);
      card.append(header,el("p","",item.content)); if (item.tags) card.append(el("span","tags",item.tags));
      $("memory-list").append(card);
    }
  } catch (error) { toast(error.message); }
}
async function refreshFiles() {
  try { const data = await api("/api/files?path=" + encodeURIComponent($("directory-path").value)); $("directory-list").textContent = data.text; }
  catch (error) { toast(error.message); }
}
function showConnection(ok, text) {
  $("connection-result").textContent = text;
  $("connection-result").className = "connection-result " + (ok ? "ok" : "fail");
}
async function boot() {
  try {
    const data = await api("/api/state"); S.thread = data.active_thread; updateOverview(data); fillSettings(data);
    const history = await api("/api/session?id=" + encodeURIComponent(S.thread)); S.messages = history.messages; renderMessages(S.messages);
    setBusy(data.busy);
    if (!data.config.model.model) showSettings();
  } catch (error) { toast("界面加载失败：" + error.message); }
}
$("new-chat").addEventListener("click",newChat);
$("chat-form").addEventListener("submit",sendMessage);
$("message-input").addEventListener("keydown",event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("chat-form").requestSubmit(); } });
$("stop-button").addEventListener("click",async() => {
  try { await api("/api/stop",{}); $("stop-button").disabled = true; $("stop-button").textContent = "正在停止…"; toast("已请求停止，当前调用结束后生效。"); } catch (error) { toast(error.message); }
});
for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click",() => switchView(button.dataset.view));
for (const button of document.querySelectorAll("[data-prompt]")) button.addEventListener("click",() => { $("message-input").value = button.dataset.prompt; $("message-input").focus(); if (!S.data.config.model.model) showSettings(); });
$("settings-button").addEventListener("click",() => {
  if (innerWidth >= 1200) document.body.classList.toggle("settings-hidden");
  else document.body.classList.toggle("settings-open");
});
$("settings-close").addEventListener("click",() => { if (innerWidth >= 1200) document.body.classList.add("settings-hidden"); else document.body.classList.remove("settings-open"); });
$("setup-open").addEventListener("click",showSettings);
$("menu-button").addEventListener("click",() => document.body.classList.toggle("menu-open"));
$("mobile-backdrop").addEventListener("click",closePanels);
$("settings-form").addEventListener("input",markDirty);
$("settings-form").addEventListener("change",markDirty);
$("provider").addEventListener("change",() => {
  const preset = presets[$("provider").value];
  if (preset) { $("base-url").value = preset; $("model-name").value = ""; $("model-options").replaceChildren(); }
  if ($("provider").value === "ollama" && !S.data.config.model.api_key_configured && !$("api-key").value) $("api-key").value = "ollama";
  markDirty();
});
$("temperature").addEventListener("input",() => $("temperature-value").textContent = $("temperature").value);
$("clear-key").addEventListener("click",() => {
  S.clearKey = true; $("api-key").value = ""; $("api-key").placeholder = "保存后清除原密钥";
  $("key-state").textContent = "待清除"; markDirty();
});
$("api-key").addEventListener("input",() => { if ($("api-key").value) S.clearKey = false; });
$("settings-form").addEventListener("submit",async(event) => {
  event.preventDefault(); if (S.busy) return;
  const button = $("save-settings"); button.disabled = true;
  try {
    const data = await api("/api/config",settingsPayload()); S.dirty = false; updateOverview(data); fillSettings(data);
    $("saved-status").textContent = "已保存 · 下一次对话即时生效"; $("saved-status").className = "saved-status success";
    toast("设置已保存");
  } catch (error) { showConnection(false,error.message); }
  finally { button.disabled = false; }
});
$("discover-models").addEventListener("click",async() => {
  const button = $("discover-models"); button.disabled = true; button.textContent = "获取中…";
  try {
    const data = await api("/api/models",settingsPayload());
    $("model-options").replaceChildren();
    for (const name of data.models) { const option = el("option"); option.value = name; $("model-options").append(option); }
    $("models-note").textContent = data.detail;
    if (data.ok) { toast("已获取 " + data.models.length + " 个模型，点击模型输入框选择。"); $("model-name").focus(); }
    else showConnection(false,data.detail);
  } catch (error) { showConnection(false,error.message); }
  finally { button.disabled = false; button.textContent = "获取模型"; }
});
$("test-connection").addEventListener("click",async() => {
  const button = $("test-connection"); button.disabled = true; button.textContent = "测试中…";
  try { const data = await api("/api/test",settingsPayload()); showConnection(data.ok,data.detail); }
  catch (error) { showConnection(false,error.message); }
  finally { button.disabled = false; button.textContent = "测试连接"; }
});
$("memory-search").addEventListener("input",() => { clearTimeout(searchTimer); searchTimer = setTimeout(refreshMemories,220); });
$("memory-form").addEventListener("submit",async(event) => {
  event.preventDefault();
  try { await api("/api/memory",{action:"add",content:$("memory-content").value,tags:$("memory-tags").value}); $("memory-content").value = ""; $("memory-tags").value = ""; await refreshMemories(); updateOverview(await api("/api/state")); toast("记忆已保存"); }
  catch (error) { toast(error.message); }
});
$("directory-form").addEventListener("submit",event => { event.preventDefault(); refreshFiles(); });
$("read-file").addEventListener("click",async() => {
  try {
    const data = await api("/api/file?path=" + encodeURIComponent($("file-path").value));
    $("file-content").value = data.text; $("file-content").readOnly = data.truncated; $("save-file").disabled = data.truncated;
    $("file-note").textContent = data.truncated ? "文件较大，仅显示前 20000 字符，已关闭保存以防截断。" : "所有路径都位于 workspace 内。";
  } catch (error) { toast(error.message); }
});
$("file-path").addEventListener("input",() => {
  if ($("file-content").readOnly) $("file-content").value = "";
  $("file-content").readOnly = false; $("save-file").disabled = false;
});
$("save-file").addEventListener("click",async() => {
  try { const data = await api("/api/file",{path:$("file-path").value,text:$("file-content").value}); toast(data.detail); refreshFiles(); }
  catch (error) { toast(error.message); }
});
document.addEventListener("keydown",event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); newChat(); }
  if (event.key === "Escape") closePanels();
});
document.querySelector(".key-hint").textContent = navigator.platform.includes("Mac") ? "⌘ K" : "Ctrl K";
setInterval(async() => {
  if (!S.busy || S.streaming) return;
  try {
    const data = await api("/api/state"); updateOverview(data); setBusy(data.busy);
    if (!data.busy) { const history = await api("/api/session?id=" + encodeURIComponent(S.thread)); S.messages = history.messages; renderMessages(S.messages); }
  } catch (_error) {}
},3000);
boot();
