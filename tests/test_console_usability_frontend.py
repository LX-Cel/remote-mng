"""Exercise the shipped console script's device journeys without remote hosts."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


NODE = shutil.which("node")
SCRIPT = Path(__file__).parents[1] / "src/remote_mng/assets/dashboard/console.js"
pytestmark = pytest.mark.skipif(not NODE, reason="Node is required for console interaction checks")

HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {webcrypto} = require('node:crypto');
class Element {
  constructor(tag, id='') { this.tagName=tag; this.id=id; this.children=[]; this.handlers={}; this.hidden=false; this.disabled=false; this.textContent=''; this._value=''; this.type=''; this.classList={toggle(){}}; }
  get value() { return this._value; }
  set value(value) { const str=String(value); this._value=this.tagName==='select' && !this.options.some(o=>o.value===str) ? '' : str; }
  get options() { return this.children.filter(c=>c.tagName==='option'); }
  append(...items) { this.children.push(...items); if(this.tagName==='select' && !this.options.some(o=>o.value===this._value))this._value=this.options[0]?.value||''; }
  replaceChildren(...items) { this.children=[]; this._value=this.tagName==='select'?'':this._value; this.append(...items); }
  addEventListener(event, handler) { (this.handlers[event] ||= []).push(handler); }
  setAttribute(name,value) { this[name]=value; }
  getAttribute(name) { return this[name]; }
  removeAttribute(name) { delete this[name]; }
  showModal() { this.open=true; }
  close() { this.open=false; }
  focus() {}
  scrollIntoView() {}
  async fire(event, extra={}) { for(const handler of this.handlers[event]||[])await handler({currentTarget:this,target:this,preventDefault(){},...extra}); }
  click() { return this.fire('click'); }
}
const selectIds=new Set(['manage-target','session-target','session-profile']);
const elements=new Map();
const document={handlers:{}, activeElement:null,
 getElementById(id){if(!elements.has(id))elements.set(id,new Element(selectIds.has(id)?'select':'div',id));return elements.get(id);},
 createElement(tag){return new Element(tag);}, createTextNode(text){const n=new Element('text');n.textContent=text;return n;},
 querySelectorAll(){return [];}, addEventListener(event,handler){(this.handlers[event] ||= []).push(handler);},
 async emit(event,detail){for(const handler of this.handlers[event]||[])await handler({detail});}};
const $=id=>document.getElementById(id);
const calls=[]; const notices=[];
let configState={revision:'rev-1',targets:[],profiles:[],operation_templates:[],credentials:[],settings:{},paths:{},diagnostics:{checks:[]}};
let customApi=null;
async function api(path,method='GET',params={}) {
 calls.push({path,method,params:structuredClone(params)});
 if(customApi){const custom=customApi(path,method,params);if(custom!==undefined)return custom;}
 if(path==='admin')return structuredClone(configState);
 if(path==='admin/target.save'){
   const config=structuredClone(params.config); if(params.password)config.password_ref='memory:1234567890abcdef1234:device-auto';
   for(const steps of [config.login_steps,config.login_flow?.steps])for(const step of steps||[])if(step.send_ref==='__device_password__')step.send_ref=config.password_ref;
   const result={name:params.name,revision:configState.revision,dry_run:params.dry_run,config,diff:[{field:'targets.'+params.name}]};
   if(!params.dry_run){configState={...configState,revision:'rev-2',targets:[{name:params.name,config,authentication:{state:'ready',references:config.password_ref?[{reference:config.password_ref,available:true}]:[]}}]};result.revision='rev-2';}
   return result;
 }
 if(path.endsWith('/check'))return {connection:{connected:true},checks:[],checked_at:1};
 throw Error('Unexpected route '+path);
}
const dashboard={api,notice:value=>notices.push(value),hasToken:()=>true,refreshOverview:async()=>{}};
const context={document,window:{rmgDashboard:dashboard,addEventListener(){}},crypto:webcrypto,structuredClone,Date,Map,Set,URL,URLSearchParams,
 setInterval(){},setTimeout,confirm:()=>false,Blob,console};
function textTree(node){return node.textContent+node.children.map(textTree).join('');}
function findText(node,tag,text){if(node.tagName===tag&&node.textContent===text)return node;for(const c of node.children){const found=findText(c,tag,text);if(found)return found;}}
async function flush(){for(let i=0;i<6;i++)await new Promise(setImmediate);}
function load(){vm.runInNewContext(fs.readFileSync(SCRIPT_PATH,'utf8'),context);}
async function submit(test=false){const button=$(test?'device-test-saved':'device-save');await $('device-form').fire('submit',{submitter:button});}
async function newDevice(){await $('add-device').click();$('device-name').value='lab';$('device-host').value='127.0.0.1';$('device-username').value='tester';}
"""


def run_node(script):
    source = HARNESS.replace("SCRIPT_PATH", json.dumps(str(SCRIPT))) + script
    completed = subprocess.run([NODE, "-e", source], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_password_preview_cancel_and_save_test_only_connect_after_confirm():
    run_node(r"""
(async()=>{
 load();await flush();await newDevice();$('device-password').value='PRIVATE_PASSWORD';
 await submit(true);
 assert.equal($('change-dialog').open,true);
 assert.equal($('device-password').value,'');
 assert(!textTree($('change-summary')).includes('PRIVATE_PASSWORD'));
 assert(!textTree($('change-preview')).includes('PRIVATE_PASSWORD'));
 assert.equal(calls.filter(c=>c.path.endsWith('/check')).length,0);
 assert.equal(calls.filter(c=>c.path==='admin/target.save'&&!c.params.dry_run).length,0);
 await $('change-cancel').click();
 assert.equal(configState.targets.length,0);
 $('device-password').value='PRIVATE_PASSWORD';await submit(true);await $('change-apply').click();
 const actions=calls.filter(c=>c.method==='POST');
 assert.deepEqual(actions.map(c=>c.path),['admin/target.save','admin/target.save','admin/target.save','targets/lab/check']);
 assert.equal(actions[1].params.dry_run,true);assert.equal(actions[2].params.dry_run,false);
 assert.equal(configState.targets[0].name,'lab');assert.equal($('device-editor-panel').hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_expired_password_is_required_and_reentry_preserves_advanced_telnet_route():
    run_node(r"""
(async()=>{
 const old='memory:1234567890abcdef1234:legacy';const independent='memory:1234567890abcdef1234:jump';
 configState.targets=[{name:'lab',config:{protocol:'telnet',host:'127.0.0.1',username:'tester',password_ref:old,
   login_steps:[{expect:'custom password:',send_ref:old},{expect:'next hop:',send_ref:independent}]},
   authentication:{state:'missing',references:[{reference:old,available:false}]}}];
 load();await flush();await document.emit('rmg:edit-target','lab');
 assert.equal($('device-auth').value,'password');assert.equal($('telnet-login-mode').value,'advanced');
 assert($('device-password').placeholder.includes('已失效'));
 await submit();assert($('device-error').textContent.includes('已失效'));
 assert.equal(calls.filter(c=>c.path==='admin/target.save').length,0);
 $('device-password').value='REPLACED_SECRET';await submit();
 const request=calls.find(c=>c.path==='admin/target.save').params;
 assert.equal(request.config.login_steps[0].expect,'custom password:');
 assert.equal(request.config.login_steps[0].send_ref,'__device_password__');
 assert.equal(request.config.login_steps[1].send_ref,independent);
 assert.equal(request.password,'REPLACED_SECRET');assert.equal($('device-password').value,'');
 await $('change-apply').click();
 assert.equal(configState.targets[0].config.login_steps[0].send_ref,configState.targets[0].config.password_ref);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_advanced_telnet_environment_password_and_flow_are_rebound_only_when_matching():
    run_node(r"""
(async()=>{
 configState.targets=[{name:'lab',config:{protocol:'telnet',host:'127.0.0.1',password_env:'OLD_PASSWORD',
  login_flow:{start:'password',steps:[{id:'password',expect:'Password:',send_env:'OLD_PASSWORD',next:'ready'},{id:'ready',expect:'ready>',finish:true}]}},
  authentication:{state:'ready',references:[{reference:'OLD_PASSWORD',available:true}]}}];
 load();await flush();await document.emit('rmg:edit-target','lab');
 $('device-auth').value='password';await $('device-auth').fire('change');$('device-password').value='NEW';await submit();
 const request=calls.find(c=>c.path==='admin/target.save').params;
 assert.equal(request.config.password_env,undefined);
 assert.equal(request.config.login_flow.steps[0].send_env,undefined);
 assert.equal(request.config.login_flow.steps[0].send_ref,'__device_password__');
 assert.equal(request.config.login_flow.steps[1].finish,true);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_terminal_navigation_waits_for_inflight_admin_refresh_before_selecting_device():
    run_node(r"""
(async()=>{
 const device=name=>({name,config:{protocol:'ssh',host:name+'.test'},authentication:{state:'ready'}});
 configState.targets=[device('first')];load();await flush();
 let resolve;customApi=path=>path==='admin'?new Promise(r=>{resolve=r;}):undefined;
 const refresh=$('refresh-admin').click();await flush();
 const prepare=document.emit('rmg:terminal-target','second');await flush();
 resolve({...configState,targets:[device('first'),device('second')]});await refresh;await prepare;await flush();
 assert.equal($('session-target').value,'second');
 assert.equal(calls.filter(c=>c.path==='admin').length,2);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_late_connection_result_never_relabels_a_different_device_and_telnet_has_no_ssh_fingerprint_action():
    run_node(r"""
(async()=>{
 configState.targets=['first','second'].map(name=>({name,config:{protocol:'telnet',host:name+'.test'},authentication:{state:'ready'}}));
 load();await flush();let firstResolve,secondResolve;
 customApi=path=>path==='targets/first/check'?new Promise(r=>{firstResolve=r;}):path==='targets/second/check'?new Promise(r=>{secondResolve=r;}):undefined;
 const cards=$('device-list').children;
 const first=findText(cards[0],'button','测试连接').click();await flush();
 const second=findText(cards[1],'button','测试连接').click();await flush();
 firstResolve({connection:{connected:true},checks:[]});await first;
 assert(!textTree($('device-action-result')).includes('设备检查 · first'));
 secondResolve({connection:{connected:false},checks:[{name:'connection',state:'fail',message:'Connection could not be verified',diagnostic:{stage:'tcp'}}]});await second;
 const text=textTree($('device-action-result'));
 assert(text.includes('设备检查 · second'));assert(text.includes('网络连接未建立'));
 assert(!findText($('device-action-result'),'button','核对服务器指纹'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_hidden_or_locked_password_fields_are_cleared_and_accessible_toggle_tracks_state():
    run_node(r"""
(async()=>{
 load();await flush();await newDevice();$('device-password').value='PRIVATE';
 await $('device-password-toggle').click();assert.equal($('device-password').type,'text');
 assert.equal($('device-password-toggle').getAttribute('aria-label'),'隐藏密码');
 $('device-auth').value='key';await $('device-auth').fire('change');assert.equal($('device-password').value,'');
 $('device-password').value='PRIVATE';await $('logout').click();assert.equal($('device-password').value,'');
 assert.equal($('device-editor-panel').hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_late_preview_response_after_lock_cannot_reopen_password_confirmation():
    run_node(r"""
(async()=>{
 load();await flush();await newDevice();$('device-password').value='PRIVATE';
 let resolve;customApi=path=>path==='admin/target.save'?new Promise(r=>{resolve=r;}):undefined;
 const submission=submit();await flush();await $('logout').click();
 resolve({revision:'rev-1',config:{protocol:'ssh',host:'127.0.0.1'},diff:[]});await submission;
 assert.notEqual($('change-dialog').open,true);assert.equal($('device-password').value,'');
 await $('change-apply').click();assert.equal(calls.filter(c=>c.path==='admin/target.save'&&!c.params.dry_run).length,0);
})().catch(e=>{console.error(e);process.exitCode=1});
""")
