"use strict";

/* Developer controls share the daemon's configuration and execution records. */
(() => {
  const $ = (id) => document.getElementById(id);
  const dashboard = window.rmgDashboard;
  if (!dashboard) return;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  };
  const actionButton = (label, callback, className = "quiet") => {
    const item = node("button", label, className);
    item.type = "button";
    item.addEventListener("click", () => runButton(item, callback));
    return item;
  };
  const stringify = (value) => JSON.stringify(value, null, 2);
  const identifier = () => typeof crypto.randomUUID === "function" ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint8Array(24)), (v) => v.toString(16).padStart(2, "0")).join("");
  const controllerId = identifier();
  const controllerName = "developer:" + controllerId;
  let admin = null;
  let overview = null;
  let adminBusy = false;
  let currentView = "overview";
  let editingDevice = null;
  let deviceOriginal = {};
  let deviceRevision = null;
  let editingProfile = null;
  let profileRevision = null;
  let editingTemplate = null;
  let templateRevision = null;
  let pendingChange = null;
  let activeSession = null;
  let sessionRecord = null;
  let sessionOffset = 0;
  let sessionBusy = false;
  let sessionPollBusy = false;
  let pendingInput = null;
  let heartbeatBusy = false;
  let pendingOpen = null;
  const pendingProfileTests = new Map();
  const namedStates = {open: "已连接", disconnected: "连接断开", closed: "已关闭", unknown: "结果待确认", ready: "可用", missing: "缺少凭据", failed: "失败", succeeded: "已完成", developer_hold: "人工保留，等待明确归还", unclaimed: "无人控制", owned: "已取得控制权"};
  const stateLabel = (value) => namedStates[value] || value || "未确认";
  const timestamp = (value) => value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString("zh-CN", {hour12: false}) : "未记录";

  function message(id, value = "", error = false) {
    const target = $(id);
    target.textContent = value;
    target.hidden = !value;
    target.classList.toggle("error-message", error);
  }
  function errorText(error) {
    const details = error.details ? "\n" + stringify(error.details) : "";
    return (error.message || String(error)) + details;
  }
  async function runButton(button, callback) {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try { await callback(); }
    catch (error) { dashboard.notice(errorText(error)); }
    finally { button.disabled = false; button.removeAttribute("aria-busy"); }
  }
  function showResult(id, title, result) {
    $(id).replaceChildren(node("h3", title), node("pre", typeof result === "string" ? result : stringify(result)));
  }
  function appendChecks(container, checks) {
    const labels = {tool: "本地程序", skill: "Claude Code Skill", daemon: "本地管理器", connection: "认证与连接", posix_shell: "POSIX Shell", job_dependencies: "持久作业运行条件", helper: "远端 Helper", helper_storage: "Helper 存储", helper_directory: "Helper 目录", directory: "部署目录", sftp: "SSH 文件通道", scp: "旧版 SCP", shell_probe: "Shell 能力探测"};
    const states = {pass: "通过", fail: "失败", warning: "需要处理", not_checked: "未检查"};
    for (const check of checks || []) {
      const row = node("div", null, "diagnostic-row");
      const top = node("div", null, "row-top");
      const label = labels[check.name] || (check.name?.startsWith("credential_") ? "环境变量凭据" : check.name?.startsWith("temporary_credential_") ? "临时凭据" : check.name?.startsWith("key_file_") ? "私钥文件" : check.name);
      top.append(node("strong", label || "检查"), node("span", states[check.state] || stateLabel(check.state), "badge " + (check.state === "pass" ? "good" : check.state === "fail" ? "bad" : "warn")));
      row.append(top, node("p", check.message || "", "small"));
      if (check.diagnostic?.stage) {
        const stages = {dns: "解析地址", tcp: "连接端口", proxy: "代理连接", jump: "跳板连接", host_key: "核对主机身份", authentication: "用户认证", login: "交互登录", login_flow: "交互登录", shell: "确认 Shell", sftp: "建立文件通道"};
        row.append(node("p", "失败阶段 · " + (stages[check.diagnostic.stage] || check.diagnostic.stage), "advice"));
      }
      if (check.advice) row.append(node("p", check.advice, "small"));
      container.append(row);
    }
  }
  function showDiagnostics(result, name) {
    const container = $("device-action-result");
    container.replaceChildren(node("h3", "设备检查 · " + name));
    container.append(node("p", `${result.connection?.connected ? "连接已验证" : "连接尚未验证"} · ${result.job_ready ? "持久作业已就绪" : "持久作业未就绪或未检查"} · ${timestamp(result.checked_at)}`, "small"));
    appendChecks(container, result.checks);
    const details = node("details"); details.append(node("summary", "完整检查证据"), node("pre", stringify(result))); container.append(details);
  }
  function fillSelect(id, values, placeholder) {
    const select = $(id);
    const before = select.value;
    select.replaceChildren();
    if (placeholder !== undefined) {
      const option = node("option", placeholder); option.value = ""; select.append(option);
    }
    for (const item of values) {
      const option = node("option", typeof item === "string" ? item : item.label);
      option.value = typeof item === "string" ? item : item.value;
      select.append(option);
    }
    if ([...select.options].some((item) => item.value === before)) select.value = before;
    select.disabled = !values.length && placeholder === undefined;
  }
  function targetName() {
    const value = $("manage-target").value;
    if (!value) throw new Error("请先添加并保存一台设备。");
    return value;
  }
  function navigate(view) {
    if (!["overview", "devices", "tasks", "sessions", "settings"].includes(view)) return;
    if (currentView === "sessions" && view !== "sessions") abandonSession();
    currentView = view;
    for (const page of document.querySelectorAll("[data-page]")) page.hidden = page.dataset.page !== view;
    for (const tab of document.querySelectorAll("[data-view]")) {
      if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    const headings = {overview: ["掌握环境，也掌握执行。", "配置设备、检查过程、接管调试。你和 Agent 使用同一份真实状态。"], devices: ["让设备接入清楚可控。", "在这里管理连接、认证和远端 Helper；每次远端操作都由你发起。"], tasks: ["看清每个步骤的依据。", "查看实际命令、执行证据和原任务结果。未知状态优先查询，不重复部署。"], sessions: ["需要时，亲自接过终端。", "观察、接管、调试，再明确归还控制权。人工与 Agent 不会同时输入。"], settings: ["把环境配置握在手里。", "管理交互模板、常用操作、临时凭据和备份。离线安装保持离线更新。"]};
    $("page-heading").textContent = headings[view][0];
    $("page-description").textContent = headings[view][1];
    if (["devices", "settings", "sessions"].includes(view)) refreshAdmin();
    if (view === "sessions") renderSessionList();
  }
  for (const tab of document.querySelectorAll("[data-view]")) tab.addEventListener("click", () => navigate(tab.dataset.view));
  document.addEventListener("rmg:navigate", (event) => navigate(event.detail));
  document.addEventListener("rmg:edit-target", async (event) => {
    navigate("devices"); await refreshAdmin();
    const item = admin?.targets?.find((target) => target.name === event.detail);
    if (item) editDevice(item);
  });
  document.addEventListener("rmg:overview", (event) => {
    overview = event.detail;
    renderSessionList();
    if (activeSession && currentView === "sessions") pollSession();
  });

  async function adminCall(action, params = {}) {
    return dashboard.api("admin/" + action, "POST", params);
  }
  async function refreshAdmin() {
    if (adminBusy || !dashboard.hasToken()) return;
    adminBusy = true;
    try {
      admin = await dashboard.api("admin");
      renderDevices(); renderProfiles(); renderTemplates(); renderCredentials(); renderDiagnostics();
      const names = (admin.targets || []).map((item) => item.name);
      fillSelect("manage-target", names);
      fillSelect("session-target", names);
      fillSelect("session-profile", (admin.profiles || []).map((item) => item.name), "普通终端");
      if (document.activeElement !== $("settings-log-limit") && document.activeElement !== $("settings-timeout")) {
        $("settings-log-limit").value = (admin.settings?.local_log_max_bytes || 67108864) / 1048576;
        $("settings-timeout").value = admin.settings?.default_timeout ?? 30;
      }
    } catch (error) { dashboard.notice(errorText(error)); }
    finally { adminBusy = false; }
  }

  async function previewChange(action, params, title, onApplied) {
    const preview = await adminCall(action, {...params, dry_run: true});
    const revision = preview.revision || params.expected_revision;
    pendingChange = {action, params: {...params, expected_revision: revision, dry_run: false}, onApplied};
    $("change-title").textContent = title;
    $("change-description").textContent = action === "template.run" ? "下面是即将提交的实际命令和目标。确认后开始执行；相同请求 ID 不会重复发送。" : "确认下面的变更后应用。其他入口修改了配置时，本次操作会被阻止，需要重新读取并预览。";
    $("change-preview").textContent = stringify(preview);
    message("change-error");
    $("change-apply").disabled = Boolean(preview.blockers?.length);
    $("change-apply").textContent = action === "template.run" ? "确认执行一次" : "确认应用";
    if (!$("change-dialog").open) $("change-dialog").showModal();
  }
  function closeChange() { $("change-dialog").close(); pendingChange = null; }
  $("change-cancel").addEventListener("click", closeChange);
  $("change-dismiss").addEventListener("click", closeChange);
  $("change-dialog").addEventListener("cancel", () => { pendingChange = null; });
  $("change-apply").addEventListener("click", async () => {
    if (!pendingChange || $("change-apply").disabled) return;
    $("change-apply").disabled = true;
    const change = pendingChange;
    try {
      const result = await adminCall(change.action, change.params);
      closeChange();
      await refreshAdmin(); await dashboard.refreshOverview();
      if (change.onApplied) await change.onApplied(result);
      else dashboard.notice("变更已应用，配置与 Agent 共用。");
    } catch (error) {
      message("change-error", errorText(error) + "\n请关闭预览并刷新状态后核对。本次不会自动重试。", true);
      pendingChange = null;
    }
  });

  function renderDevices() {
    const list = $("device-list"); list.replaceChildren();
    if (!admin.targets?.length) list.append(node("p", "还没有设备。点击“添加设备”，配置你的第一台 SSH 或 Telnet 目标。", "empty"));
    for (const item of admin.targets || []) {
      const card = node("article", null, "target-card");
      const top = node("div", null, "row-top");
      top.append(node("h3", item.name), node("span", item.config.protocol.toUpperCase(), "badge"));
      card.append(top, node("p", `${item.config.username ? item.config.username + "@" : ""}${item.config.host}:${item.config.port || (item.config.protocol === "ssh" ? 22 : 23)}`, "device-address"));
      const auth = item.authentication || {};
      card.append(node("p", "认证引用 · " + stateLabel(auth.state) + (auth.default_ssh_authentication ? " · 系统 SSH 认证需实际连接验证" : ""), auth.state === "missing" ? "advice" : "small"));
      for (const ref of auth.references || []) card.append(node("p", `${ref.field} · ${ref.reference} · ${ref.available ? "管理器可取得" : "当前不可用"}`, "small"));
      const actions = node("div", null, "actions");
      actions.append(actionButton("编辑", () => editDevice(item)), actionButton("复制", () => editDevice(item, true)), actionButton("诊断 / Helper", () => {
        $("manage-target").value = item.name; $("device-action-result").scrollIntoView({behavior: "smooth", block: "center"});
      }), actionButton("删除", () => previewChange("target.delete", {name: item.name, expected_revision: admin.revision}, "删除设备 " + item.name), "danger"));
      card.append(actions); list.append(card);
    }
  }
  const deviceFields = new Set(["protocol", "host", "port", "username", "password_ref", "password_env", "client_keys", "passphrase_ref", "encoding", "connect_timeout", "shell", "helper_dir", "transfer"]);
  function editDevice(item = null, copy = false) {
    const config = item?.config || {protocol: "ssh", encoding: "utf-8", connect_timeout: 15, shell: "posix", helper_dir: "~/.local/share/remote-mng"};
    deviceOriginal = structuredClone(config);
    editingDevice = copy ? null : item?.name || null;
    deviceRevision = admin?.revision;
    $("device-name").value = copy ? item.name + "-copy" : item?.name || "";
    $("device-name").disabled = Boolean(editingDevice);
    $("device-protocol").value = config.protocol || "ssh";
    $("device-host").value = config.host || "";
    $("device-port").value = config.port || "";
    $("device-username").value = config.username || "";
    $("device-auth").value = config.password_ref ? "memory" : config.password_env ? "environment" : config.client_keys?.length ? "key" : "agent";
    $("device-keys").value = (config.client_keys || []).join("\n");
    $("device-credential").value = config.password_ref || config.password_env || "";
    $("device-passphrase").value = config.passphrase_ref || "";
    $("device-encoding").value = config.encoding || "utf-8";
    $("device-timeout").value = config.connect_timeout || 15;
    $("device-shell").value = config.shell || (config.protocol === "ssh" ? "posix" : "unknown");
    $("device-helper").value = config.helper_dir || "~/.local/share/remote-mng";
    $("device-transfer-host").value = config.transfer?.host || "";
    $("device-transfer-user").value = config.transfer?.username || "";
    $("device-transfer-port").value = config.transfer?.port || "";
    $("device-transfer-ref").value = config.transfer?.password_ref || "";
    $("device-advanced").value = stringify(Object.fromEntries(Object.entries(config).filter(([key]) => !deviceFields.has(key))));
    $("telnet-login-mode").value = config.login_flow || config.login_steps?.length ? "advanced" : "none";
    $("telnet-user-prompt").value = "(?i)(login|username):\\s*$";
    $("telnet-password-prompt").value = "(?i)password:\\s*$";
    $("device-editor-title").textContent = editingDevice ? "编辑设备 · " + editingDevice : copy ? "复制设备" : "添加设备";
    $("device-editor-panel").hidden = false;
    message("device-error"); updateAuthFields();
    $("device-editor-panel").scrollIntoView({behavior: "smooth", block: "start"});
    $(editingDevice ? "device-host" : "device-name").focus({preventScroll: true});
  }
  function updateAuthFields() {
    const method = $("device-auth").value;
    $("telnet-login-fields").hidden = $("device-protocol").value !== "telnet";
    $("device-keys-field").hidden = method !== "key";
    $("device-credential-field").hidden = !["memory", "environment"].includes(method);
    $("device-credential").placeholder = method === "memory" ? "在设置中创建的凭据引用" : "管理器进程可读取的环境变量名";
    $("device-auth-advice").textContent = $("device-protocol").value === "telnet" ? "Telnet 明文传输。复杂登录请配置高级 login_steps，密码用 send_ref 引用临时凭据；传包可使用独立 SSH 通道。" : "认证引用可用不代表登录成功。临时凭据在设置中创建，管理器重启后失效。";
  }
  $("add-device").addEventListener("click", () => editDevice());
  $("close-device-editor").addEventListener("click", () => { $("device-editor-panel").hidden = true; });
  $("reset-device").addEventListener("click", () => { $("device-editor-panel").hidden = true; });
  $("device-auth").addEventListener("change", updateAuthFields);
  $("device-protocol").addEventListener("change", () => { $("device-shell").value = $("device-protocol").value === "ssh" ? "posix" : "unknown"; updateAuthFields(); });
  $("refresh-admin").addEventListener("click", refreshAdmin);
  $("device-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.submitter; if (submit.disabled) return; submit.disabled = true;
    try {
      const extra = JSON.parse($("device-advanced").value || "{}");
      if (!extra || Array.isArray(extra) || typeof extra !== "object") throw new Error("高级配置必须是 JSON 对象。");
      const config = {...extra, protocol: $("device-protocol").value, host: $("device-host").value.trim(), encoding: $("device-encoding").value.trim(), connect_timeout: Number($("device-timeout").value), shell: $("device-shell").value, helper_dir: $("device-helper").value.trim()};
      if ($("device-port").value) config.port = Number($("device-port").value);
      if ($("device-username").value.trim()) config.username = $("device-username").value.trim();
      const auth = $("device-auth").value;
      if (auth === "key") config.client_keys = $("device-keys").value.split("\n").map((s) => s.trim()).filter(Boolean);
      if (auth === "memory" || auth === "environment") {
        if (!$("device-credential").value.trim()) throw new Error("请填写密码引用，或选择其他认证方式。");
        config[auth === "memory" ? "password_ref" : "password_env"] = $("device-credential").value.trim();
      }
      if ($("device-passphrase").value.trim()) config.passphrase_ref = $("device-passphrase").value.trim();
      if (config.protocol === "telnet" && $("telnet-login-mode").value !== "advanced") {
        delete config.login_flow; config.login_steps = [];
        const mode = $("telnet-login-mode").value;
        if (mode === "user-password") {
          if (!config.username) throw new Error("用户名与密码登录需要填写用户名。");
          config.login_steps.push({expect: $("telnet-user-prompt").value, send: config.username, newline: true});
        }
        if (mode === "password" || mode === "user-password") {
          if (!config.password_ref && !config.password_env) throw new Error("Telnet 自动登录需要选择临时凭据或环境变量认证，并填写密码引用。");
          config.login_steps.push({expect: $("telnet-password-prompt").value, [config.password_ref ? "send_ref" : "send_env"]: config.password_ref || config.password_env, newline: true});
        }
      }
      if ($("device-transfer-host").value.trim()) {
        config.transfer = {...deviceOriginal.transfer, host: $("device-transfer-host").value.trim()};
        for (const field of ["username", "port", "password_ref"]) delete config.transfer[field];
        if ($("device-transfer-user").value.trim()) config.transfer.username = $("device-transfer-user").value.trim();
        if ($("device-transfer-port").value) config.transfer.port = Number($("device-transfer-port").value);
        if ($("device-transfer-ref").value.trim()) config.transfer.password_ref = $("device-transfer-ref").value.trim();
      } else if (deviceOriginal.transfer && !deviceOriginal.transfer.host) config.transfer = deviceOriginal.transfer;
      const name = $("device-name").value.trim();
      const params = {name, expected_revision: deviceRevision};
      if (editingDevice) params.patch = Object.fromEntries([...new Set([...Object.keys(deviceOriginal), ...Object.keys(config)])].map((key) => [key, key in config ? config[key] : null]));
      else params.config = config;
      await previewChange(editingDevice ? "target.patch" : "target.create", params, editingDevice ? "更新设备 " + name : "添加设备 " + name, () => {
        $("device-editor-panel").hidden = true; $("manage-target").value = name;
        dashboard.notice("设备配置已保存。可在下方点击“测试连接”，单独验证远端可达性与认证。");
      });
      message("device-error");
    } catch (error) { message("device-error", errorText(error), true); }
    finally { submit.disabled = false; }
  });

  $("diagnose-target").addEventListener("click", (event) => runButton(event.currentTarget, async () => {
    const name = targetName();
    try { showResult("device-action-result", "正在测试 " + name, "正在连接所选设备…"); const result = await dashboard.api("targets/" + encodeURIComponent(name) + "/check", "POST"); showDiagnostics(result, name); }
    catch (error) { showResult("device-action-result", "连接诊断 · " + name, errorText(error)); }
    await dashboard.refreshOverview();
  }));
  $("probe-hostkey").addEventListener("click", (event) => runButton(event.currentTarget, async () => {
    const name = targetName();
    const probe = await adminCall("hostkey.probe", {name, route: $("hostkey-endpoint").value});
    showResult("device-action-result", "核对主机身份 · " + name, probe);
    const help = node("p", "从服务器控制台、管理员或已核实的设备记录获取指纹，与下方值逐字核对。读取到的网络公钥本身不能证明服务器身份。", "advice");
    const label = node("label", "输入从可信来源核对的指纹");
    const input = node("input"); input.autocomplete = "off"; input.spellcheck = false; input.placeholder = "SHA256:…"; label.append(input);
    const replaceLabel = node("label", null, "checkbox"); const replace = node("input"); replace.type = "checkbox";
    replaceLabel.append(replace, document.createTextNode("确认替换此端点已保存的旧主机公钥（仅在核实密钥变更后勾选）"));
    const trust = actionButton("预览保存主机身份", async () => {
      if (input.value.trim() !== probe.fingerprint) throw new Error("输入指纹与探测值不一致。请先核对可信来源。");
      await previewChange("hostkey.trust", {probe_id: probe.probe_id, fingerprint: input.value.trim(), expected_revision: admin.revision, confirmed: true, replace: replace.checked}, "保存已核实的主机身份");
    });
    $("device-action-result").append(help, label, replaceLabel, trust);
  }));
  $("helper-health").addEventListener("click", (event) => runButton(event.currentTarget, async () => {
    const name = targetName(); const result = await dashboard.api("targets/" + encodeURIComponent(name) + "/health", "POST"); showResult("device-action-result", "Helper 存储 · " + name, result); await dashboard.refreshOverview();
  }));
  $("helper-install").addEventListener("click", (event) => runButton(event.currentTarget, async () => {
    const name = targetName();
    if (!confirm(`向 ${name} 安装或更新随本工具提供的 Helper？会写入该设备配置的 Helper 目录。`)) return;
    showResult("device-action-result", "正在安装 Helper · " + name, "远端操作正在执行，请勿重复提交。");
    const result = await dashboard.api("targets/" + encodeURIComponent(name) + "/helper/install", "POST");
    showResult("device-action-result", "Helper 安装结果 · " + name, result); await dashboard.refreshOverview();
  }));
  $("helper-cleanup").addEventListener("click", (event) => runButton(event.currentTarget, async () => {
    const name = targetName(); const path = "targets/" + encodeURIComponent(name) + "/helper/cleanup";
    const jobIds = $("helper-job-ids").value.split(/[\s,]+/).filter(Boolean);
    if (!jobIds.length || jobIds.length > 100) throw new Error("请填写 1 到 100 个原作业 ID。也可以到对应任务详情直接预览日志清理。");
    const plan = await dashboard.api(path, "POST", {job_ids: jobIds, apply: false});
    showResult("device-action-result", "Helper 日志清理预览 · " + name, plan);
    const apply = actionButton("按此预览清理日志", async () => {
      if (!confirm("日志清理不可恢复。确认按这份预览清理已结束作业的日志？作业 ID 和退出结果会保留。")) return;
      const result = await dashboard.api(path, "POST", {job_ids: jobIds, apply: true, expected_plan: plan.plan_id});
      showResult("device-action-result", "Helper 清理结果", result);
    }, "danger");
    apply.disabled = !(plan.jobs || []).length || (plan.jobs || []).some((item) => !item.eligible);
    $("device-action-result").append(apply);
  }));

  function renderProfiles() {
    const list = $("profile-list"); list.replaceChildren();
    if (!admin.profiles?.length) list.append(node("p", "暂无交互模板。普通终端无需模板；专用前台可以配置进入和退出规则。", "empty"));
    for (const item of admin.profiles || []) {
      const card = node("article", null, "target-card");
      card.append(node("h3", item.name), node("p", item.config.enter || "直接观察默认终端", "monospace"), node("p", "提示符 · " + (item.config.prompt || "未设置，无法验证已进入"), "small"));
      const target = node("select"); target.setAttribute("aria-label", item.name + " 的测试设备");
      for (const device of admin.targets || []) { const option = node("option", device.name); option.value = device.name; target.append(option); }
      const actions = node("div", null, "actions");
      const finishTest = actionButton("核对后结束上次测试请求", () => {
        if (!confirm("结束此页对上次模板测试的重取？这不会关闭会话或撤销进入命令。请先核对会话与活动记录，再发起新的测试。")) return;
        pendingProfileTests.delete(item.name); test.textContent = "测试并打开会话"; finishTest.hidden = true;
      });
      finishTest.hidden = !pendingProfileTests.has(item.name);
      const test = actionButton(pendingProfileTests.has(item.name) ? "重取上次测试结果" : "测试并打开会话", async () => {
        if (!target.value) throw new Error("请先添加设备。");
        if (!pendingProfileTests.has(item.name)) {
          if (!confirm(`在 ${target.value} 打开会话并发送模板 ${item.name} 的进入命令？测试后会话保留，可手动退出或关闭。`)) return;
          pendingProfileTests.set(item.name, {target: target.value, request_id: identifier()});
        }
        finishTest.hidden = false;
        test.textContent = "重取上次测试结果";
        const result = await sessionApi("profiles/" + encodeURIComponent(item.name) + "/test", pendingProfileTests.get(item.name));
        if (!result.id) throw new Error("模板测试的开启结果仍待确认。请继续重取原请求，并检查会话列表。记录：" + (result.open_request_id || result.request_id));
        pendingProfileTests.delete(item.name); test.textContent = "测试并打开会话"; finishTest.hidden = true;
        await dashboard.refreshOverview(); navigate("sessions"); await selectSession(result.id);
      });
      test.disabled = !(admin.targets || []).length;
      actions.append(actionButton("编辑", () => editProfile(item)), target, test, finishTest, actionButton("删除", () => previewChange("profile.delete", {name: item.name, expected_revision: admin.revision}, "删除交互模板 " + item.name), "danger"));
      card.append(actions); list.append(card);
    }
  }
  function editProfile(item = null) {
    editingProfile = item?.name || null; profileRevision = admin?.revision;
    const config = item?.config || {};
    $("profile-name").value = item?.name || ""; $("profile-name").disabled = Boolean(item);
    for (const [field, key] of [["label", "name"], ["enter", "enter"], ["prompt", "prompt"], ["exit", "exit"], ["exit-prompt", "exit_prompt"]]) $("profile-" + field).value = config[key] || "";
    $("profile-timeout").value = config.timeout || 15;
    $("profile-form").hidden = false; message("profile-error"); $("profile-form").scrollIntoView({behavior: "smooth"});
  }
  $("add-profile").addEventListener("click", () => editProfile());
  $("close-profile").addEventListener("click", () => { $("profile-form").hidden = true; });
  $("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const submit = event.submitter; if (submit.disabled) return; submit.disabled = true;
    try {
      const config = {timeout: Number($("profile-timeout").value)};
      for (const [field, key] of [["label", "name"], ["enter", "enter"], ["prompt", "prompt"], ["exit", "exit"], ["exit-prompt", "exit_prompt"]]) if ($("profile-" + field).value) config[key] = $("profile-" + field).value;
      // Validation is server-side because these patterns use Python, not JavaScript, regex syntax.
      const name = $("profile-name").value.trim();
      const params = {name, expected_revision: profileRevision};
      if (editingProfile) params.patch = Object.fromEntries(["name", "enter", "prompt", "exit", "exit_prompt", "timeout"].map((key) => [key, config[key] ?? null]));
      else params.config = config;
      await previewChange(editingProfile ? "profile.patch" : "profile.create", params, "保存交互模板 " + name, () => { $("profile-form").hidden = true; dashboard.notice("交互模板已保存。测试将真实打开远端会话，需要单独发起。"); });
      message("profile-error");
    } catch (error) { message("profile-error", errorText(error), true); }
    finally { submit.disabled = false; }
  });

  function renderTemplates() {
    const list = $("template-list"); list.replaceChildren();
    if (!admin.operation_templates?.length) list.append(node("p", "保存常用诊断或测试命令后，可以选择设备、核对计划并执行。", "empty"));
    for (const item of admin.operation_templates || []) {
      const card = node("article", null, "target-card");
      card.append(node("h3", item.name), node("p", item.config.description || (item.config.kind === "job" ? "持久作业" : "短命令"), "small"), node("pre", item.config.command));
      const target = node("select"); target.setAttribute("aria-label", item.name + " 的执行设备");
      for (const device of admin.targets || []) { const option = node("option", device.name); option.value = device.name; target.append(option); }
      const run = actionButton("预览并运行", async () => {
        if (!target.value) throw new Error("请先配置设备。");
        await previewChange("template.run", {name: item.name, target: target.value, expected_revision: admin.revision, request_id: identifier()}, "运行常用操作 " + item.name, async (result) => {
          showResult("config-result", "操作提交结果", result); navigate("tasks");
          const taskId = result.task?.id || result.task_id || result.id;
          if (taskId && dashboard.selectTask) await dashboard.selectTask(taskId);
          dashboard.notice("操作已提交。请在任务列表核对实际输出与结果；提交记录不代表业务验证通过。");
        });
      });
      run.disabled = !(admin.targets || []).length;
      const actions = node("div", null, "actions"); actions.append(actionButton("编辑", () => editTemplate(item)), target, run, actionButton("删除", () => previewChange("template.delete", {name: item.name, expected_revision: admin.revision}, "删除常用操作 " + item.name), "danger"));
      card.append(actions); list.append(card);
    }
  }
  function editTemplate(item = null) {
    editingTemplate = item?.name || null; templateRevision = admin?.revision;
    const config = item?.config || {};
    $("template-name").value = item?.name || ""; $("template-name").disabled = Boolean(item);
    $("template-kind").value = config.kind || "job";
    $("template-timeout").disabled = $("template-kind").value === "job";
    for (const key of ["description", "command", "cwd", "timeout"]) $("template-" + key).value = config[key] ?? "";
    $("template-form").hidden = false; message("template-error"); $("template-form").scrollIntoView({behavior: "smooth"});
  }
  $("add-template").addEventListener("click", () => editTemplate());
  $("template-kind").addEventListener("change", () => { $("template-timeout").disabled = $("template-kind").value === "job"; });
  $("close-template").addEventListener("click", () => { $("template-form").hidden = true; });
  $("template-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const submit = event.submitter; if (submit.disabled) return; submit.disabled = true;
    try {
      const config = {kind: $("template-kind").value, command: $("template-command").value, description: $("template-description").value};
      if ($("template-cwd").value) config.cwd = $("template-cwd").value;
      if (config.kind === "exec" && $("template-timeout").value !== "") config.timeout = Number($("template-timeout").value);
      const params = {name: $("template-name").value.trim(), expected_revision: templateRevision};
      if (editingTemplate) params.patch = {...config, cwd: config.cwd ?? null, timeout: config.timeout ?? null}; else params.config = config;
      await previewChange(editingTemplate ? "template.patch" : "template.create", params, "保存常用操作", () => { $("template-form").hidden = true; dashboard.notice("常用操作已保存。运行前仍需选择目标并核对执行计划。"); });
    } catch (error) { message("template-error", errorText(error), true); }
    finally { submit.disabled = false; }
  });

  function renderCredentials() {
    const list = $("credential-list"); list.replaceChildren();
    for (const item of admin.credentials || []) {
      const row = node("div", null, "activity-row");
      const info = node("div"); info.append(node("strong", item.reference || item.name), node("p", "管理器内存 · 重启失效 · " + (item.available === false ? "不可用" : "可用")));
      row.append(info, actionButton("删除", async () => {
        if (!confirm(`删除临时凭据 ${item.name}？引用它的设备可能无法建立新连接。`)) return;
        await adminCall("credential.delete", {name: item.name}); await refreshAdmin();
      }, "danger")); list.append(row);
    }
    if (!admin.credentials?.length) list.append(node("p", "没有保存临时凭据。你也可以使用现有 SSH Agent、私钥文件或管理器环境变量。", "small"));
  }
  $("credential-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const submit = event.submitter; if (submit.disabled) return; submit.disabled = true;
    const value = $("credential-value").value; $("credential-value").value = "";
    try {
      const result = await adminCall("credential.set", {name: $("credential-name").value.trim(), value});
      message("credential-result", "已保存到管理器内存。设备配置中使用引用：" + (result.reference || result.name) + "。管理器重启后需重新输入。");
      await refreshAdmin();
    } catch (error) { message("credential-result", errorText(error) + "\n输入框已清空。", true); }
    finally { submit.disabled = false; }
  });
  function renderDiagnostics() {
    const container = $("local-diagnostics"); container.replaceChildren();
    const paths = node("dl", null, "facts");
    for (const [key, label] of [["home", "数据目录"], ["config", "配置文件"], ["backups", "备份目录"], ["logs", "日志目录"]]) if (admin.paths?.[key]) paths.append(node("dt", label), node("dd", admin.paths[key]));
    container.append(paths);
    const effective = admin.settings_effective;
    message("settings-effective-note", effective && effective.log_limit_source !== "settings" ? `当前实际日志上限为 ${(effective.local_log_max_bytes / 1048576).toFixed(2)} MiB，由管理器启动环境 ${effective.log_limit_source === "RMG_MAX_LOG_BYTES" ? "RMG_MAX_LOG_BYTES" : "中的无效 RMG_MAX_LOG_BYTES（已回退）"} 决定。保存配置不会覆盖该启动环境。` : "", false);
    appendChecks(container, admin.diagnostics?.checks);
    const details = node("details"); details.append(node("summary", "本地诊断与 Skill 就绪状态"), node("pre", stringify(admin.diagnostics || {})));
    container.append(details, node("p", "本地配置版本 · " + (admin.revision || "未记录"), "small monospace"));
  }
  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const submit = event.submitter; if (submit.disabled) return; submit.disabled = true;
    try {
      await previewChange("settings.patch", {patch: {local_log_max_bytes: Math.round(Number($("settings-log-limit").value) * 1048576), default_timeout: Number($("settings-timeout").value)}, expected_revision: admin.revision}, "更新本地设置");
      message("settings-error");
    } catch (error) { message("settings-error", errorText(error), true); }
    finally { submit.disabled = false; }
  });
  $("export-config").addEventListener("click", (event) => runButton(event.currentTarget, async () => {
    const result = await adminCall("config.export");
    const url = URL.createObjectURL(new Blob([stringify(result)], {type: "application/json"}));
    const link = node("a"); link.href = url; link.download = "remote-mng-config-" + new Date().toISOString().slice(0, 10) + ".json"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    showResult("config-result", "配置已导出", "导出包含设备、模板、设置和凭据引用，不含临时凭据值。请按配置的敏感程度保管文件。");
  }));
  $("import-config").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", async () => {
    try {
      const file = $("import-file").files[0]; if (!file) return;
      if (file.size > 1048576) throw new Error("配置文件过大，请选择 1 MiB 以内的 JSON 文件。");
      const config = JSON.parse(await file.text());
      await previewChange("config.import", {config, expected_revision: admin.revision}, "导入配置 · " + file.name);
    } catch (error) { showResult("config-result", "配置导入失败", errorText(error)); }
    finally { $("import-file").value = ""; }
  });
  $("list-backups").addEventListener("click", (event) => runButton(event.currentTarget, async () => {
    const result = await adminCall("config.backups");
    const records = Array.isArray(result) ? result : result.backups || [];
    $("backup-list").replaceChildren(node("h3", "配置备份"));
    if (!records.length) $("backup-list").append(node("p", "暂无备份。应用配置变更时会自动备份。", "small"));
    for (const item of records) {
      const row = node("div", null, "activity-row");
      row.append(node("span", `${item.name} · ${timestamp(item.created_at)} · ${item.bytes ?? "—"} B`, "small"), actionButton("预览恢复", () => previewChange("config.restore", {backup: item.name, expected_revision: admin.revision}, "恢复配置备份")));
      $("backup-list").append(row);
    }
  }));

  async function sessionApi(path, body, keepalive = false) {
    return dashboard.api(path, body === undefined ? "GET" : "POST", body || {}, {"X-RMG-Controller": controllerId}, keepalive);
  }
  function ownsSession(record = sessionRecord) {
    return Boolean(record && record.state === "open" && (record.controller === controllerName || record.controller === controllerId || record.control_state === "owned" && record.owned_by_requester === true));
  }
  function renderSessionList() {
    const list = $("session-list"); list.replaceChildren();
    const sessions = overview?.sessions || [];
    if (!sessions.length) list.append(node("p", "暂无会话。在上方选择设备并新建，或观察 Agent 已打开的会话。", "empty"));
    for (const item of sessions.slice(0, 100)) {
      const button = actionButton("", () => selectSession(item.id), "task-row" + (activeSession === item.id ? " selected" : ""));
      button.setAttribute("aria-pressed", String(activeSession === item.id));
      button.append(node("strong", item.target || "终端会话"), node("p", `${stateLabel(item.state)} · ${item.state_label || "默认终端"}`, "small"), node("p", item.id, "small monospace"));
      list.append(button);
    }
  }
  async function selectSession(id) {
    if (activeSession !== id) {
      abandonSession(); activeSession = id; sessionRecord = null; sessionOffset = 0; pendingInput = null;
      $("session-output").textContent = ""; $("session-input").value = ""; $("session-sensitive").checked = false; $("session-retry").hidden = true;
    }
    renderSessionList(); await pollSession();
  }
  function renderSession() {
    const record = sessionRecord;
    const open = record?.state === "open";
    const owns = ownsSession(record);
    $("session-title").textContent = record ? `${record.target || "终端"} · ${record.id}` : "会话详情";
    $("session-status").textContent = record ? `${stateLabel(record.state)} · 当前环境 ${record.state_label || "未知"}${record.control_expires_at ? " · 控制权有效至 " + timestamp(record.control_expires_at) : ""}` : "选择一个会话查看输出。";
    $("session-owner").textContent = owns ? "由此页面控制" : record?.control_state === "developer_hold" ? "人工保留" : record?.controller === "agent" ? "Agent 控制" : record?.controller ? "其他页面控制" : "仅观察";
    $("session-claim").disabled = !open || owns || sessionBusy;
    $("session-release").disabled = !open || !owns || sessionBusy;
    for (const id of ["session-interrupt", "session-leave", "session-close", "session-input", "session-send", "session-resize"]) $(id).disabled = !open || !owns || sessionBusy || (id === "session-send" && Boolean(pendingInput));
    $("session-retry").hidden = !pendingInput;
    $("session-retry").disabled = !owns || sessionBusy;
  }
  async function pollSession() {
    if (!activeSession || sessionPollBusy || !dashboard.hasToken()) return;
    sessionPollBusy = true;
    const id = activeSession;
    try {
      const record = await sessionApi("sessions/" + encodeURIComponent(id));
      if (id !== activeSession) return;
      sessionRecord = record; renderSession();
      const query = new URLSearchParams({kind: "session", id, offset: String(sessionOffset), limit: "32768"});
      const log = await dashboard.api("logs?" + query.toString());
      if (id !== activeSession) return;
      let text = log.data || "";
      if (log.gap || typeof log.offset === "number" && log.offset > sessionOffset) text = `\n[日志已轮转，可用输出从 ${log.offset ?? "较新位置"} 开始。]\n` + text;
      const output = $("session-output"); const nearEnd = output.scrollHeight - output.scrollTop - output.clientHeight < 80;
      output.textContent += text;
      if (output.textContent.length > 600000) output.textContent = "[页面仅显示最近 600000 个字符]\n" + output.textContent.slice(-600000);
      sessionOffset = log.next_offset ?? sessionOffset;
      if (nearEnd) output.scrollTop = output.scrollHeight;
    } catch (error) { if (id === activeSession) message("session-feedback", errorText(error), true); }
    finally { sessionPollBusy = false; }
  }
  async function performSession(action, params = {}) {
    if (!activeSession || sessionBusy) return;
    sessionBusy = true; renderSession();
    const id = activeSession;
    try {
      const result = await sessionApi("sessions/" + encodeURIComponent(id) + "/" + action, params);
      if (id === activeSession) {
        if (action === "release") sessionRecord = {...sessionRecord, controller: null, control_state: "unclaimed"};
        const explanation = {claim: "已接管当前会话。Agent 的旧控制权失效；操作完成后请明确归还。", release: "已归还控制权。Agent 需要读取新输出并重新获取控制权，工具不会自动重发命令。", interrupt: "已发送 Ctrl+C。远端程序是否停止，请查看实际输出。", leave: "退出请求已处理。请核对外层提示符是否匹配，未确认时不要重复退出。", close: "连接已关闭。远端进程是否结束仍需单独确认。", resize: "终端尺寸已调整。"};
        message("session-feedback", (explanation[action] || "操作已记录。") + "\n" + stringify(result));
      }
      await dashboard.refreshOverview(); await pollSession();
    } catch (error) { message("session-feedback", errorText(error), true); await pollSession(); }
    finally { sessionBusy = false; renderSession(); }
  }
  async function openSession(retry = false) {
    const submit = $("session-open-submit");
    if (!retry && pendingOpen) return;
    if (submit.getAttribute("aria-busy") === "true") return;
    submit.disabled = true; submit.setAttribute("aria-busy", "true"); $("session-open-retry").disabled = true;
    try {
      if (!retry) {
        const params = {target: $("session-target").value, request_id: identifier()};
        if (!params.target) throw new Error("请先在设备页面添加目标。");
        if ($("session-profile").value) params.profile = $("session-profile").value;
        if ($("session-command").value) params.command = $("session-command").value;
        pendingOpen = params;
      }
      if (!pendingOpen) return;
      const result = await sessionApi("sessions/open", pendingOpen);
      if (!result.id) {
        message("session-open-error", "原创建请求的结果仍待确认。请继续查询原请求并检查会话列表，不要重复创建。记录：" + (result.open_request_id || result.request_id), true);
        return;
      }
      pendingOpen = null;
      await dashboard.refreshOverview(); await selectSession(result.id); message("session-open-error");
    } catch (error) { message("session-open-error", errorText(error) + "\n若结果未确认，请重取原创建请求的结果，避免重复打开会话或再次发送模板进入命令。", true); await dashboard.refreshOverview(); }
    finally { submit.disabled = Boolean(pendingOpen); submit.removeAttribute("aria-busy"); $("session-open-retry").hidden = !pendingOpen; $("session-open-forget").hidden = !pendingOpen; $("session-open-retry").disabled = false; }
  }
  $("session-open-form").addEventListener("submit", (event) => { event.preventDefault(); openSession(); });
  $("session-open-retry").addEventListener("click", () => openSession(true));
  $("session-open-forget").addEventListener("click", () => {
    if (!confirm("准备结束这次请求的页面重取？这不会取消连接或模板命令。请先核对会话列表与活动记录，确认原请求的影响后，再明确发起新连接。")) return;
    pendingOpen = null; $("session-open-submit").disabled = false; $("session-open-retry").hidden = true; $("session-open-forget").hidden = true;
    message("session-open-error", "已结束本页对旧创建请求的重取。原记录仍保留，未取消其远端影响。");
  });
  $("refresh-sessions").addEventListener("click", async () => { await dashboard.refreshOverview(); await pollSession(); });
  $("session-claim").addEventListener("click", () => {
    if (sessionRecord?.controller && !confirm("接管将使当前控制者的输入权限失效。确认接管此会话？")) return;
    performSession("claim", {force: Boolean(sessionRecord?.controller || sessionRecord?.control_state === "developer_hold")});
  });
  $("session-release").addEventListener("click", () => performSession("release", {}));
  $("session-interrupt").addEventListener("click", () => { if (confirm("向当前前台发送 Ctrl+C？可能中断正在执行的测试，但不等同于取消全部受管任务。")) performSession("interrupt", {request_id: identifier()}); });
  $("session-leave").addEventListener("click", () => { if (confirm("发送当前模板配置的退出命令，并核对退出提示符？未配置退出规则时操作会被拒绝。")) performSession("leave"); });
  $("session-close").addEventListener("click", () => { if (confirm("关闭此终端连接？这不保证远端程序结束，也不取消独立运行的 Helper 作业。")) performSession("close"); });
  $("session-resize").addEventListener("click", () => performSession("resize", {cols: Number($("session-cols").value), rows: Number($("session-rows").value)}));
  async function sendInput(retry = false) {
    if (!activeSession || !ownsSession() || sessionBusy) return;
    if (!retry) {
      if (pendingInput) return;
      const data = $("session-input").value;
      if (!data && !$("session-newline").checked) return;
      pendingInput = {id: activeSession, params: {data, newline: $("session-newline").checked, sensitive: $("session-sensitive").checked, request_id: identifier()}};
    }
    if (!pendingInput || pendingInput.id !== activeSession) return;
    sessionBusy = true; renderSession();
    try {
      const result = await sessionApi("sessions/" + encodeURIComponent(activeSession) + "/write", pendingInput.params);
      pendingInput = null; $("session-input").value = ""; $("session-sensitive").checked = false;
      message("session-feedback", "本次输入已记录。发送不代表业务成功，请查看实际输出。\n" + stringify(result));
      await pollSession();
    } catch (error) {
      message("session-feedback", errorText(error) + "\n结果未确认。可使用“查询 / 重取上次发送结果”，它会复用原请求 ID，不新建第二次输入。", true);
      $("session-input").value = "";
    } finally { sessionBusy = false; renderSession(); }
  }
  $("session-input-form").addEventListener("submit", (event) => { event.preventDefault(); sendInput(); });
  $("session-retry").addEventListener("click", () => sendInput(true));
  function abandonSession() {
    if (!activeSession || !ownsSession()) return;
    const id = activeSession;
    sessionRecord = {...sessionRecord, controller: null, control_state: "developer_hold"}; renderSession();
    sessionApi("sessions/" + encodeURIComponent(id) + "/release", {abandon: true}, true).catch(() => { /* Lease expiry also preserves human hold. */ });
  }
  window.addEventListener("pagehide", abandonSession);
  $("logout").addEventListener("click", abandonSession, {capture: true});
  setInterval(async () => {
    if (currentView === "sessions") await pollSession();
    if (!activeSession || !ownsSession() || heartbeatBusy) return;
    heartbeatBusy = true;
    try { await sessionApi("sessions/" + encodeURIComponent(activeSession) + "/heartbeat", {}); }
    catch (error) { message("session-feedback", "控制权续期失败。请重新核对会话并接管，页面不会继续发送输入。\n" + errorText(error), true); sessionRecord = {...sessionRecord, controller: null}; renderSession(); }
    finally { heartbeatBusy = false; }
  }, 5000);
  if (dashboard.hasToken()) refreshAdmin();
})();
