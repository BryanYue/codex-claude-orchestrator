"""Execute the actual UI functions against synthetic snapshots and delayed reads."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProductViewerTests(unittest.TestCase):
    def test_states_stale_reads_event_backlog_and_polling(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for the executable UI regression')
        script = re.search(r'<script>(.*?)</script>', (ROOT/'assets/dashboard.html').read_text(), re.S).group(1)
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
class Node {
  constructor(){this.children=[];this.dataset={};this.classList={toggle(){},add(){},remove(){}};this.value='';this.textContent='';}
  append(...x){this.children.push(...x)}
  replaceChildren(...x){this.children=x}
  setAttribute(){}
  addEventListener(){}
  querySelector(){return new Node()}
}
const nodes=new Map();const byId=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id)};
let scheduled=[];
const context=vm.createContext({URL,URLSearchParams,AbortSignal,AbortController,console,setTimeout(fn,ms){scheduled.push(ms)},clearTimeout(){},
  location:{hash:'#token=fixture&run=run-first',origin:'http://127.0.0.1'},history:{replaceState(){}},
  navigator:{},window:{addEventListener(){}},document:{body:new Node(),hidden:false,
    getElementById:byId,createElement:()=>new Node(),querySelectorAll:()=>[],addEventListener(){}}});
vm.runInContext(SOURCE,context);const run=s=>vm.runInContext(s,context);const realApi=run('api');
(async()=>{
  assert.equal(run("displayState({status:'reported',decision:{decision:'accepted'}})"),'accepted');
  assert.equal(run("displayState({status:'reported',decision:{decision:'accepted'},superseded_by:'run-new'})"),'superseded');
  assert.equal(run("label({status:'reported',decision:{decision:'accepted'},superseded_by:'run-new'})"),'已验收 · 已有后续轮');
  assert.equal(run("report({status:'reported',decision:{decision:'accepted'},superseded_by:'run-new'})[0]"),'已验收 · 已有后续轮');
  assert.equal(run("displayState({status:'unknown',reconciliation:{outcome:'confirmed_stopped'}})"),'unknown');
  assert.deepEqual(JSON.parse(JSON.stringify(run("runCliEvidence({cli_version:'2.1.278',cli_identity:'native-id',environment:{status:'local_checks_passed'}})"))),{version:'2.1.278',identity:'native-id',environmentStatus:'local_checks_passed'});
  assert.equal(run("runCliEvidence({environment:{cli_version:'2.1.277',cli_identity:{id:'stored-id'}}}).version"),'2.1.277');
  assert.equal(run("runCliEvidence({environment:{cli_version:'2.1.277',cli_identity:{id:'stored-id'}}}).identity"),'stored-id');
  assert.equal(run("runCliEvidence({cli_identity_id:'fixed-dispatch-id'}).identity"),'fixed-dispatch-id');
  run("st.events=[{seq:1,received_at:'2026-09-22T00:00:01Z',summary:'旧分页活动'}];st.lastSync=1000;render({run_id:'run-visibility',task_id:'visibility',cwd:'/fixture',revision:1,status:'running',elapsed_seconds:12,last_activity_at:'2026-09-22T00:02:03Z',last_event:{seq:9,received_at:'2026-09-22T00:02:03Z',summary:'读取当前文件',tool:'Read'},requested_model:'requested-sonnet',actual_model:'actual-opus',actual_model_source:'assistant_message',requested_effort:'high',events_count:9})");
  assert.equal(byId('simpleActivity').textContent,'读取当前文件');
  assert.match(byId('simpleActivityAt').textContent,/2026/);
  assert.equal(byId('requestedModel').textContent,'requested-sonnet');
  assert.equal(byId('actualModel').textContent,'actual-opus');
  assert.equal(byId('requestedEffort').textContent,'high');
  assert.equal(run("actualModelText({actual_model:'init-only'})"),'待 provider 回报');
  assert.equal(run("actualModelText({actual_models:['model-a','model-b','model-a']})"),'model-a、model-b');
  const activityBeforeRefresh=byId('simpleActivityAt').textContent;
  run("st.lastSync=2000;render({run_id:'run-visibility',task_id:'visibility',cwd:'/fixture',revision:1,status:'running',elapsed_seconds:12,last_activity_at:'2026-09-22T00:02:03Z',last_event:{seq:9,received_at:'2026-09-22T00:02:03Z',summary:'读取当前文件'},requested_model:'requested-sonnet',events_count:9})");
  assert.equal(byId('simpleActivityAt').textContent,activityBeforeRefresh);
  assert.match(byId('lastSync').textContent,/页面最后同步/);
  assert.equal(byId('actualModel').textContent,'待 provider 回报');
  assert.equal(byId('requestedEffort').textContent,'本轮记录未提供');
  run("connectionError(new Error('offline'))");
  assert.equal(byId('simpleStatus').textContent,'执行中');
  assert.match(byId('connection').textContent,/连接中断/);
  run("st.events=[];render({run_id:'run-empty',task_id:'empty',cwd:'/fixture',revision:1,status:'running',elapsed_seconds:1,requested_model:'sonnet',model:'misleading-fallback',events_count:0})");
  assert.equal(byId('simpleActivity').textContent,'暂无新的公开活动');
  assert.match(byId('simpleActivityAt').textContent,/不等于执行停止/);
  assert.equal(byId('requestedModel').textContent,'sonnet');
  assert.equal(byId('actualModel').textContent,'待 provider 回报');
  assert.match(run("maintenanceNotice({notice_pending:true,current_version:'2.1.278',target_version:'2.1.279',message:'保留当前已验证版本'})"),/当前 2\.1\.278；目标 2\.1\.279/);
  assert.match(run("maintenanceNotice({notice_pending:true,current_version:'2.1.278',target_version:'2.1.279',message:'保留当前已验证版本'})"),/保留当前已验证版本/);
  assert.equal(run("maintenanceNotice({state:'up_to_date',notice_pending:false,current_version:'2.1.279'})"),'');
  assert.match(run("maintenanceNotice({state:'available',notice_pending:false,current_version:'2.1.280',active_unqualified_for_contract:true,effective_dispatch_version:'2.1.278'})"),/基础只读任务预计使用 2\.1\.278/);
  const activeNotice=run("maintenanceNotice({state:'acquiring',notice_pending:false,current_version:'2.1.278',target_version:'2.1.279',progress:{phase:'downloading',percent:42},reason:'machine_download_failure',next_action:'machine_retry'})");
  assert.match(activeNotice,/正在下载支持版本（42%）/);
  assert.match(activeNotice,/当前 2\.1\.278；目标 2\.1\.279/);
  assert.ok(!activeNotice.includes('machine_download_failure')&&!activeNotice.includes('machine_retry'));
  run("renderMaintenance({state:'acquiring',notice_pending:false,current_version:'2.1.278',target_version:'2.1.279',progress:{phase:'downloading',percent:42}})");
  assert.match(byId('maintenanceNotice').textContent,/正在下载支持版本（42%）/);
  run("renderMaintenance({state:'available',notice_pending:false,current_version:'2.1.278',target_version:'2.1.280',progress:{phase:'qualification'},channel:{source:'official',scope:'official_latest'},qualification:{state:'pending',missing_groups:['workflow']}})");
  assert.match(byId('maintenanceNotice').textContent,/验证候选兼容能力/);
  assert.match(byId('maintenanceNotice').textContent,/使用 Claude 额度/);
  assert.match(byId('maintenanceDetail').textContent,/尚未验证通过 workflow/);
  run("renderMaintenance({state:'switched',notice_pending:true,message:'新版已切换',qualification:{missing_groups:['workflow'],fallback_evidence:[{group:'workflow',version:'2.1.278'}]}})");
  assert.match(byId('maintenanceDetail').textContent,/workflow → CLI 2.1.278/);
  assert.ok(!byId('maintenanceNotice').textContent.includes('正在验证'));
  assert.equal(run("maintenanceNotice({state:'failed',notice_pending:false,message:'已确认失败'})"),'');
  run("renderMaintenance({notice_pending:true,current_version:'2.1.278',target_version:'2.1.279',message:'下载失败，已保留当前版本',reason:'machine_download_failure',next_action:'machine_retry',source:'bundled',scope:'installed_plugin_release',supported_target:'2.1.279'})");
  assert.match(byId('maintenanceNotice').textContent,/下载失败，已保留当前版本/);
  assert.ok(!byId('maintenanceNotice').textContent.includes('machine_download_failure')&&!byId('maintenanceNotice').textContent.includes('machine_retry'));
  assert.match(byId('maintenanceDetail').textContent,/来源 bundled/);
  assert.match(byId('maintenanceDetail').textContent,/支持目标 2\.1\.279/);
  assert.equal(run("runCliEvidence({cli_version:'2.1.278',cli_identity:'run-fixed-id'}).version"),'2.1.278');
  assert.equal(run("action({status:'reported'})[0]"),'');
  run("st.snapshot={status:'reported',decision:{decision:'returned',resolution:'completed_by_codex',completion_summary:'已逐项补齐审查',reason:'原报告事实错误',evidence:['file:1']},workspace_changes:{status:'observed',changed_files:[],preexisting_dirty_files:['old.txt'],worktree_dirty_files:['old.txt']}};st.decision=st.snapshot.decision;st.result={structured:{summary:'旧的错误结论'}};render(st.snapshot)");
  assert.match(byId('simpleStatus').textContent,/Codex 已完成/);
  assert.equal(run('action(st.snapshot)[1]'),'');
  assert.match(byId('readableSummary').textContent,/已逐项补齐审查/);
  assert.ok(!byId('readableSummary').textContent.includes('旧的错误结论'));
  assert.match(byId('workspaceChangeSummary').textContent,/0 个文件变化；开始前已有 1/);
  assert.match(run("workspaceChangeText({workspace_changes:{status:'unknown'}})"),/尚不能确认/);
  assert.match(run("action({status:'reported',decision:{decision:'returned'}})[1]"),/修正/);
  assert.equal(run("completedByCodex({...st.snapshot,superseded_by:'next'})"),false);
  run("st.events=[];st.snapshot={status:'running',last_activity_at:300,last_event:{seq:99,kind:'provider',received_at:300,summary:'thinking_tokens'},last_meaningful_event:{seq:2,kind:'tool',tool:'Read',summary:'tool path permitted',received_at:100}};render(st.snapshot)");
  assert.equal(byId('simpleActivity').textContent,'已允许读取');
  assert.equal(run('activityAt(st.snapshot,latestPublicEvent(st.snapshot))'),100000);
  run('st.snapshot=null;st.decision=null;st.result=null');
  assert.equal(run("statusSummary({status:'reported',summary:'reported is not accepted',decision:{decision:'accepted',reason:'已对照源文件完成核验'}})"),'已对照源文件完成核验');
  assert.match(run("action({status:'blocked'})[0]"),/预检/);
  assert.match(run("action({status:'blocked',blocked_by:'preflight'})[0]"),/预检/);
  run("st.result={structured:{summary:'缺少验收准则',unresolved:['请提供范围说明']}};");
  assert.match(run("action({status:'blocked',blocked_by:'executor'})[0]"),/缺少验收准则/);
  assert.match(run("action({status:'blocked',blocked_by:'executor'})[0]"),/请提供范围说明/);
  assert.match(run("action({status:'unknown'})[0]"),/暂不重派/);
  assert.match(run("action({status:'cancelling'})[0]"),/等待确认/);
  assert.ok(run('pollingDelay(true,false,false)')>run('pollingDelay(false,false,false)'));
  assert.ok(run('pollingDelay(false,true,false)')>run('pollingDelay(false,false,false)'));
  assert.ok(run('pollingDelay(true,false,true)')<1000);
  run("st.run='run-first';st.epoch=1;api=()=>new Promise(resolve=>{globalThis.resolveRead=resolve})");
  const pending=run("artifact('result')");run("st.run='run-second';st.epoch=2;resolveRead({available:true,content:'OLD RESULT'})");
  await pending;assert.notEqual(byId('artifact').textContent,'OLD RESULT');
  run("st.cursor=0;st.events=[];api=async(path,p)=>({events:Array.from({length:Math.min(200,550-p.after)},(_,i)=>({seq:p.after+i+1})),next_cursor:Math.min(550,p.after+200),has_more:p.after+200<550})");
  await run("fetchEvents('run-second',2)");
  assert.equal(run('st.cursor'),550);assert.equal(run('st.events.length'),300);assert.equal(run('st.events.at(-1).seq'),550);
  run("st.snapshot={status:'reported'};st.result={structured:{summary:'OLD'}};st.loadedKey='old';selectRun('run-third')");
  assert.equal(run('st.snapshot'),null);assert.equal(run('st.result'),null);assert.equal(run('st.cursor'),0);
  // Exercise the actual fetch/AbortSignal path: switching records interrupts a
  // slow terminal poll and queues an immediate fetch for the new record.
  context.api=realApi;
  const requests=[];
  context.fetch=(url,{signal})=>new Promise((resolve,reject)=>{
    requests.push(url.pathname);
    signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});
  });
  run("st.listAt=Date.now();st.maintenanceAt=Date.now();st.run='slow-terminal';st.snapshot={status:'reported'}");
  const slow=run('tick()');await Promise.resolve();
  assert.deepEqual(requests,['/api/snapshot']);
  run("selectRun('new-record')");await slow;
  assert.equal(scheduled.at(-1),0);assert.equal(run('st.busy'),false);
  // A large old log starts at its latest window; snapshot paints before slow
  // auxiliary maintenance, and an unavailable maintenance endpoint is separate.
  const eventOffsets=[];
  context.fetch=async(url)=>{
    const route=url.pathname;let data;
    if(route==='/api/snapshot')data={run_id:'new-record',status:'running',events_count:100000,last_event:{seq:100000,kind:'tool',tool:'Read',summary:'LATEST'}};
    else if(route==='/api/events'){eventOffsets.push(Number(url.searchParams.get('after')));data={events:[{seq:100000,kind:'tool',summary:'LATEST'}],next_cursor:100000,has_more:false};}
    else if(route==='/api/artifact')data={available:false};
    else if(route==='/api/cli-maintenance'){assert.equal(byId('simpleStatus').textContent,'执行中');throw new Error('slow maintenance unavailable');}
    else data={runs:[],has_more:false,next_offset:0};
    return {ok:true,json:async()=>data};
  };
  run('st.maintenanceAt=0');await run('tick()');
  assert.deepEqual(eventOffsets,[99700]);assert.equal(run('st.connected'),true);
  assert.match(byId('maintenanceNotice').textContent,/维护状态暂不可用/);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        source = 'const SOURCE=' + __import__('json').dumps(script) + ';\n' + harness
        result = subprocess.run([node,'-'],input=source,text=True,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
