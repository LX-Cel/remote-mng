"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const storageKey = "remote-mng.ui-token";
  let token = "";
  try {
    const fragment = location.hash.slice(1);
    if (fragment) {
      token = fragment.startsWith("token=") ? new URLSearchParams(fragment).get("token") : decodeURIComponent(fragment);
      sessionStorage.setItem(storageKey, token || "");
      history.replaceState(null, "", location.pathname + location.search);
    } else {
      token = sessionStorage.getItem(storageKey) || "";
    }
  } catch {
    history.replaceState(null, "", location.pathname);
  }

  let snapshot = null;
  let selectedTask = null;
  let selectedLog = null;
  let offset = 0;
  let overviewBusy = false;
  let logBusy = false;
  let logGeneration = 0;
  const rendered = new Map();
  const busyTargets = new Set();
  const stateNames = {
    running: "进行中", pending: "等待执行", open: "已连接", succeeded: "步骤完成",
    failed: "失败", cancelled: "已取消", unknown: "结果待确认", needs_attention: "需要关注",
    ready: "检查通过", disconnected: "连接已断开", closed: "已关闭", observed: "已观察到结果",
    dispatching: "正在提交", cancel_requested: "已请求取消",
    not_checked: "尚未检查", completed: "已完成", submitted: "已提交", uncertain: "结果待确认",
  };
  const stateName = (state) => stateNames[state] || state || "尚未确认";
  const tone = (state) => ["ready", "succeeded", "observed", "completed"].includes(state) ? "good"
    : ["failed"].includes(state) ? "bad"
      : ["unknown", "needs_attention", "disconnected", "uncertain"].includes(state) ? "warn" : "";
  const stamp = (value) => {
    if (!value) return "尚无观测时间";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", {hour12: false});
  };
  const short = (value) => String(value || "—").slice(0, 12);
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  const badge = (state, label) => el("span", label || stateName(state), "badge " + tone(state));
  const empty = (text) => el("p", text, "empty");
  const button = (text, action, className = "quiet") => {
    const node = el("button", text, className);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  };
  const changed = (key, value) => {
    const encoded = JSON.stringify(value);
    if (rendered.get(key) === encoded) return false;
    rendered.set(key, encoded);
    return true;
  };
  const notice = (message = "") => {
    $("notice").textContent = message;
    $("notice").hidden = !message;
  };

  async function api(path, method = "GET") {
    if (!token) throw new Error("控制台尚未解锁。请让 Agent 运行 rmg ui，使用返回的本地链接打开控制台。");
    const response = await fetch("/ui/api/" + path, {
      method, cache: "no-store", credentials: "omit",
      headers: {Authorization: "Bearer " + token, ...(method === "POST" ? {"Content-Type": "application/json"} : {})},
      ...(method === "POST" ? {body: "{}"} : {}),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      if (response.status === 401) throw new Error("访问凭据已失效。管理器重启后，请使用 Agent 提供的新控制台链接。");
      throw new Error(payload.error?.message || "暂时无法读取状态，请稍后重试。");
    }
    return payload.result;
  }

  function renderTargets(targets) {
    if (!changed("targets", [targets, [...busyTargets]])) return;
    const container = $("targets");
    container.replaceChildren();
    if (!targets.length) container.append(empty("还没有配置设备。告诉 Agent 目标地址、连接方式和认证引用后，即可开始。"));
    for (const target of targets) {
      const check = target.inspection || target.last_check;
      const card = el("article", null, "target-card");
      const top = el("div", null, "row-top");
      top.append(el("h3", target.name), badge(check?.state || "not_checked"));
      const address = `${(target.protocol || "").toUpperCase()} · ${target.host || "—"}${target.port ? ":" + target.port : ""}`;
      card.append(top, el("p", address, "small"));
      if (check?.checks) {
        const details = el("details");
        details.append(el("summary", "查看检查结果", "small"));
        const checks = Array.isArray(check.checks) ? check.checks : Object.entries(check.checks).map(([name, value]) => ({name, ...(typeof value === "object" ? value : {message: value})}));
        for (const item of checks) {
          const line = el("p", `${item.label || item.name || item.code || "检查"}：${item.message || item.summary || stateName(item.state || item.status)}`, "small");
          details.append(line);
        }
        card.append(details);
      }
      const bottom = el("div", null, "row-bottom");
      const checkButton = button(busyTargets.has(target.name) ? "检查中…" : "检查设备", async () => {
        busyTargets.add(target.name);
        renderTargets(snapshot.targets);
        try {
          await api("targets/" + encodeURIComponent(target.name) + "/check", "POST");
          notice();
        } catch (error) { notice(error.message); }
        finally { busyTargets.delete(target.name); await refreshOverview(); }
      });
      checkButton.disabled = busyTargets.has(target.name);
      bottom.append(el("span", check ? stamp(check.checked_at || check.observed_at) : "远端状态尚未检查", "small"), checkButton);
      card.append(bottom);
      container.append(card);
    }
  }

  function renderTasks(tasks) {
    if (!changed("tasks", [tasks, selectedTask])) return;
    const container = $("tasks");
    container.replaceChildren();
    if (!tasks.length) container.append(empty("还没有任务记录。Agent 通过 task 命令创建任务后，执行步骤会显示在这里。"));
    for (const task of tasks) {
      const row = button("", async () => { selectedTask = task.id; renderTasks(snapshot.tasks); await refreshTask(); }, "task-row" + (selectedTask === task.id ? " selected" : ""));
      row.setAttribute("aria-pressed", String(selectedTask === task.id));
      const top = el("div", null, "row-top");
      top.append(el("strong", task.title || short(task.id)), badge(task.state));
      row.append(top, el("p", `${task.target || "未指定设备"} · ${task.step_count ?? task.steps?.length ?? 0} 个步骤 · ${short(task.id)}`, "small"));
      row.append(el("p", stamp(task.updated_at || task.created_at), "small"));
      container.append(row);
    }
  }

  function addFact(container, title, value) {
    if (value === undefined || value === null || value === "") return;
    container.append(el("dt", title), el("dd", typeof value === "object" ? JSON.stringify(value) : value));
  }

  function logReference(resource) {
    if (!resource) return null;
    if (resource.kind === "job") return {kind: "job", target: resource.target, job_id: resource.job_id || resource.id};
    if (["operation", "session"].includes(resource.kind)) return {kind: resource.kind, id: resource.id};
    return null;
  }

  function renderTask(task) {
    if (!changed("detail", task)) return;
    $("task-title").textContent = task.title || "任务详情";
    $("task-subtitle").textContent = task.id;
    const container = $("task-detail");
    container.replaceChildren();
    const facts = el("dl", null, "facts");
    addFact(facts, "任务状态", stateName(task.state));
    addFact(facts, "目标设备", task.target);
    addFact(facts, "产物", task.artifact);
    addFact(facts, "记录更新", stamp(task.updated_at));
    addFact(facts, "步骤提交", task.sealed ? "已结束步骤提交" : "Agent 可继续添加步骤");
    container.append(facts);
    container.append(el("p", "“步骤完成”表示工具记录的操作已完成；业务测试是否通过，需要查看测试输出与明确的成功判据。断线后的未知状态需要重新查询。", "advice"));
    const timeline = el("ol", null, "timeline");
    for (const step of task.steps || []) {
      const item = el("li", null, "step " + tone(step.state));
      const top = el("div", null, "row-top");
      top.append(el("h3", step.label || step.id), badge(step.state));
      item.append(top, el("p", step.method || ""), el("p", "观测时间 · " + stamp(step.observed_at)));
      if (step.last_confirmed_state && ["unknown", "needs_attention"].includes(step.state)) {
        item.append(el("p", "最后确认的状态：" + stateName(step.last_confirmed_state) + "；当前远端结果待确认。"));
      }
      if (step.error) item.append(el("pre", `${step.error.code || "错误"}: ${step.error.message || JSON.stringify(step.error)}`));
      const details = step.result || step.observation;
      if (details) {
        const disclosure = el("details");
        disclosure.append(el("summary", "查看操作结果", "small"), el("pre", JSON.stringify(details, null, 2)));
        item.append(disclosure);
      }
      const ref = logReference(step.resource);
      if (ref) item.append(button("查看日志", () => chooseLog(ref, step.label || step.id)));
      timeline.append(item);
    }
    container.append(timeline.children.length ? timeline : empty("任务已创建，等待 Agent 记录执行步骤。"));
    const hasJob = (task.steps || []).some((step) => step.resource?.kind === "job");
    const canCancel = (task.steps || []).some((step) => step.resource?.kind === "job" && !["succeeded", "failed", "cancelled"].includes(step.state));
    $("refresh-task").hidden = !hasJob;
    $("cancel-task").hidden = !canCancel;
  }

  function renderActivities(records, containerId, kind) {
    if (!changed(containerId, records)) return;
    const container = $(containerId);
    container.replaceChildren();
    const visible = records.filter((item) => !["task", "target_inspection"].includes(item.kind)).slice(0, 30);
    if (!visible.length) container.append(empty(kind === "session" ? "暂无终端会话。" : "暂无操作记录。"));
    for (const record of visible) {
      const row = el("div", null, "activity-row");
      const info = el("div");
      info.append(el("strong", `${record.target || "本地"} · ${record.kind || kind}`), el("p", `${short(record.id)} · ${stamp(record.updated_at || record.created_at)}`));
      const status = el("div", null, "activity-status");
      status.append(badge(record.state, record.kind === "job_reference" && record.state === "succeeded" ? "提交已记录" : undefined));
      if (record.kind !== "job_reference") status.append(button("日志", () => chooseLog({kind, id: record.id}, `${record.target || ""} / ${short(record.id)}`)));
      row.append(info, status);
      container.append(row);
    }
  }

  async function refreshTask(remote = false) {
    if (!selectedTask) return;
    const requested = selectedTask;
    try {
      const task = await api("tasks/" + encodeURIComponent(requested) + (remote ? "/refresh" : ""), remote ? "POST" : "GET");
      if (selectedTask === requested) renderTask(task);
    } catch (error) { notice(error.message); }
  }

  async function refreshOverview() {
    if (overviewBusy || !token) return;
    overviewBusy = true;
    try {
      snapshot = await api("overview");
      $("connection-state").textContent = "本地管理器已连接";
      $("connection-dot").className = "dot online";
      $("server-version").textContent = "v" + (snapshot.server.version || "—");
      $("last-update").textContent = "本地更新 · " + new Date().toLocaleTimeString("zh-CN", {hour12: false});
      $("count-targets").textContent = snapshot.targets.length;
      $("count-running").textContent = snapshot.tasks.filter((task) => ["running", "pending"].includes(task.state)).length;
      $("count-attention").textContent = snapshot.tasks.filter((task) => ["failed", "unknown", "needs_attention"].includes(task.state)).length;
      $("count-sessions").textContent = snapshot.counts?.active_sessions ?? snapshot.sessions.filter((session) => session.state === "open").length;
      $("task-list-scope").textContent = `显示最近 ${snapshot.tasks.length} 条任务，最多 ${snapshot.limits?.tasks || 100} 条；选择查看步骤。`;
      $("activity-scope").textContent = `操作显示最近 ${Math.min(30, snapshot.operations.length)} / ${snapshot.counts?.operations_total ?? snapshot.operations.length} 条；会话显示最近 ${Math.min(30, snapshot.sessions.length)} / ${snapshot.counts?.sessions_total ?? snapshot.sessions.length} 条。`;
      renderTargets(snapshot.targets);
      renderTasks(snapshot.tasks);
      renderActivities(snapshot.operations, "operations", "operation");
      renderActivities(snapshot.sessions, "sessions", "session");
      await refreshTask();
    } catch (error) {
      $("connection-state").textContent = "状态读取中断，保留上次记录";
      $("connection-dot").className = "dot offline";
      notice(error.message);
    } finally { overviewBusy = false; }
  }

  async function chooseLog(reference, label) {
    selectedLog = reference;
    offset = 0;
    logGeneration += 1;
    $("log-output").textContent = "";
    $("log-description").textContent = label;
    $("log-stream").value = "stdout";
    $("log-stream").disabled = reference.kind === "session";
    $("follow-log").checked = false;
    $("load-log").disabled = false;
    $("log-note").textContent = reference.kind === "job" ? "远端日志按需读取。勾选“持续读取”后，每 3 秒查询远端；不会重启或重发任务。" : "读取本地已保存的输出。日志可能已轮转，缺失区间会明确提示。";
    await loadLog();
    $("log-output").scrollIntoView({behavior: "smooth", block: "nearest"});
  }

  async function loadLog() {
    if (!selectedLog || logBusy) return;
    logBusy = true;
    const generation = logGeneration;
    $("load-log").disabled = true;
    try {
      const query = new URLSearchParams({...selectedLog, offset: String(offset), limit: "32768", stream: $("log-stream").value});
      const log = await api("logs?" + query.toString());
      if (generation !== logGeneration) return;
      let prefix = "";
      if (log.gap || (typeof log.offset === "number" && log.offset > offset)) {
        prefix = `\n[日志已轮转：请求位置 ${offset}，可用输出从 ${log.offset ?? log.start_offset ?? "较新位置"} 开始；中间内容未保留。]\n`;
      } else if (log.log_truncated && offset === 0) {
        prefix = "\n[此记录的日志曾触及容量限制，历史输出可能不完整。]\n";
      }
      const output = $("log-output");
      output.textContent += prefix + (log.data || "");
      if (output.textContent.length > 600000) output.textContent = "[为保持页面响应，仅显示最近 600000 个字符。]\n" + output.textContent.slice(-600000);
      offset = log.next_offset ?? offset;
      $("load-log").textContent = log.eof ? "检查新输出" : "读取下一页";
      if ($("follow-log").checked) output.scrollTop = output.scrollHeight;
    } catch (error) { notice(error.message); $("follow-log").checked = false; }
    finally {
      logBusy = false;
      $("load-log").disabled = !selectedLog;
      if (generation !== logGeneration && selectedLog) queueMicrotask(loadLog);
    }
  }

  $("refresh-overview").addEventListener("click", async () => { notice(); await refreshOverview(); });
  $("refresh-task").addEventListener("click", async () => {
    $("refresh-task").disabled = true;
    try { await refreshTask(true); await refreshOverview(); }
    finally { $("refresh-task").disabled = false; }
  });
  $("cancel-task").addEventListener("click", async () => {
    if (!selectedTask || !confirm("取消会请求停止本任务中仍活动的受管远端作业。它不会回滚已部署的文件，也不会终止其他终端中的程序。继续取消？")) return;
    $("cancel-task").disabled = true;
    try {
      await api("tasks/" + encodeURIComponent(selectedTask) + "/cancel", "POST");
      await refreshOverview();
    } catch (error) { notice(error.message); }
    finally { $("cancel-task").disabled = false; }
  });
  $("load-log").addEventListener("click", loadLog);
  $("log-stream").addEventListener("change", async () => {
    offset = 0; logGeneration += 1; $("log-output").textContent = ""; await loadLog();
  });
  $("logout").addEventListener("click", () => {
    try { sessionStorage.removeItem(storageKey); } catch { /* The in-memory token is still cleared. */ }
    token = "";
    location.reload();
  });

  if (!token) {
    $("connection-state").textContent = "控制台已锁定";
    notice("请让 Agent 运行 rmg ui，使用它返回的本地链接打开控制台。链接中的访问凭据仅保留在此标签页，不会写入日志或地址栏。");
  } else {
    refreshOverview();
    setInterval(async () => {
      await refreshOverview();
      if ($("follow-log").checked) await loadLog();
    }, 3000);
  }
})();
