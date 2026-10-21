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
  let adminRefresh = null;
  let deviceCheckGeneration = 0;
  let deviceContext = null;
  let currentView = "overview";
  let editingDevice = null;
  let deviceOriginal = {};
  let deviceRevision = null;
  let editingProfile = null;
  let profileRevision = null;
  let editingTemplate = null;
  let templateRevision = null;
  let pendingChange = null;
  let changeGeneration = 0;
  let activeSession = null;
  let sessionRecord = null;
  let sessionOffset = 0;
  let sessionBusy = false;
  let sessionPollBusy = false;
  let pendingInput = null;
  let heartbeatBusy = false;
  let pendingOpen = null;
  const pendingProfileTests = new Map();
  const namedStates = {open: "已连接", disconnected: "连接断开", closed: "已关闭", unknown: "结果待确认", ready: "检查通过", missing: "缺少登录信息", failed: "检查失败", succeeded: "已完成", not_checked: "尚未测试", needs_attention: "需要处理", warning: "需要处理", developer_hold: "人工保留，等待明确归还", unclaimed: "无人控制", owned: "已取得控制权"};
  const stateLabel = (value) => namedStates[value] || value || "未确认";
  const timestamp = (value) => value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString("zh-CN", {hour12: false}) : "未记录";

  function message(id, value = "", error = false) {
    const target = $(id);
    target.textContent = value;
    target.hidden = !value;
    target.classList.toggle("error-message", error);
  }
  function errorText(error) {
    const advice = {config_conflict: "配置已在其他入口更新。请关闭摘要，刷新配置后重新编辑。", target_exists: "设备名称已存在。请换一个名称，或编辑已有设备。", target_in_use: "这台设备仍有任务或会话引用。更换设备地址时，请新建一个设备名称。", credential_missing: "登录信息已失效。请编辑设备并重新输入密码。", invalid_target: "连接信息不完整或不符合要求，请检查地址、端口和高级选项。", authentication_failed: "登录认证失败。请检查用户名、密码或私钥。"};
    return advice[error.code] || error.message || String(error);
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
    const container = $(id);
    container.replaceChildren(node("h3", title));
    if (typeof result === "string") container.append(node("p", result, "result-message"));
    else {
      container.append(node("p", result.message || result.advice || "操作已有记录。展开技术详情查看完整结果。", "small"));
      const details = node("details"); details.append(node("summary", "技术详情"), node("pre", stringify(result))); container.append(details);
    }
  }
  function appendChecks(container, checks) {
    const labels = {tool: "本地程序", skill: "Claude Code Skill", daemon: "本地管理器", connection: "认证与连接", posix_shell: "POSIX Shell", job_dependencies: "持久作业运行条件", helper: "远端 Helper", helper_storage: "Helper 存储", helper_directory: "Helper 目录", directory: "部署目录", sftp: "SSH 文件通道", scp: "旧版 SCP", shell_probe: "Shell 能力探测"};
    const states = {pass: "通过", fail: "失败", warning: "需要处理", not_checked: "未检查"};
    for (const check of checks || []) {
      const row = node("div", null, "diagnostic-row");
      const top = node("div", null, "row-top");
      const label = labels[check.name] || (check.name?.startsWith("credential_") ? "环境变量凭据" : check.name?.startsWith("temporary_credential_") ? "临时凭据" : check.name?.startsWith("key_file_") ? "私钥文件" : check.name);
      top.append(node("strong", label || "检查"), node("span", states[check.state] || stateLabel(check.state), "badge " + (check.state === "pass" ? "good" : check.state === "fail" ? "bad" : "warn")));
      const explanation = explainCheck(check);
      row.append(top, node("p", explanation.message, "small"));
      if (check.diagnostic?.stage) {
        const stages = {dns: "解析地址", tcp: "连接端口", proxy: "代理连接", jump: "跳板连接", host_key: "核对主机身份", authentication: "用户认证", login: "交互登录", login_flow: "交互登录", shell: "确认 Shell", sftp: "建立文件通道"};
        row.append(node("p", "失败阶段 · " + (stages[check.diagnostic.stage] || check.diagnostic.stage), "advice"));
      }
      if (explanation.advice) row.append(node("p", explanation.advice, "small"));
      container.append(row);
    }
  }
  function explainCheck(check) {
    const passed = check.state === "pass";
    const name = check.name || "";
    const descriptions = {
      tool: ["本机工具已就绪。", "本机工具需要检查。"],
      skill: ["Claude Code Skill 已安装，且与当前工具版本一致。", "Claude Code Skill 尚未通过完整性或版本检查。请查看技术详情并让 Agent 检查安装状态。"],
      daemon: ["本机管理器正在运行。", "本机管理器尚未就绪或版本需要更新；请先核对当前任务，再检查管理器状态。"],
      connection: ["已成功连接设备并完成认证。", "尚未验证连接成功，请按失败阶段检查连接信息。"],
      posix_shell: ["已确认可执行标准 Shell 命令。", check.state === "not_checked" ? "此设备配置为菜单或应用终端，未发送 Shell 探测命令。" : "未确认标准 Shell 可用，请检查设备的终端类型和 Shell 配置。"],
      job_dependencies: ["后台长任务需要的 Linux 环境和命令已就绪。", "后台长任务的运行条件尚未满足。" + (check.missing_tools?.length ? " 缺少命令：" + check.missing_tools.join("、") : " 请检查 Linux /proc 与所需命令。")],
      helper: ["后台任务组件已安装，协议检查通过。", check.message === "Durable-job helper is not installed" ? "尚未安装后台任务组件。需要断线后仍可查询的长任务时，点击“安装后台任务组件”。" : "后台任务组件尚未确认兼容或可用。请查看详情，必要时安装本工具附带的组件。"],
      helper_storage: ["后台任务日志存储检查通过。", "后台任务日志的存储空间或组件兼容性需要处理。请查看详情后再决定更新组件或清理日志。"],
      helper_directory: ["已有后台任务目录可写；检查未创建文件。", "尚未确认后台任务目录可写。组件未安装时可稍后再检查。"],
      directory: ["部署目录存在且可写。", "部署目录不存在或当前账号不可写，请检查路径与权限。"],
      sftp: ["SSH 文件传输通道可用，检查未写入文件。", check.state === "not_checked" ? "未配置 SSH 文件传输通道。Telnet 设备需要单独配置传包连接。" : "尚未验证 SSH 文件传输通道，请检查目标机的 SFTP 服务或独立传包连接。"],
      scp: ["旧版 SCP 检查通过。", "本次连接检查未执行旧版 SCP 传输。"],
      shell_probe: ["远端能力检查通过。", "远端能力检查未完成，请检查 Shell 兼容性与连接稳定性。"],
    };
    if (name.startsWith("temporary_credential_")) return {message: passed ? "当前临时登录信息可用。" : "临时登录信息已失效，请编辑设备重新输入。"};
    if (name.startsWith("credential_")) return {message: passed ? "管理器可读取配置的环境变量凭据。" : "管理器无法读取配置的环境变量。请检查启动环境，或改为直接输入密码。"};
    if (name.startsWith("key_file_")) return {message: passed ? "本机私钥文件存在。实际登录仍需通过连接测试。" : "未找到本机私钥文件，请编辑设备检查文件路径。"};
    const stage = check.diagnostic?.stage;
    const failures = {host_key: "服务器身份尚未验证。请从可信来源核对指纹，再保存服务器身份。", authentication: "设备未接受登录认证。请检查用户名及所选密码、私钥或 SSH Agent。", dns: "设备地址解析失败。请核对主机名、DNS 或改用正确的 IP 地址。", tcp: "网络连接未建立。请检查设备地址、端口、网络路由与服务是否可达。", proxy: "代理连接未完成。请检查代理地址、端口和认证配置。", jump: "跳板连接未完成。请检查跳板配置，并在技术详情核对失败端点。", login: "交互登录未完成。请检查登录提示符与自动输入规则。", login_flow: "交互登录流程未完成。请检查提示符、步骤与跳转规则。"};
    const codeStages = {host_key_untrusted: "host_key", authentication_failed: "authentication", dns_failed: "dns"};
    const failure = !passed && failures[stage || codeStages[check.code]];
    return {message: failure || descriptions[name]?.[passed ? 0 : 1] || check.message || "检查已有记录。", advice: descriptions[name] || failure ? undefined : check.advice};
  }
  function showDiagnostics(result, name) {
    const container = $("device-action-result");
    container.replaceChildren(node("h3", "设备检查 · " + name));
    container.append(node("p", `${result.connection?.connected ? "连接已验证" : "连接尚未验证"} · ${result.job_ready ? "后台长任务已就绪" : "后台长任务未就绪或未检查"} · ${timestamp(result.checked_at)}`, "small"));
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
    if (!["overview", "devices", "tasks", "sessions", "operations", "settings"].includes(view)) return;
    if (currentView === "sessions" && view !== "sessions") abandonSession();
    currentView = view;
    for (const page of document.querySelectorAll("[data-page]")) page.hidden = page.dataset.page !== view;
    for (const tab of document.querySelectorAll("[data-view]")) {
      if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    const headings = {overview: ["远程工作总览", "连接远程设备，跟踪 Agent 的任务，需要时接管终端调试。"], devices: ["设备连接", "添加 SSH 或 Telnet 设备，测试连接，或打开终端。"], tasks: ["任务记录", "查看 Agent 与常用操作的执行过程、输出和结果。"], sessions: ["终端调试", "选择设备打开终端；也可以观察或接管 Agent 已有的连接。"], operations: ["常用操作", "保存重复使用的命令，选择设备后运行，结果统一进入任务记录。"], settings: ["工具设置", "管理本机配置、备份、更新与高级凭据。"]};
    $("page-heading").textContent = headings[view][0];
    $("page-description").textContent = headings[view][1];
    if (["devices", "settings", "sessions", "operations"].includes(view)) refreshAdmin();
    if (view === "sessions") renderSessionList();
  }
  for (const tab of document.querySelectorAll("[data-view]")) tab.addEventListener("click", () => navigate(tab.dataset.view));
  for (const entry of document.querySelectorAll("[data-go-view]")) entry.addEventListener("click", () => navigate(entry.dataset.goView));
  for (const entry of document.querySelectorAll("[data-add-device]")) entry.addEventListener("click", async () => { navigate("devices"); await refreshAdmin(); editDevice(); });
  document.addEventListener("rmg:navigate", (event) => navigate(event.detail));
  document.addEventListener("rmg:edit-target", async (event) => {
    navigate("devices"); await refreshAdmin();
    const item = admin?.targets?.find((target) => target.name === event.detail);
    if (item || event.detail === null) editDevice(item);
  });
  document.addEventListener("rmg:overview", (event) => {
    overview = event.detail;
    if (admin) renderDevices();
    renderSessionList();
    if (activeSession && currentView === "sessions") pollSession();
  });
  document.addEventListener("rmg:test-target", (event) => { navigate("devices"); runButton($("diagnose-target"), async () => { await refreshAdmin(); await testDevice(event.detail); }); });
  document.addEventListener("rmg:terminal-target", (event) => prepareTerminal(event.detail));

  async function adminCall(action, params = {}) {
    return dashboard.api("admin/" + action, "POST", params);
  }
  async function refreshAdmin() {
    if (!dashboard.hasToken()) return;
    if (adminRefresh) return adminRefresh;
    adminRefresh = (async () => { try {
      admin = await dashboard.api("admin");
      renderDevices(); renderProfiles(); renderTemplates(); renderCredentials(); renderDiagnostics();
      const names = (admin.targets || []).map((item) => item.name);
      fillSelect("manage-target", names);
      fillSelect("session-target", names);
      fillSelect("session-profile", (admin.profiles || []).map((item) => item.name), "普通终端");
      updateDeviceContext();
      if (document.activeElement !== $("settings-log-limit") && document.activeElement !== $("settings-timeout")) {
        $("settings-log-limit").value = (admin.settings?.local_log_max_bytes || 67108864) / 1048576;
        $("settings-timeout").value = admin.settings?.default_timeout ?? 30;
      }
    } catch (error) { dashboard.notice(errorText(error)); }
    finally { adminRefresh = null; }
    })();
    return adminRefresh;
  }

  async function previewChange(action, params, title, onApplied) {
    const generation = ++changeGeneration;
    const preview = await adminCall(action, {...params, dry_run: true});
    if (generation !== changeGeneration || !dashboard.hasToken()) return;
    const revision = preview.revision || params.expected_revision;
    pendingChange = {action, params: {...params, expected_revision: revision, dry_run: false}, onApplied};
    $("change-title").textContent = title;
    $("change-description").textContent = action === "template.run" ? "核对设备与命令后执行一次。结果会出现在任务记录中。" : "核对以下内容后保存。配置将与 Agent 同步使用。";
    $("change-preview").textContent = stringify(preview);
    renderChangeSummary(action, params, preview);
    message("change-error");
    $("change-apply").disabled = Boolean(preview.blockers?.length);
    $("change-apply").textContent = action === "template.run" ? "确认执行一次" : action === "target.save" ? "确认保存" : "确认应用";
    if (!$("change-dialog").open) $("change-dialog").showModal();
  }
  function renderChangeSummary(action, params, preview) {
    const summary = $("change-summary"); if (!summary) return;
    summary.replaceChildren();
    if (action === "target.save") {
      const cfg = preview.config || params.config;
      const facts = node("dl", null, "facts");
      for (const [label, value] of [["设备", params.name], ["连接地址", `${cfg.host}:${cfg.port || (cfg.protocol === "ssh" ? 22 : 23)}`], ["协议", cfg.protocol.toUpperCase()], ["用户名", cfg.username || "未填写"], ["登录方式", cfg.password_ref ? "密码登录" : cfg.password_env ? "环境变量密码" : cfg.client_keys?.length ? "私钥文件" : "系统 SSH 密钥 / 无自动登录"]]) facts.append(node("dt", label), node("dd", value));
      summary.append(facts, node("p", params.password ? "密码仅在当前本机管理器中有效，重启后需重新输入。" : "此次保存不包含新密码。", "small"));
    } else if (action === "hostkey.trust") {
      summary.append(node("p", "设备：" + (preview.name || "当前所选设备")), node("p", "将保存你已独立核对的服务器身份。"), node("pre", params.fingerprint));
      if (params.replace) summary.append(node("p", "本次会替换该端点原来保存的服务器身份。", "advice"));
    } else if (action === "template.run") {
      summary.append(node("p", "设备：" + params.target), node("p", "常用操作：" + params.name));
      const template = admin?.operation_templates?.find((item) => item.name === params.name);
      if (template) summary.append(node("pre", template.config.command));
    } else {
      const changes = preview.diff || [];
      summary.append(node("p", changes.length ? `共 ${changes.length} 项配置变更` : "配置内容未变化。"));
      const list = node("ul");
      for (const change of changes.slice(0, 12)) list.append(node("li", change.field + (change.before === undefined || change.before === null ? " · 新增" : change.after === undefined || change.after === null ? " · 移除" : " · 更新")));
      summary.append(list);
    }
    for (const blocker of preview.blockers || []) summary.append(node("p", typeof blocker === "string" ? blocker : blocker.message || stringify(blocker), "advice"));
  }
  function closeChange() { changeGeneration += 1; $("change-dialog").close(); pendingChange = null; }
  $("change-cancel").addEventListener("click", closeChange);
  $("change-dismiss").addEventListener("click", closeChange);
  $("change-dialog").addEventListener("cancel", () => { changeGeneration += 1; pendingChange = null; });
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
    const query = ($("device-search")?.value || "").trim().toLowerCase();
    const devices = (admin.targets || []).filter((item) => [item.name, item.config.host, item.config.username, item.config.protocol].some((value) => String(value || "").toLowerCase().includes(query)));
    if ($("device-list-empty")) $("device-list-empty").hidden = Boolean(admin.targets?.length);
    if (query && !devices.length) list.append(node("p", "没有找到匹配的设备。试试设备名称、地址或用户名。", "empty"));
    for (const item of devices) {
      const card = node("article", null, "target-card");
      const top = node("div", null, "row-top");
      top.append(node("h3", item.name), node("span", item.config.protocol.toUpperCase(), "badge"));
      card.append(top, node("p", `${item.config.username ? item.config.username + "@" : ""}${item.config.host}:${item.config.port || (item.config.protocol === "ssh" ? 22 : 23)}`, "device-address"));
      const auth = item.authentication || {};
      const check = overview?.targets?.find((target) => target.name === item.name)?.inspection || overview?.targets?.find((target) => target.name === item.name)?.last_check;
      card.append(node("p", auth.state === "missing" ? "登录信息已失效，请补填密码或检查私钥。" : check ? `最近检查：${stateLabel(check.state)} · ${timestamp(check.checked_at || check.observed_at)}` : "配置已保存 · 尚未测试连接", auth.state === "missing" ? "advice" : "small"));
      const actions = node("div", null, "actions");
      actions.append(actionButton("测试连接", () => testDevice(item.name), "primary"), actionButton("打开终端", () => prepareTerminal(item.name)), actionButton(auth.state === "missing" ? "补填登录信息" : "编辑", () => editDevice(item)));
      const more = node("details", null, "device-more"); more.append(node("summary", "更多操作"));
      const extra = node("div", null, "actions");
      extra.append(actionButton("后台任务支持", () => { selectDeviceContext(item.name); $("device-action-result").scrollIntoView({behavior: "smooth", block: "center"}); }), actionButton("复制配置", () => editDevice(item, true)), actionButton("删除设备", () => previewChange("target.delete", {name: item.name, expected_revision: admin.revision}, "删除设备 " + item.name), "danger"));
      more.append(extra);
      card.append(actions, more); list.append(card);
    }
  }
  function updateDeviceContext() {
    const item = admin?.targets?.find((target) => target.name === $("manage-target").value);
    const name = item?.name || null;
    if (deviceContext !== name) {
      deviceContext = name; deviceCheckGeneration += 1;
      $("device-action-result").replaceChildren(); message("device-feedback");
    }
    if ($("manage-target-title")) $("manage-target-title").textContent = item ? "连接检查 · " + item.name : "连接检查与后台任务支持";
    if ($("manage-target-address")) $("manage-target-address").textContent = item ? `${item.config.protocol.toUpperCase()} · ${item.config.host}:${item.config.port || (item.config.protocol === "ssh" ? 22 : 23)}` : "添加设备后，可在这里检查连接和启用断线后可查询的后台任务。";
  }
  function selectDeviceContext(name) { $("manage-target").value = name; updateDeviceContext(); }
  async function prepareTerminal(name) {
    navigate("sessions"); await refreshAdmin(); $("session-target").value = name;
    $("session-open-form").scrollIntoView({behavior: "smooth", block: "start"}); $("session-open-submit").focus();
    dashboard.notice("已选择设备 " + name + "。点击“打开终端”建立连接。" );
  }
  async function testDevice(name) {
    selectDeviceContext(name);
    const generation = ++deviceCheckGeneration;
    const resultBox = $("device-action-result"); resultBox.scrollIntoView({behavior: "smooth", block: "center"});
    message("device-feedback", "正在连接 " + name + "…");
    showResult("device-action-result", "正在测试连接 · " + name, "正在核对设备身份、登录信息与远端能力，请稍候…");
    try {
      const result = await dashboard.api("targets/" + encodeURIComponent(name) + "/check", "POST");
      if (generation !== deviceCheckGeneration) { await dashboard.refreshOverview(); return; }
      showDiagnostics(result, name);
      message("device-feedback", result.connection?.connected ? name + " 连接成功。可以打开终端；需要后台长任务时，再安装后台任务组件。" : name + " 连接未通过。请查看下方检查结果和处理建议。", !result.connection?.connected);
      const next = node("div", null, "actions");
      if (result.connection?.connected) next.append(actionButton("打开终端", () => prepareTerminal(name), "primary"));
      else {
        next.append(actionButton("修改连接信息", () => editDevice(admin?.targets?.find((item) => item.name === name))));
        const failureStage = result.connection?.error?.details?.diagnostic?.stage || result.checks?.find((check) => check.name === "connection")?.diagnostic?.stage;
        if (admin?.targets?.find((item) => item.name === name)?.config.protocol === "ssh" && failureStage === "host_key") next.append(actionButton("核对服务器指纹", () => { selectDeviceContext(name); $("hostkey-endpoint").value = "target"; $("probe-hostkey").click(); }));
      }
      resultBox.append(next);
    } catch (error) {
      if (generation !== deviceCheckGeneration) return;
      showResult("device-action-result", "连接未完成 · " + name, errorText(error));
      message("device-feedback", "连接未完成。请检查地址、端口和登录方式。", true);
    }
    await dashboard.refreshOverview(); renderDevices();
  }
  $("device-search")?.addEventListener("input", () => { if (admin) renderDevices(); });
  $("manage-target").addEventListener("change", updateDeviceContext);
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
    $("device-auth").value = config.password_ref ? "password" : config.password_env ? "environment" : config.client_keys?.length ? "key" : item ? "agent" : "password";
    $("device-password").value = ""; $("device-password").type = "password";
    $("device-password-toggle").textContent = "显示"; $("device-password-toggle").setAttribute("aria-pressed", "false");
    $("device-password-toggle").setAttribute("aria-label", "显示密码");
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
    const steps = config.login_steps || [];
    const simplePassword = !config.login_flow && steps.length <= 2 && steps.length > 0 && steps[steps.length - 1].expect === "(?i)password:\\s*$" && (config.password_ref && steps[steps.length - 1].send_ref === config.password_ref || config.password_env && steps[steps.length - 1].send_env === config.password_env) && (steps.length === 1 || steps[0].send === config.username && steps[0].expect === "(?i)(login|username):\\s*$");
    $("telnet-login-mode").value = simplePassword ? steps.length === 2 ? "user-password" : "password" : config.login_flow || steps.length ? "advanced" : "none";
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
    $("device-password-field").hidden = method !== "password";
    const ref = deviceOriginal.password_ref;
    const available = ref && (admin?.targets || []).some((item) => item.authentication?.references?.some((entry) => entry.reference === ref && entry.available));
    $("device-password").placeholder = ref ? available ? "已填写；留空保留当前密码" : "密码已失效，请重新输入" : "输入设备的登录密码";
    $("device-password-status").textContent = ref ? available ? "当前密码可用。需要更换时再填写；管理器重启后需重新输入。" : "当前密码不可用。请重新输入，无需到其他页面配置。" : "仅保存在本机管理器内存中，重启后需重新输入。";
    $("device-credential-field").hidden = !["memory", "environment"].includes(method);
    $("device-credential").placeholder = method === "memory" ? "在设置中创建的凭据引用" : "管理器进程可读取的环境变量名";
    $("device-auth-advice").textContent = $("device-protocol").value === "telnet" ? "Telnet 以明文传输登录信息。下方可选择是否自动输入用户名和密码。" : method === "password" ? "保存后测试连接，确认服务器接受此用户名和密码。" : method === "key" ? "填写本机私钥文件的完整路径。" : method === "agent" ? "使用管理器可访问的 SSH Agent 或本机默认密钥。" : "高级用法：引用管理器可读取的已有凭据。";
  }
  $("device-password-toggle").addEventListener("click", () => { const visible = $("device-password").type === "password"; $("device-password").type = visible ? "text" : "password"; $("device-password-toggle").textContent = visible ? "隐藏" : "显示"; $("device-password-toggle").setAttribute("aria-pressed", String(visible)); $("device-password-toggle").setAttribute("aria-label", visible ? "隐藏密码" : "显示密码"); });
  const closeDeviceEditor = () => { closeChange(); $("device-editor-panel").hidden = true; $("device-password").value = ""; };
  $("logout").addEventListener("click", () => { closeDeviceEditor(); closeChange(); $("credential-value").value = ""; $("session-input").value = ""; pendingInput = null; }, {capture: true});
  $("add-device").addEventListener("click", () => editDevice());
  $("close-device-editor").addEventListener("click", closeDeviceEditor);
  $("reset-device").addEventListener("click", closeDeviceEditor);
  $("device-auth").addEventListener("change", () => { $("device-password").value = ""; updateAuthFields(); });
  $("device-protocol").addEventListener("change", () => { const ssh = $("device-protocol").value === "ssh"; $("device-shell").value = ssh ? "posix" : "unknown"; $("device-port").placeholder = ssh ? "22" : "23"; if ($("device-port").value === (ssh ? "23" : "22")) $("device-port").value = ssh ? "22" : "23"; if (!ssh && $("device-auth").value === "password") $("telnet-login-mode").value = "user-password"; updateAuthFields(); });
  $("refresh-admin").addEventListener("click", refreshAdmin);
  $("device-test-saved").type = "submit";
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
      let password;
      if (auth === "password") {
        password = $("device-password").value;
        if (password) config.password_ref = "__device_password__";
        else if (deviceOriginal.password_ref) {
          const available = (admin.targets || []).some((item) => item.authentication?.references?.some((entry) => entry.reference === deviceOriginal.password_ref && entry.available));
          if (!available) throw new Error("密码已失效，请在当前页面重新输入密码。");
          config.password_ref = deviceOriginal.password_ref;
        } else if (config.protocol !== "telnet" || ["user-password", "password"].includes($("telnet-login-mode").value)) throw new Error("请填写登录密码，或选择私钥 / 系统 SSH 密钥。");
      }
      if (auth === "key") config.client_keys = $("device-keys").value.split("\n").map((s) => s.trim()).filter(Boolean);
      if (auth === "memory" || auth === "environment") {
        if (!$("device-credential").value.trim()) throw new Error("请填写密码引用，或选择其他认证方式。");
        config[auth === "memory" ? "password_ref" : "password_env"] = $("device-credential").value.trim();
      }
      // Advanced login steps may use custom prompts or a multi-stage flow.
      // Preserve their routing while replacing only the device's own password
      // source; independently configured hop credentials remain untouched.
      if (password) for (const steps of [config.login_steps, config.login_flow?.steps]) if (Array.isArray(steps)) for (const step of steps) {
        if (step && typeof step === "object" && (deviceOriginal.password_ref && step.send_ref === deviceOriginal.password_ref || deviceOriginal.password_env && step.send_env === deviceOriginal.password_env)) {
          delete step.send_env; step.send_ref = "__device_password__";
        }
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
          if (!config.password_ref && !config.password_env) throw new Error("请填写密码，才能自动登录 Telnet。");
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
      const params = {name, mode: editingDevice ? "replace" : "create", config, expected_revision: deviceRevision};
      if (password) params.password = password;
      const testAfter = submit.id === "device-test-saved";
      await previewChange("target.save", params, editingDevice ? "保存设备 " + name : "添加设备 " + name, async () => {
        closeDeviceEditor(); selectDeviceContext(name);
        message("device-feedback", "设备 " + name + " 已保存。" + (testAfter ? "正在测试连接…" : "点击“测试连接”确认远端可用。"));
        dashboard.notice("设备配置已保存，Agent 可直接使用设备名 " + name + "。");
        if (testAfter) await testDevice(name);
      });
      $("device-password").value = "";
      message("device-error");
    } catch (error) { message("device-error", errorText(error), true); }
    finally { $("device-password").value = ""; submit.disabled = false; }
  });

  $("diagnose-target").addEventListener("click", (event) => runButton(event.currentTarget, () => testDevice(targetName())));
  $("probe-hostkey").addEventListener("click", (event) => runButton(event.currentTarget, async () => {
    const name = targetName();
    const probe = await adminCall("hostkey.probe", {name, route: $("hostkey-endpoint").value});
    showResult("device-action-result", "核对主机身份 · " + name, probe);
    const identity = node("dl", null, "facts");
    for (const [label, value] of [["服务器端点", `${probe.host}:${probe.port}`], ["公钥算法", probe.algorithm], ["待核对指纹", probe.fingerprint]]) identity.append(node("dt", label), node("dd", value));
    $("device-action-result").append(identity);
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
    $("session-title").textContent = record ? `${record.target || "终端"} · 终端` : "终端输出";
    $("session-status").textContent = record ? `${stateLabel(record.state)} · 当前环境 ${record.state_label || "未知"}${record.control_expires_at ? " · 控制权有效至 " + timestamp(record.control_expires_at) : ""}` : "选择一个会话查看输出。";
    $("session-owner").textContent = owns ? "由此页面控制" : record?.control_state === "developer_hold" ? "人工保留" : record?.controller === "agent" ? "Agent 控制" : record?.controller ? "其他页面控制" : "仅观察";
    $("session-claim").disabled = !open || owns || sessionBusy;
    $("session-release").disabled = !open || !owns || sessionBusy;
    for (const id of ["session-interrupt", "session-leave", "session-close", "session-input", "session-send", "session-resize"]) $(id).disabled = !open || !owns || sessionBusy || (id === "session-send" && Boolean(pendingInput));
    $("session-retry").hidden = !pendingInput;
    $("session-retry").disabled = !owns || sessionBusy;
  }
  function sessionFeedback(text, result) {
    message("session-feedback", text);
    let details = $("session-result-details");
    if (!details) { details = node("details"); details.id = "session-result-details"; $("session-feedback").after(details); }
    details.replaceChildren(node("summary", "本次操作的技术详情"), node("pre", stringify(result)));
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
        sessionFeedback(explanation[action] || "操作已记录。", result);
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
      sessionFeedback("本次输入已发送，请从终端输出核对执行结果。", result);
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
  function clearFormSecrets() { $("device-password").value = ""; $("credential-value").value = ""; pendingChange = null; }
  window.addEventListener("pagehide", clearFormSecrets);
  $("logout").addEventListener("click", clearFormSecrets);
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
