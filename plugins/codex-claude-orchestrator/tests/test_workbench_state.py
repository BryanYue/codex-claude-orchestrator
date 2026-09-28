"""Execute the actual workbench state/derivation functions from dashboard.html in Node.

This complements test_product_viewer.py: that file drives DOM rendering and network
timing behavior, this file exercises the pure cache/current-round/fact-derivation/
scheduling functions in isolation against the v2 UI spec's decision tables.
"""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS_PRELUDE = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
function makeClassList(){
  const set=new Set();
  return {add(...n){for(const x of n)set.add(x);},remove(...n){for(const x of n)set.delete(x);},
    toggle(name,force){if(force===undefined){if(set.has(name)){set.delete(name);return false;}set.add(name);return true;}
      if(force){set.add(name);return true;}set.delete(name);return false;},
    contains(name){return set.has(name);}};
}
class Node {
  constructor(){this.children=[];this.dataset={};this.attrs={};this.style={};this._value='';this.textContent='';
    this.disabled=false;this.classList=makeClassList();}
  get value(){return this._value;} set value(v){this._value=v;}
  get options(){return this.children;}
  append(...x){this.children.push(...x);}
  replaceChildren(...x){this.children=x;}
  setAttribute(name,v){this.attrs[name]=String(v);}
  getAttribute(name){return Object.hasOwn(this.attrs,name)?this.attrs[name]:null;}
  addEventListener(){}
  querySelector(){return new Node();}
  querySelectorAll(){return [];}
  focus(){}
}
const nodes=new Map();const byId=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
let scheduled=[];
const context=vm.createContext({URL,URLSearchParams,AbortSignal,AbortController,console,
  setTimeout(fn,ms){scheduled.push(ms);},clearTimeout(){},
  getComputedStyle:()=>({display:'none'}),
  location:{hash:'#token=fixture&run=',origin:'http://127.0.0.1'},history:{replaceState(){}},
  navigator:{},window:{addEventListener(){}},
  document:{documentElement:new Node(),activeElement:new Node(),body:new Node(),hidden:false,
    getElementById:byId,createElement:()=>new Node(),createTextNode:text=>({textContent:String(text)}),querySelectorAll:()=>[],addEventListener(){}}});
vm.runInContext(SOURCE,context);
const run=s=>vm.runInContext(s,context);
'''


class WorkbenchStateTests(unittest.TestCase):
    def test_maintenance_drawer_keeps_keyboard_focus(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for the executable UI regression')
        script = re.search(r'<script>(.*?)</script>', (ROOT / 'assets/dashboard.html').read_text(), re.S).group(1)
        harness = HARNESS_PRELUDE.replace('let scheduled=[];', 'let scheduled=[];const listeners={};')
        harness = harness.replace('querySelectorAll:()=>[],addEventListener(){}', 'querySelectorAll:()=>[],addEventListener(name,fn){listeners[name]=fn;}')
        harness += r'''
const close=byId('drawerClose'),launcher=byId('maintenanceBtn');
close.focus=()=>{context.document.activeElement=close;};
launcher.focus=()=>{context.document.activeElement=launcher;};
byId('drawer').querySelectorAll=()=>[close];
context.document.activeElement=launcher;
byId('maintenanceBtn').onclick();
assert.equal(context.document.activeElement,close);
for(const shiftKey of [false,true]){
  let prevented=false;
  listeners.keydown({key:'Tab',shiftKey,preventDefault(){prevented=true;}});
  assert.equal(prevented,true,'Tab must wrap inside the modal, including a single focus target');
  assert.equal(context.document.activeElement,close);
}
let escaped=false;
listeners.keydown({key:'/',preventDefault(){escaped=true;}});
assert.equal(escaped,false,'global search shortcut must not steal modal focus');
assert.equal(context.document.activeElement,close);
listeners.keydown({key:'Escape',preventDefault(){}});
assert.equal(byId('drawerOverlay').classList.contains('hidden'),true);
assert.equal(context.document.activeElement,launcher);
'''
        source = 'const SOURCE=' + __import__('json').dumps(script) + ';\n' + harness
        result = subprocess.run([node, '-'], input=source, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_states_cache_current_round_facts_and_scheduling(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for the executable UI regression')
        script = re.search(r'<script>(.*?)</script>', (ROOT / 'assets/dashboard.html').read_text(), re.S).group(1)
        harness = HARNESS_PRELUDE + r'''
(async()=>{
  assert.equal(run("taskKey({task_id:'t1',cwd:'/a'})"),'t1\u0000/a');
  assert.equal(run("taskKey({run_id:'run-x',cwd:'/a'})"),'run:run-x');

  run("let e=mergeSummary(undefined,{run_id:'r1',status:'running',decision:null,superseded_by:null},1000);globalThis.e=e;");
  assert.equal(run('e.summaryAt'),1000);
  assert.equal(run('e.detailStale'),false);
  run("e=mergeDetail(e,{run_id:'r1',status:'running',decision:null,claude_started:true},1100);globalThis.e=e;");
  assert.equal(run('e.detail.claude_started'),true);
  run("e=mergeSummary(e,{run_id:'r1',status:'running',decision:null,superseded_by:null},1200);globalThis.e=e;");
  assert.equal(run('e.detailStale'),false,'unchanged status/decision/superseded_by must not invalidate detail');
  assert.equal(run('Boolean(e.detail)'),true,'summary refresh must never erase detail (R1)');
  run("e=mergeSummary(e,{run_id:'r1',status:'reported',decision:null,superseded_by:null},1300);globalThis.e=e;");
  assert.equal(run('e.detailStale'),true,'status change must invalidate stale detail');
  run("const view=viewOf(e);globalThis.view=view;");
  assert.equal(run('view.hasDetail'),false);
  assert.equal(run('view.view.status'),'reported','stale view must reflect the fresher summary status');
  assert.equal(run('view.view.claude_started'),true,'stale view still carries fields the summary does not have (R1)');

  assert.deepEqual(JSON.parse(JSON.stringify(run('currentRoundOf([])'))),{state:'not_loaded',pointer:null});
  assert.equal(run("currentRoundOf([{run_id:'r1',revision:1,superseded_by:null}]).state"),'known');
  const notLoaded=run("currentRoundOf([{run_id:'r1',revision:1,superseded_by:'r2'}])");
  assert.equal(notLoaded.state,'not_loaded');assert.equal(notLoaded.pointer,'r2');
  const inconsistent=run("currentRoundOf([{run_id:'r1',revision:1,superseded_by:null},{run_id:'r2',revision:2,superseded_by:null}])");
  assert.equal(inconsistent.state,'pending','conflicting tips cannot authorize a current-round action');
  const higherConflict=run("currentRoundOf([{run_id:'r1',revision:1,superseded_by:null},{run_id:'r2',revision:2,superseded_by:'r1'}])");
  assert.equal(higherConflict.state,'pending','a lower revision tip cannot outrank contradictory higher evidence');
  run("st.cache.set('current',mergeDetail(undefined,{run_id:'current',task_id:'task',cwd:'/a',revision:2,status:'reported',decision:null,superseded_by:null},Date.now()-70000,10));");
  assert.equal(run("currentRoundOf([viewOf(st.cache.get('current')).view],st.cache).state"),'pending','a stale candidate cannot be called the current round');
  run("st.cache.set('current',mergeDetail(st.cache.get('current'),{run_id:'current',task_id:'task',cwd:'/a',revision:2,status:'reported',decision:null,superseded_by:null},Date.now(),11));");
  assert.equal(run("currentRoundOf([viewOf(st.cache.get('current')).view],st.cache).state"),'known');
  assert.equal(run("st.cache.get('current').summaryAt===st.cache.get('current').detailAt"),true,'snapshot success refreshes card freshness');
  run("st.cache.set('current',mergeSummary(st.cache.get('current'),{run_id:'current',task_id:'task',cwd:'/a',revision:2,status:'failed',decision:null,superseded_by:null},Date.now(),12));");
  assert.equal(run("currentRoundOf([viewOf(st.cache.get('current')).view],st.cache).state"),'pending','a changed summary must invalidate old detail');
  run("st.cache.set('current',mergeDetail(st.cache.get('current'),{run_id:'current',task_id:'task',cwd:'/a',revision:2,status:'reported',decision:null,superseded_by:null},Date.now(),11));");
  assert.equal(run("st.cache.get('current').detailStale"),true,'an older detail response cannot override the newer summary');

  run("globalThis.visited=new Set();");
  assert.equal(run("nextSupersedeHop('r2',visited)"),'r2');
  run("visited.add('r2')");
  assert.equal(run("nextSupersedeHop('r2',visited)"),null,'already-visited hop must not repeat (loop guard)');
  for (let i=0;i<5;i++) run(`visited.add('x${i}')`);
  assert.equal(run("nextSupersedeHop('r99',visited)"),null,'must stop after 5 hops');

  assert.deepEqual(JSON.parse(JSON.stringify(run("execFact({status:'preflight'},false)"))),{tone:'none',label:'已创建 · 预检中',detail:''});
  assert.equal(run("execFact({status:'starting',claude_started:null},true).label"),'正在启动');
  assert.equal(run("execFact({status:'starting',claude_started:true},true).label"),'执行中');
  assert.equal(run("execFact({status:'blocked'},false).detail"),'原因详情未载入','undetailed blocked must not guess a reason');
  assert.equal(run("execFact({status:'blocked',receipt:{blocked_by:'preflight'},claude_started:false},true).label"),'预检阻止 · Claude 未启动');
  assert.equal(run("execFact({status:'blocked',receipt:{blocked_by:'preflight'},claude_started:null},true).label"),'预检阻止 · 启动状态未确认');
  assert.equal(run("execFact({status:'blocked',result:{blocked_by:'executor'}},true).label"),'执行者无法继续');
  assert.equal(run("execFact({status:'unknown',reconciliation:{outcome:'confirmed_stopped'}},true).label"),'已核对停止');
  assert.equal(run("execFact({status:'unknown'},true).tone"),'unknown');
  assert.equal(run("execFact({status:'cancelled',claude_started:false},true).label"),'启动前已取消');
  assert.equal(run("execFact({status:'cancelled',claude_started:true},true).label"),'已停止');
  assert.equal(run("execFact({status:'failed',claude_started:false},true).label"),'启动失败 · Claude 未启动');
  assert.equal(run("execFact({status:'failed',claude_started:null},true).label"),'执行失败','null claude_started must not be read as false (spec 5.1)');
  assert.equal(run("execFact({status:'reported'},true).tone"),'settled');

  assert.equal(run("reportFact({status:'failed'},false).kind"),'not_loaded');
  assert.equal(run("reportFact({status:'failed',result:{structured:{summary:'x'}}},true).kind"),'failed_with_content');
  assert.equal(run("reportFact({status:'failed',result:{}},true).kind"),'failed_no_structured');
  assert.equal(run("reportFact({status:'failed',result:null},true).kind"),'failed_no_structured');
  assert.equal(run("reportFact({status:'reported',decision:null},false).kind"),'not_loaded','R1: reported row without loaded detail must not claim a report body');
  assert.equal(run("reportFact({status:'reported',decision:null},false).label"),'已回报 · 待核验');
  assert.equal(run("reportFact({status:'reported',decision:null},true).kind"),'formal');
  assert.equal(run("reportFact({status:'reported',decision:{decision:'accepted'}},true).kind"),'accepted');
  assert.equal(run("reportFact({status:'reported',decision:{decision:'accepted'}},true).label"),'已回报 · 已验收');
  assert.equal(run("reportFact({status:'reported',decision:{decision:'returned'}},false).label"),'已回报 · 已退回');
  assert.equal(run("reportFact({status:'reported',decision:{decision:'returned',resolution:'completed_by_codex'}},true).kind"),'codex');
  const historyReport=run("reportFact({status:'reported',decision:{decision:'returned'},superseded_by:'r2'},true)");
  assert.equal(historyReport.tone,'history');assert.match(historyReport.label,/· 历史$/);

  assert.equal(run('gate.B1'),false);assert.equal(run('gate.B2'),false);assert.equal(run('gate.B3'),false);
  run('gate.B1=true;');
  assert.equal(run("reportFact({status:'reported',report:{integrity:'truncated'}},true).kind"),'truncated');
  run('gate.B1=false;');
  assert.notEqual(run("reportFact({status:'reported',report:{integrity:'truncated'}},true).kind"),'truncated','closed gate must fall back to the plain status, scenario 3');
  run('gate.B2=true;');
  assert.equal(run("reportFact({status:'reported',report:{collection_state:'pending'}},true).kind"),'pending');
  run('gate.B2=false;');
  run('gate.B3=true;');
  assert.match(run("reportFact({status:'reported',report:{contract_check:{passed:false,failures:[{kind:'placeholder'}]}}},true).label"),/占位值/);
  assert.equal(run("reportFact({status:'reported',report:{contract_check:{passed:false,failures:[{kind:'identity_mismatch'}]}}},true).label"),'未通过报告契约');
  run('gate.B3=false;');

  const okVerify=run("verifyFact({status:'reported',decision:{decision:'accepted'}},true)");
  assert.equal(okVerify.tone,'ok');assert.equal(okVerify.label,'通过');
  const historyVerify=run("verifyFact({status:'reported',decision:{decision:'accepted'},superseded_by:'r2'},true)");
  assert.equal(historyVerify.tone,'history');assert.equal(historyVerify.symbol,'✓');
  const codexVerify=run("verifyFact({status:'reported',decision:{decision:'returned',resolution:'completed_by_codex'}},true)");
  assert.equal(codexVerify.tone,'ok');assert.equal(codexVerify.label,'Codex 已补齐完成');
  assert.equal(run("verifyFact({status:'failed'},true).label"),'不适用');
  const noneVerify=run("verifyFact({status:'reported',decision:null},true)");
  assert.equal(noneVerify.label,'未核验');assert.match(noneVerify.detail,/等待你在 Codex 核验/);

  assert.equal(run("groupFor({state:'known',run:{status:'reported',decision:{decision:'accepted'}}},true)"),'closed');
  assert.equal(run("groupFor({state:'known',run:{status:'running'}},true)"),'active');
  assert.equal(run("groupFor({state:'known',run:{status:'reported',decision:null}},true)"),'attention');
  assert.equal(run("groupFor({state:'not_loaded',pointer:'r2'},false)"),'attention');
  const notLoadedReason=run("reasonLine({state:'not_loaded',pointer:'r2'},false)");
  assert.equal(notLoadedReason.text,'当前轮待加载');assert.equal(notLoadedReason.tone,'unknown');
  assert.equal(run("reasonLine({state:'known',run:{status:'unknown'}},true).text"),'状态未知 · 暂不要重派');

  assert.match(run("nextStepFor({state:'known',run:{run_id:'run-x',status:'reported',decision:null}},true).prompt"),/核验执行 run-x 的报告/);
  assert.equal(run("nextStepFor({state:'known',run:{run_id:'run-x',status:'reported',decision:{decision:'returned'}}},true).prompt"),'按退回原因修正这项任务。');
  assert.match(run("nextStepFor({state:'known',run:{run_id:'run-x',status:'blocked',receipt:{blocked_by:'preflight'},claude_started:false}},true).prompt"),/预检阻止原因/);
  const executorStep=run("nextStepFor({state:'known',run:{run_id:'run-x',status:'blocked',result:{blocked_by:'executor',structured:{summary:'缺材料',unresolved:['范围']}}}},true)");
  assert.match(executorStep.body,/缺材料/);assert.match(executorStep.body,/范围/);
  assert.match(run("nextStepFor({state:'known',run:{run_id:'run-x',status:'unknown',reconciliation:{outcome:'confirmed_stopped'}}},true).body"),/历史结果仍未验收/);
  assert.match(run("nextStepFor({state:'known',run:{run_id:'run-x',status:'unknown'}},true).prompt"),/暂不重派/);
  assert.equal(run("nextStepFor({state:'known',run:{run_id:'run-x',status:'cancelling'}},true).prompt"),null);
  assert.equal(run("nextStepFor({state:'known',run:{run_id:'run-x',status:'reported',superseded_by:'r2'}},true)"),null,'a historical run must never carry a next step');
  assert.equal(run("nextStepFor({state:'not_loaded',pointer:'r2'},false)"),null);

  assert.deepEqual(Array.from(run("scheduleBackground([],['obs1','obs2'],['pre1','pre2','pre3'],2)")),['pre1','obs1']);
  assert.deepEqual(Array.from(run("scheduleBackground([],[],['pre1','pre2','pre3'],2)")),['pre1','pre2']);
  assert.deepEqual(Array.from(run("scheduleBackground([],['obs1'],[],2)")),['obs1']);
  assert.deepEqual(Array.from(run("scheduleBackground(['bf1'],['obs1','obs2'],['pre1'],2)")),['bf1','obs1'],'backfill must take priority over prefetch within the shared slot');

  assert.deepEqual(Array.from(run("reconcileLoadedOrder(['r5','r4','r3'],['r6','r5'])")),['r6','r5','r4','r3']);

  run("globalThis.baseline=new Set(['r1','r2']);globalThis.seen=new Set(['r1','r2','r3']);");
  assert.equal(run("isNewCandidate({run_id:'r1'},baseline,seen)"),false);
  assert.equal(run("isNewCandidate({run_id:'r3'},baseline,seen)"),false,'a run already seen through pagination/backfill is never new');
  assert.equal(run("isNewCandidate({run_id:'r4'},baseline,seen)"),true);
  run("globalThis.existingKeys=new Set(['t1\\u0000/a']);");
  assert.equal(run("isNewTaskCandidate({task_id:'t1',cwd:'/a'},existingKeys)"),false);
  assert.equal(run("isNewTaskCandidate({task_id:'t2',cwd:'/a'},existingKeys)"),true);

  assert.equal(run('recoveryReady({homeOk:true,snapshotOk:false,eventsOk:false},false)'),true);
  assert.equal(run('recoveryReady({homeOk:true,snapshotOk:false,eventsOk:false},true)'),false);
  assert.equal(run('recoveryReady({homeOk:true,snapshotOk:true,eventsOk:true},true)'),true);
  run("conn.status='recovered';renderConnection();");
  assert.match(byId('connection').textContent,/已连接/,'recovered data must not retain a connecting header');
  assert.equal(run('nextBackoff(0)'),15000);
  assert.equal(run('nextBackoff(15000)'),30000);
  assert.equal(run('nextBackoff(30000)'),60000);
  assert.equal(run('nextBackoff(60000)'),60000);

  assert.equal(run('homePollingDelay(true,false)'),5000);
  assert.equal(run('homePollingDelay(false,false)'),15000);
  assert.equal(run('homePollingDelay(true,true)'),60000);
  assert.ok(run('pollingDelay(true,false,false)')>run('pollingDelay(false,false,false)'));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        source = 'const SOURCE=' + __import__('json').dumps(script) + ';\n' + harness
        result = subprocess.run([node, '-'], input=source, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
