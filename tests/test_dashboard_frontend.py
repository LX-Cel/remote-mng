"""Exercise the browser update flows with the real scripts and an isolated DOM adapter."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


NODE = shutil.which("node")
ASSETS = Path(__file__).parents[1] / "src" / "remote_mng" / "assets" / "dashboard"
pytestmark = pytest.mark.skipif(not NODE, reason="Node is required for the isolated browser-script checks")

HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.textContent = ''; this.handlers = {}; this.hidden = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, action) { this.handlers[name] = action; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  scrollIntoView() {}
}
const nodes = new Map();
const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Element('div')); return nodes.get(id); }, createElement(tag) { return new Element(tag); }};
const stored = new Map();
const intervals = [];
const requests = [];
const navigations = [];
const replacements = [];
const location = {hash: '#token=local-access', pathname: '/ui/', search: '', href: 'http://127.0.0.1:1234/ui/#token=local-access', assign(value) { navigations.push(value); }};
const context = {document, location, URL, URLSearchParams, Date, Map, Set, queueMicrotask,
  sessionStorage: {setItem(k, v) { stored.set(k, v); }, getItem(k) { return stored.get(k); }, removeItem(k) { stored.delete(k); }},
  history: {replaceState(_state, _title, path) { replacements.push(path); }},
  setInterval(fn) { intervals.push(fn); }, confirm: () => false,
  fetch: async (path, options) => { requests.push({path, options}); return {ok: true, status: 200, json: async () => ({ok: true, result: response(path, options)})}; }};
function textTree(node) { return node.textContent + node.children.map(textTree).join(''); }
async function flush() { for (let i = 0; i < 6; i++) await new Promise(setImmediate); }
function run(path) { vm.runInNewContext(fs.readFileSync(path, 'utf8'), context); }
"""


def run_node(script):
    completed = subprocess.run([NODE, "-e", HARNESS + script], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_update_card_checks_only_on_click_and_starts_pinned_plan():
    run_node(r"""
const state = {installation: {kind: 'uv_tool', current_version: '0.3.0', previous_version: '0.2.0', origin: {url: 'credential-bearing-value'}}, components: {cli: {version: '0.3.0'}, daemon: {version: '0.3.0'}, skill: {version: '0.3.0'}}, history: []};
const plan = {id: 'plan-checked', state: 'ready', current_version: '0.3.0', target_version: '0.4.0', release: {notes: '<script>malicious()</script>'}};
function response(path, options) {
  if (path.endsWith('/overview')) return {server: {version: '0.3.0'}, targets: [], tasks: [], sessions: [], operations: []};
  if (path === '/ui/api/update') return state;
  if (path === '/ui/api/update/check') return plan;
  if (path === '/ui/api/update/start' || path === '/ui/api/update/rollback') return {id: 'update-1', state: 'queued', monitor_url: 'http://127.0.0.1:4567/update/#token=monitor-access'};
  throw Error('Unexpected route ' + path);
}
(async () => {
  run(SCRIPT_PATH); await flush();
  assert(requests.every(item => item.options.method === 'GET'));
  assert(requests.some(item => item.path === '/ui/api/update'));
  assert.equal(document.getElementById('install-update').hidden, true);
  assert(!textTree(document.getElementById('update-components')).includes('credential-bearing-value'));
  await document.getElementById('check-update').handlers.click(); await flush();
  assert.equal(requests.filter(item => item.path.endsWith('/check')).length, 1);
  assert.equal(document.getElementById('install-update').hidden, false);
  assert(textTree(document.getElementById('update-plan')).includes('<script>malicious()</script>'));
  assert.equal(navigations.length, 0);
  await document.getElementById('install-update').handlers.click(); await flush();
  const start = requests.find(item => item.path.endsWith('/start'));
  assert.deepEqual(JSON.parse(start.options.body), {plan_id: 'plan-checked'});
  assert.equal(navigations[0], 'http://127.0.0.1:4567/update/#token=monitor-access');
  const before = requests.length;
  await document.getElementById('rollback-update').handlers.click(); await flush();
  assert.equal(requests.length, before);
  context.confirm = () => true;
  await document.getElementById('rollback-update').handlers.click(); await flush();
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {version: '0.2.0'});
  assert(replacements.includes('/ui/'));
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SCRIPT_PATH", json.dumps(str(ASSETS / "app.js"))))


def test_independent_monitor_uses_local_read_only_poll_and_hides_fragment():
    run_node(r"""
location.pathname = '/update/'; location.href = 'http://127.0.0.1:4567/update/#token=local-access';
let record = {id: 'update-1', state: 'verifying', plan: {current_version: '0.3.0', target_version: '0.4.0'}, events: [{state: 'verifying', message: '<script>inert()</script>'}]};
function response(path, options) { assert.equal(path, '/update/api/status'); assert.equal(options.headers.Authorization, 'Bearer local-access'); return record; }
(async () => {
  run(SCRIPT_PATH); await flush();
  assert.equal(requests.length, 1);
  assert(replacements.includes('/update/'));
  assert(textTree(document.getElementById('monitor-events')).includes('<script>inert()</script>'));
  assert(textTree(document.getElementById('monitor-facts')).includes('0.3.0'));
  assert(textTree(document.getElementById('monitor-facts')).includes('0.4.0'));
  assert.equal(document.getElementById('monitor-result-panel').hidden, true);
  record = {...record, state: 'succeeded', console_url: 'http://127.0.0.1:8901/ui/#token=new-console-access'};
  intervals[0](); await flush();
  assert.equal(document.getElementById('monitor-result-panel').hidden, false);
  assert.equal(document.getElementById('monitor-console').href, record.console_url);
  assert(!textTree(document.getElementById('monitor-result-panel')).includes('new-console-access'));
  const count = requests.length; intervals[0](); await flush(); assert.equal(requests.length, count);
  record = {...record, console_url: 'https://evil.invalid/ui/#token=should-never-follow'};
  await document.getElementById('monitor-refresh').handlers.click(); await flush();
  assert.equal(document.getElementById('monitor-console').hidden, true);
  assert.equal(document.getElementById('monitor-console').href, undefined);
  assert(requests.every(item => !item.options.method || item.options.method === 'GET'));
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SCRIPT_PATH", json.dumps(str(ASSETS / "update.js"))))


def test_delayed_monitor_is_accepted_and_followed_using_the_existing_update_id():
    run_node(r"""
const plan = {id: 'checked-plan', state: 'ready', current_version: '0.3.0', target_version: '0.4.0'};
const state = {installation: {kind: 'standalone', current_version: '0.3.0'}, latest_plan: plan, history: []};
function response(path) {
  if (path.endsWith('/overview')) return {server: {}, targets: [], tasks: [], sessions: [], operations: []};
  if (path === '/ui/api/update') return state;
  if (path.endsWith('/start')) return {id: 'accepted-update', state: 'queued', plan};
  throw Error('Unexpected request ' + path);
}
(async () => {
  run(SCRIPT_PATH); await flush();
  await document.getElementById('install-update').handlers.click(); await flush();
  assert.equal(navigations.length, 0);
  assert(document.getElementById('notice').textContent.includes('accepted-update'));
  assert(document.getElementById('notice').textContent.includes('请求已受理'));
  assert(!document.getElementById('notice').textContent.includes('失败'));
  state.history.push({id: 'accepted-update', state: 'preparing', plan, monitor_url: 'http://127.0.0.1:4567/update/#token=ready-now'});
  intervals[0](); await flush();
  assert.equal(navigations[0], state.history[0].monitor_url);
  assert.equal(requests.filter(item => item.path.endsWith('/start')).length, 1);
  assert(textTree(document.getElementById('update-history-list')).includes('0.4.0'));
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SCRIPT_PATH", json.dumps(str(ASSETS / "app.js"))))


def test_monitor_handoff_only_follows_distinct_authenticated_loopback_endpoint():
    run_node(r"""
location.pathname = '/update/'; location.href = 'http://127.0.0.1:4567/update/#token=local-access';
let record = {id: 'update-1', state: 'handoff', monitor_url: location.href};
function response() { return record; }
(async () => {
  run(SCRIPT_PATH); await flush(); assert.equal(navigations.length, 0);
  for (const value of ['https://evil.invalid/update/#token=bad', 'javascript:alert(1)', 'http://user:pass@127.0.0.1:9876/update/#token=bad', 'http://127.0.0.1:9876/rpc#token=bad', 'http://127.0.0.1:9876/update/?next=external#token=bad', 'http://127.0.0.1:9876/update/#other=bad']) {
    record.monitor_url = value;
    await document.getElementById('monitor-refresh').handlers.click(); await flush();
    assert.equal(navigations.length, 0);
  }
  record.monitor_url = 'http://127.0.0.1:9876/update/#token=new-monitor-access';
  await document.getElementById('monitor-refresh').handlers.click(); await flush();
  assert.equal(navigations.length, 1);
  assert.equal(navigations[0], record.monitor_url);
  assert(!textTree(document.getElementById('monitor-facts')).includes('new-monitor-access'));
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SCRIPT_PATH", json.dumps(str(ASSETS / "update.js"))))


def test_dashboard_recovery_requires_confirmation_and_only_submits_recorded_id():
    run_node(r"""
const id = 'a'.repeat(32);
const state = {installation: {kind: 'standalone'}, pending_recovery: {id, state: 'interrupted'}, history: [{id, state: 'interrupted', plan: {current_version: '0.3.0', target_version: '0.4.0'}, last_state: 'activating'}]};
function response(path) {
  if (path.endsWith('/overview')) return {server: {}, targets: [], tasks: [], sessions: [], operations: []};
  if (path === '/ui/api/update') return state;
  if (path === '/ui/api/update/recover') return {id, state: 'recovering'};
  throw Error('Unexpected request ' + path);
}
function findButton(node, text) { return node.tagName === 'button' && node.textContent === text ? node : node.children.map(child => findButton(child, text)).find(Boolean); }
(async () => {
  run(SCRIPT_PATH); await flush();
  assert.equal(document.getElementById('update-history').open, true);
  const button = findButton(document.getElementById('update-history-list'), '恢复此更新');
  assert(button);
  const count = requests.length;
  await button.handlers.click(); await flush(); assert.equal(requests.length, count);
  context.confirm = () => true;
  await button.handlers.click(); await flush();
  const call = requests.find(item => item.path === '/ui/api/update/recover');
  assert.deepEqual(JSON.parse(call.options.body), {update_id: id});
  assert.equal(requests.filter(item => item.path === '/ui/api/update/recover').length, 1);
  assert.equal(navigations.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SCRIPT_PATH", json.dumps(str(ASSETS / "app.js"))))


def test_read_only_monitor_copies_recovery_request_without_submitting_it():
    run_node(r"""
location.pathname = '/update/'; location.href = 'http://127.0.0.1:4567/update/#token=local-access';
const id = 'a'.repeat(32);
const record = {id, state: 'failed', recovery: {state: 'needs_attention'}, plan: {current_version: '0.3.0', home: '/state/selected'}};
const copied = [];
context.navigator = {clipboard: {async writeText(value) { copied.push(value); }}};
function response() { return record; }
(async () => {
  run(SCRIPT_PATH); await flush();
  assert.equal(document.getElementById('monitor-recovery').hidden, false);
  assert.equal(document.getElementById('monitor-recovery-command').textContent, 'rmg update recover --id ' + id);
  const count = requests.length;
  await document.getElementById('monitor-copy-recovery').handlers.click(); await flush();
  assert.equal(requests.length, count);
  assert.equal(copied.length, 1);
  assert(copied[0].includes(id) && copied[0].includes('/state/selected'));
  assert(!copied[0].includes('local-access'));
  record.recovery.state = 'restored';
  await document.getElementById('monitor-refresh').handlers.click(); await flush();
  assert.equal(document.getElementById('monitor-recovery').hidden, true);
  assert.equal(document.getElementById('monitor-recovery-command').textContent, '');
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SCRIPT_PATH", json.dumps(str(ASSETS / "update.js"))))
