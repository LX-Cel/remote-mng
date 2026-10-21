"use strict";

/**
 * Real-browser regression for the developer console, against a fresh daemon.
 *
 * Start the daemon separately with an empty, disposable --home. Then provide:
 *   RMG_UI_URL                rmg ui URL, including its access fragment
 *   RMG_SMOKE_HOME            exact disposable home returned by /ui/api/admin
 *   RMG_PLAYWRIGHT_MODULE     optional installed Playwright module path
 *   RMG_CHROME_PATH           optional Chromium/Chrome executable path
 *
 * No package download or daemon startup happens here. The script accepts only
 * a loopback UI and refuses a nonempty home. Remote action routes are blocked;
 * devices use loopback addresses and unused ports, and no connection is opened.
 * Tokens and synthetic passwords remain in memory and are never printed.
 */

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const net = require("node:net");
const path = require("node:path");

const completed = [];
let stage = "validate environment";
let browser;
let page;
let homeVerified = false;
const devices = [];
const templates = [];
const failures = [];
const blocked = [];
const passwords = [];

function check(condition, message) {
  assert.ok(condition, message);
}

function containsPassword(text) {
  // The unique ASCII portion still matches if JSON escapes quotes or Unicode.
  return passwords.some(value => text.includes(value.slice(0, 40)));
}

function safeErrorMessage(error) {
  let message = String(error?.message || "Unknown error");
  const sensitive = [...passwords];
  const suppliedUrl = process.env.RMG_UI_URL || "";
  if (suppliedUrl) {
    sensitive.push(suppliedUrl);
    try {
      const url = new URL(suppliedUrl);
      const fragment = url.hash.slice(1);
      sensitive.push(url.hash, fragment);
      if (fragment) sensitive.push(fragment.startsWith("token=") ? new URLSearchParams(fragment).get("token") : decodeURIComponent(fragment));
    } catch { /* Invalid URL diagnostics must still avoid echoing input. */ }
  }
  for (const value of sensitive.filter(Boolean).sort((a, b) => b.length - a.length)) {
    for (const form of new Set([value, encodeURIComponent(value), JSON.stringify(value).slice(1, -1)])) message = message.split(form).join("[redacted]");
  }
  // Retain useful locator/assertion diagnostics, while bounding output and
  // suppressing access fragments that a browser might have reformatted.
  return message.replace(/([#?&]token=)[^\s"']+/g, "$1[redacted]").slice(0, 3000);
}

async function unusedPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  return port;
}

async function api(action, method = "GET", body = {}) {
  return page.evaluate(({action, method, body}) => window.rmgDashboard.api(action, method, body), {action, method, body});
}

function card(name) {
  return page.locator("#device-list article").filter({has: page.getByRole("heading", {name, exact: true})});
}

function templateCard(name) {
  return page.locator("#template-list article").filter({has: page.getByRole("heading", {name, exact: true})});
}

async function navigate(view) {
  await page.locator(`[data-view="${view}"]`).click();
  await page.locator(`[data-page="${view}"]`).waitFor({state: "visible"});
}

async function previewDevice() {
  await page.locator('#device-form button[type="submit"]:not(#device-test-saved)').click();
  await page.locator("#change-dialog[open]").waitFor();
  check(await page.locator("#device-password").inputValue() === "", "Password input was not cleared after preview");
  const preview = await page.locator("#change-dialog").textContent();
  check(!containsPassword(preview), "A password appeared in the change preview");
}

async function applyChange() {
  await page.locator("#change-apply").click();
  await page.locator("#change-dialog").waitFor({state: "hidden"});
}

async function saveDevice(name) {
  const sent = page.waitForRequest(request => {
    if (request.method() !== "POST" || new URL(request.url()).pathname !== "/ui/api/admin/target.save") return false;
    return request.postDataJSON().dry_run === false;
  });
  await applyChange();
  const request = await sent;
  await card(name).waitFor({state: "visible"});
  return request.postDataJSON();
}

async function assertNoSecretsOnDisk(home) {
  let count = 0;
  async function walk(directory) {
    for (const entry of await fs.readdir(directory, {withFileTypes: true})) {
      const file = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) await walk(file);
      else if (entry.isFile()) {
        check(++count <= 500, "Disposable smoke home unexpectedly contains too many files");
        // Windows keeps the zero-byte daemon lock exclusively open. There is
        // no content to scan in an empty file, and reading that lock is EBUSY.
        if ((await fs.stat(file)).size === 0) continue;
        let content;
        try { content = await fs.readFile(file); }
        catch (error) { throw new Error("Cannot inspect disposable file " + path.relative(home, file) + ": " + error.code); }
        check(!containsPassword(content.toString("utf8")), "A password was written to the disposable home");
      }
    }
  }
  await walk(home);
}

async function assertSafeState(home) {
  const exported = JSON.stringify(await api("admin/config.export", "POST"));
  const storage = await page.evaluate(() => JSON.stringify({local: {...localStorage}, session: {...sessionStorage}}));
  check(!containsPassword(exported), "A password appeared in a configuration export");
  check(!containsPassword(storage), "A password appeared in browser storage");
  await assertNoSecretsOnDisk(home);
}

async function removeCreatedResources() {
  if (!homeVerified || !page || page.isClosed()) return;
  for (const [action, names, field] of [["template.delete", templates, "operation_templates"], ["target.delete", devices, "targets"]]) {
    for (const name of names) {
      const state = await api("admin");
      if (state[field].some(item => item.name === name)) {
        await api("admin/" + action, "POST", {name, expected_revision: state.revision, dry_run: false});
      }
    }
  }
  // The home was verified empty before mutation, so these are test credentials.
  for (const item of (await api("admin")).credentials) await api("admin/credential.delete", "POST", {name: item.name});
}

async function main() {
  const ui = new URL(process.env.RMG_UI_URL || "");
  check(ui.protocol === "http:" && ["127.0.0.1", "[::1]", "localhost"].includes(ui.hostname), "RMG_UI_URL must be an HTTP loopback URL");
  check(!ui.username && !ui.password && ui.hash, "RMG_UI_URL must contain the local access fragment");
  const expectedHome = process.env.RMG_SMOKE_HOME;
  check(expectedHome && path.isAbsolute(expectedHome), "RMG_SMOKE_HOME must name an absolute disposable home");
  const {chromium} = require(process.env.RMG_PLAYWRIGHT_MODULE || "playwright");
  browser = await chromium.launch({headless: true, ...(process.env.RMG_CHROME_PATH ? {executablePath: process.env.RMG_CHROME_PATH} : {})});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on("pageerror", () => failures.push("pageerror"));
  await context.route("**/*", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const remoteAction = request.method() === "POST" && (
      /^\/ui\/api\/targets\//.test(url.pathname) ||
      /^\/ui\/api\/sessions\//.test(url.pathname) ||
      /^\/ui\/api\/profiles\//.test(url.pathname) ||
      /^\/ui\/api\/admin\/(hostkey\.|template\.run)/.test(url.pathname) ||
      /^\/ui\/api\/update\//.test(url.pathname)
    );
    if (url.origin !== ui.origin || remoteAction) {
      blocked.push("unexpected network or remote action");
      await route.abort("blockedbyclient");
    } else await route.continue();
  });

  stage = "empty state";
  await page.goto(ui.href);
  await page.locator("#count-targets").filter({hasText: "0"}).waitFor();
  let state = await api("admin");
  check(path.resolve(state.paths.home) === path.resolve(expectedHome), "The daemon home does not match RMG_SMOKE_HOME");
  check(state.targets.length === 0 && state.operation_templates.length === 0 && state.credentials.length === 0, "The smoke home must have no devices, operations, or credentials");
  homeVerified = true;
  const overview = await api("overview");
  check(!overview.tasks?.length && !overview.sessions?.length && !overview.operations?.length, "The smoke home must have no previous activity");
  await navigate("devices");
  await page.locator("#device-list-empty").waitFor({state: "visible"});
  completed.push("empty state");

  const suffix = Date.now().toString(36) + "-" + crypto.randomBytes(4).toString("hex");
  const ssh = "ux-ssh-" + suffix;
  const telnet = "ux-telnet-" + suffix;
  const operation = "ux-operation-" + suffix;
  passwords.push("ui-smoke-first-" + crypto.randomBytes(16).toString("hex") + '-"中文', "ui-smoke-replace-" + crypto.randomBytes(16).toString("hex"), "ui-smoke-telnet-" + crypto.randomBytes(16).toString("hex"));

  stage = "cancel password preview";
  await page.locator("#add-device").click();
  await page.locator("#device-name").fill(ssh);
  await page.locator("#device-host").fill("127.0.0.1");
  await page.locator("#device-port").fill(String(await unusedPort()));
  await page.locator("#device-username").fill("smoke-user");
  await page.locator("#device-auth").selectOption("password");
  await page.locator("#device-password").fill(passwords[0]);
  await previewDevice();
  await page.locator("#change-dismiss").click();
  state = await api("admin");
  check(state.targets.length === 0 && state.credentials.length === 0, "Cancelled preview changed devices or credentials");
  await assertSafeState(expectedHome);
  completed.push("cancel preview stores no password");

  stage = "save SSH password device";
  devices.push(ssh);
  await page.locator("#device-password").fill(passwords[0]);
  await previewDevice();
  let sent = await saveDevice(ssh);
  check(sent.password === passwords[0], "The password save did not use the supplied password");
  state = await api("admin");
  const firstRef = state.targets.find(item => item.name === ssh).config.password_ref;
  check(firstRef && state.targets.find(item => item.name === ssh).authentication.state === "ready", "Saved device credentials are not ready");
  await card(ssh).getByRole("button", {name: "测试连接", exact: true}).waitFor();
  await card(ssh).getByRole("button", {name: "打开终端", exact: true}).waitFor();
  check((await card(ssh).textContent()).includes("尚未测试"), "An untested device was not clearly labelled");
  completed.push("direct SSH password save");

  stage = "device search";
  await page.locator("#device-search").fill("does-not-exist-" + suffix);
  check(await card(ssh).count() === 0, "Device search did not filter the list");
  await page.locator("#device-search").fill("SMOKE-USER");
  await card(ssh).waitFor({state: "visible"});
  await page.locator("#device-search").fill("");
  completed.push("device search");

  stage = "edit with blank password";
  await card(ssh).getByRole("button", {name: "编辑", exact: true}).click();
  check(await page.locator("#device-password").inputValue() === "", "Editing exposed the saved password");
  check((await page.locator("#device-password-status").textContent()).includes("可用"), "Current password availability was not explained");
  await previewDevice();
  sent = await saveDevice(ssh);
  check(!Object.hasOwn(sent, "password"), "Blank password editing sent a replacement password");
  state = await api("admin");
  check(state.targets.find(item => item.name === ssh).config.password_ref === firstRef, "Blank editing replaced the password reference");
  completed.push("blank edit preserves password");

  stage = "replace password";
  await card(ssh).getByRole("button", {name: "编辑", exact: true}).click();
  await page.locator("#device-password").fill(passwords[1]);
  await previewDevice();
  sent = await saveDevice(ssh);
  check(sent.password === passwords[1], "The replacement password was not submitted");
  state = await api("admin");
  check(state.targets.find(item => item.name === ssh).config.password_ref === firstRef && state.credentials.length === 1, "Password replacement created another reference");
  await assertSafeState(expectedHome);
  completed.push("password replacement without exposure");

  stage = "Telnet simple login";
  devices.push(telnet);
  await page.locator("#add-device").click();
  await page.locator("#device-name").fill(telnet);
  await page.locator("#device-protocol").selectOption("telnet");
  await page.locator("#device-host").fill("127.0.0.1");
  await page.locator("#device-port").fill(String(await unusedPort()));
  await page.locator("#device-username").fill("telnet-user");
  await page.locator("#device-auth").selectOption("password");
  await page.locator("#device-password").fill(passwords[2]);
  await page.locator("#telnet-login-mode").selectOption("user-password");
  await previewDevice();
  await saveDevice(telnet);
  state = await api("admin");
  const telnetConfig = state.targets.find(item => item.name === telnet).config;
  check(telnetConfig.login_steps.length === 2, "Telnet login did not save the two simple steps");
  check(telnetConfig.login_steps[0].send === "telnet-user" && telnetConfig.login_steps[1].send_ref === telnetConfig.password_ref && !Object.hasOwn(telnetConfig.login_steps[1], "send"), "Telnet password was not represented by its temporary reference");
  await card(telnet).getByRole("button", {name: "编辑", exact: true}).click();
  check(await page.locator("#telnet-login-mode").inputValue() === "user-password", "Telnet simple login did not reopen in the simple form");
  await page.locator("#close-device-editor").click();
  completed.push("Telnet simple login configuration");

  stage = "operation CRUD";
  await navigate("operations");
  await page.locator("#add-template").click();
  await page.locator("#template-name").fill(operation);
  await page.locator("#template-kind").selectOption("exec");
  await page.locator("#template-description").fill("Browser smoke configuration only");
  await page.locator("#template-command").fill("printf 'not executed by usability smoke\\n'");
  await page.locator("#template-timeout").fill("12");
  templates.push(operation);
  await page.locator('#template-form button[type="submit"]').click();
  await page.locator("#change-dialog[open]").waitFor();
  await applyChange();
  await templateCard(operation).waitFor({state: "visible"});
  await templateCard(operation).getByRole("button", {name: "编辑", exact: true}).click();
  await page.locator("#template-description").fill("Updated configuration only");
  await page.locator('#template-form button[type="submit"]').click();
  await page.locator("#change-dialog[open]").waitFor();
  await applyChange();
  await templateCard(operation).getByText("Updated configuration only", {exact: true}).waitFor();
  state = await api("admin");
  check(state.operation_templates.find(item => item.name === operation).config.timeout === 12, "Operation editing lost the timeout");
  await templateCard(operation).getByRole("button", {name: "删除", exact: true}).click();
  await page.locator("#change-dialog[open]").waitFor();
  await applyChange();
  await templateCard(operation).waitFor({state: "detached"});
  completed.push("separate operations entry and CRUD");

  stage = "mobile layouts";
  await page.setViewportSize({width: 390, height: 844});
  for (const view of ["overview", "devices", "tasks", "sessions", "operations", "settings"]) {
    await navigate(view);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "A console page overflows the mobile viewport");
  }
  await navigate("devices");
  await card(ssh).getByRole("button", {name: "编辑", exact: true}).click();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "The device editor overflows the mobile viewport");
  await page.locator("#close-device-editor").click();
  completed.push("mobile pages and device editor fit");

  stage = "final safety checks";
  await assertSafeState(expectedHome);
  check(!failures.length, "The browser reported a page error");
  check(!blocked.length, "The console attempted an unexpected network or remote action");
  completed.push("no page errors, remote actions, or password persistence");
}

(async () => {
  let error;
  try { await main(); }
  catch (caught) { error = caught; }
  try {
    await removeCreatedResources();
    if (homeVerified) {
      const state = await api("admin");
      check(state.targets.length === 0 && state.operation_templates.length === 0 && state.credentials.length === 0, "Disposable smoke resources were not removed");
    }
  } catch (cleanupError) {
    if (!error) { error = cleanupError; stage = "cleanup"; }
  } finally {
    if (browser) await browser.close();
  }
  // Playwright call logs may include input values or navigation URLs. Redact
  // both before exposing enough detail to diagnose a repeatable regression.
  console.log(JSON.stringify({ok: !error, completed, ...(error ? {failedAt: stage, errorType: error.name, message: safeErrorMessage(error)} : {})}));
  process.exitCode = error ? 1 : 0;
})();
