"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const storageKey = "remote-mng.update-token";
  let token = "";
  try {
    const fragment = location.hash.slice(1);
    if (fragment) {
      token = new URLSearchParams(fragment).get("token") || "";
      sessionStorage.setItem(storageKey, token);
      history.replaceState(null, "", location.pathname + location.search);
    } else token = sessionStorage.getItem(storageKey) || "";
  } catch {
    history.replaceState(null, "", location.pathname);
  }
  const names = {queued: "准备更新", downloading: "下载发布包", preparing: "验证候选版本", handoff: "正在交接更新进程", quiescing: "等待管理器停止", activating: "切换程序与 Skill", verifying: "验证更新结果", recovering: "恢复原有版本", succeeded: "更新完成", failed: "更新失败", rolled_back: "已回退", blocked: "等待处理", interrupted: "更新中断", cancelled: "已取消更新"};
  const terminal = new Set(["succeeded", "failed", "rolled_back", "blocked", "interrupted", "cancelled"]);
  let finished = false;
  let busy = false;
  let recoveryPrompt = "";
  const initialMonitor = new URL("/update/", location.href);
  initialMonitor.hash = new URLSearchParams({token}).toString();
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  const stamp = (value) => {
    if (!value) return "尚未记录";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", {hour12: false});
  };
  function message(value) {
    if (typeof value === "string") return value;
    return value?.message || value?.advice || value?.code || "";
  }
  function notice(value) {
    $("monitor-notice").textContent = value || "";
    $("monitor-notice").hidden = !value;
  }
  function consoleLink(value) {
    if (!value) return null;
    const url = new URL(value, location.href);
    return url.protocol === "http:" && url.hostname === "127.0.0.1" && url.pathname === "/ui/"
      && !url.username && !url.password && url.hash.startsWith("#token=") ? url.href : null;
  }
  function monitorLink(value) {
    if (!value) return null;
    try {
      const url = new URL(value);
      const fragment = new URLSearchParams(url.hash.slice(1));
      return url.protocol === "http:" && url.hostname === "127.0.0.1" && url.pathname === "/update/"
        && !url.username && !url.password && !url.search && fragment.size === 1 && fragment.get("token")
        ? url.href : null;
    } catch { return null; }
  }
  function render(record) {
    const nextMonitor = monitorLink(record.monitor_url);
    if (nextMonitor && nextMonitor !== initialMonitor.href) {
      finished = true;
      $("monitor-title").textContent = "正在交接到新版更新进程…";
      location.assign(nextMonitor);
      return;
    }
    const state = record.state || "queued";
    const successful = ["succeeded", "rolled_back"].includes(state);
    finished = terminal.has(state);
    $("monitor-title").textContent = names[state] || state;
    $("monitor-state").textContent = names[state] || state;
    $("monitor-state").className = "badge " + (successful ? "good" : finished ? "warn" : "");
    $("monitor-connection").textContent = finished ? "已读取最终记录" : "独立更新进程已连接";
    $("monitor-dot").className = "dot online";
    $("monitor-observed").textContent = "本地读取 · " + new Date().toLocaleTimeString("zh-CN", {hour12: false});
    const facts = $("monitor-facts");
    facts.replaceChildren();
    for (const [label, value] of [["更新 ID", record.id], ["操作", (record.action || record.plan?.action) === "rollback" ? "兼容回退" : "安装更新"], ["原版本", record.current_version || record.previous_version || record.plan?.current_version], ["目标版本", record.target_version || record.plan?.target_version], ["开始时间", stamp(record.started_at || record.created_at)], ["记录更新", stamp(record.updated_at)]]) {
      if (value !== undefined && value !== null) facts.append(el("dt", label), el("dd", value));
    }
    const timeline = $("monitor-events");
    timeline.replaceChildren();
    for (const event of (record.events || record.steps || []).slice(-100)) {
      const phase = event.state || event.phase || event.stage;
      const item = el("li", null, "step" + (phase === "failed" ? " bad" : ""));
      item.append(el("h3", names[phase] || event.label || phase || "进度记录"));
      if (message(event)) item.append(el("p", message(event)));
      if (event.at || event.time || event.created_at) item.append(el("p", stamp(event.at || event.time || event.created_at), "small"));
      timeline.append(item);
    }
    if (!timeline.children.length) timeline.append(el("li", record.message || "更新进程已记录当前阶段。", "step"));
    $("monitor-result-panel").hidden = !finished;
    $("monitor-result-title").textContent = names[state] || "更新结果";
    const defaults = {succeeded: "程序与托管 Skill 已更新，验证结果已记录。", rolled_back: "兼容版本回退已完成。", blocked: "此次更新未能继续。处理下方阻碍后，让 Agent 重新检查更新计划。", interrupted: "更新记录表明流程中断。请让 Agent 查询此更新 ID 并核对当前版本，避免直接重复更新。", failed: "此次更新没有完成。请保留更新 ID，按恢复建议核对安装与管理器状态。"};
    $("monitor-result").textContent = message(record.error) || record.message || defaults[state] || "";
    const advice = message(record.advice) || message(record.recovery?.advice) || (record.recovery?.state ? "恢复状态：" + record.recovery.state : "");
    $("monitor-advice").textContent = advice;
    $("monitor-advice").hidden = !advice;
    $("monitor-claude").hidden = !successful;
    const recoveryPhases = new Set(["quiescing", "activating", "verifying", "recovering"]);
    const recoverable = ["failed", "blocked", "interrupted"].includes(state) && record.recovery?.state !== "restored"
      && /^[0-9a-f]{32}$/.test(record.id || "") && record.plan?.current_version
      && (record.recovery?.state === "needs_attention" || recoveryPhases.has(record.last_state)
        || (record.events || []).some((event) => recoveryPhases.has(event.state)));
    $("monitor-recovery").hidden = !recoverable;
    if (recoverable) {
      const command = "rmg update recover --id " + record.id;
      $("monitor-recovery-command").textContent = command;
      recoveryPrompt = `请使用 remote-mng 查询更新 ${record.id}，核对原版本 ${record.plan.current_version} 和本地恢复记录，然后执行 ${command} 恢复此更新。${record.plan.home ? "使用原状态目录：" + record.plan.home + "。" : "使用此更新原有的状态目录。"}保留配置与远端任务 ID，不重发业务命令，报告恢复结果。`;
    } else {
      $("monitor-recovery-command").textContent = "";
      recoveryPrompt = "";
    }
    const link = consoleLink(record.console_url);
    $("monitor-console").hidden = !link;
    if (link) $("monitor-console").href = link;
    else $("monitor-console").removeAttribute("href");
  }
  async function refresh() {
    if (busy || !token) return;
    busy = true;
    $("monitor-refresh").disabled = true;
    try {
      const response = await fetch("/update/api/status", {cache: "no-store", credentials: "omit", headers: {Authorization: "Bearer " + token}});
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(response.status === 401 ? "更新进度链接已失效。请让 Agent 查询原更新 ID 获取状态。" : payload.error?.message || "暂时无法读取更新记录。");
      render(payload.result);
      notice();
    } catch (error) {
      $("monitor-dot").className = "dot offline";
      $("monitor-connection").textContent = "连接中断，保留已读取记录";
      notice(error.message + " 更新可能仍在进行；请勿重复启动。可让 Agent 查询原更新 ID。");
    } finally { busy = false; $("monitor-refresh").disabled = false; }
  }
  $("monitor-refresh").addEventListener("click", refresh);
  $("monitor-copy-recovery").addEventListener("click", async () => {
    if (!recoveryPrompt) return;
    try { await navigator.clipboard.writeText(recoveryPrompt); notice("已复制恢复请求，可交给当前 Agent 继续处理。"); }
    catch { notice(recoveryPrompt); }
  });
  if (!token) {
    $("monitor-connection").textContent = "缺少此更新的访问凭据";
    notice("请使用 Agent 或控制台返回的完整本地进度链接打开此页面。");
  } else {
    refresh();
    setInterval(() => { if (!finished) refresh(); }, 1500);
  }
})();
