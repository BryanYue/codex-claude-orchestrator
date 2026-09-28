"""Execute the actual UI functions and DOM/network timing behavior of dashboard.html.

Migrated from the 0.4.7 single-page viewer: this keeps the behavioral coverage that
mattered (stale-response/abort discarding on switch, event backlog catch-up, isolated
maintenance failures, honest sync-vs-activity time) and adds coverage for the v2
workbench's connection state machine (offline/token_invalid/recovered) and the R1/R6
merge/error-boundary rules.
"""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProductViewerTests(unittest.TestCase):
    def test_states_stale_reads_event_backlog_isolation_and_connection_recovery(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for the executable UI regression')
        script = re.search(r'<script>(.*?)</script>', (ROOT / 'assets/dashboard.html').read_text(), re.S).group(1)
        harness = r'''
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
function findByClass(node,cls){
  if(!node)return null;
  if(node.className===cls)return node;
  for(const c of (node.children||[])){const found=findByClass(c,cls);if(found)return found;}
  return null;
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
(async()=>{
  // --- migrated: maintenanceNotice/maintenanceProgress pure text formatting ---
  assert.match(run("maintenanceNotice({notice_pending:true,current_version:'2.1.278',target_version:'2.1.279',message:'保留当前已验证版本'})"),/当前 2\.1\.278；目标 2\.1\.279/);
  assert.equal(run("maintenanceNotice({state:'up_to_date',notice_pending:false,current_version:'2.1.279'})"),'');
  const activeNotice=run("maintenanceNotice({state:'acquiring',notice_pending:false,current_version:'2.1.278',target_version:'2.1.279',progress:{phase:'downloading',percent:42},reason:'machine_download_failure',next_action:'machine_retry'})");
  assert.match(activeNotice,/正在下载支持版本（42%）/);
  assert.ok(!activeNotice.includes('machine_download_failure')&&!activeNotice.includes('machine_retry'));
  run("renderMaintenance({state:'acquiring',notice_pending:false,current_version:'2.1.278',target_version:'2.1.279',progress:{phase:'downloading',percent:42}})");
  assert.match(run("$('drawerMaintenance').textContent"),/正在下载支持版本（42%）/);

  // --- migrated: event backlog catch-up starts a large old log at its latest tail window ---
  run("st.run='run-events';st.epoch=0;conn.connGen=0;st.cache.set('run-events',emptyEntry());");
  context.fetch=async(url)=>{
    const route=url.pathname;
    if(route==='/api/events'){
      const after=Number(url.searchParams.get('after'));
      const data={events:Array.from({length:Math.min(200,550-after)},(_,i)=>({seq:after+i+1})),next_cursor:Math.min(550,after+200),has_more:after+200<550};
      return {ok:true,json:async()=>data};
    }
    return {ok:true,json:async()=>({})};
  };
  await run("fetchEventsFor('run-events',st.epoch,conn.connGen)");
  assert.equal(run("st.cache.get('run-events').cursor"),550);
  assert.equal(run("st.cache.get('run-events').events.length"),300,'MAX_EVENTS must trim the in-memory tail');
  assert.equal(run("st.cache.get('run-events').events.at(-1).seq"),550);

  // --- large old log's snapshot tail-window priming + isolated maintenance failure ---
  const eventOffsets=[];
  run("st.run='run-large';st.epoch=1;conn.connGen=0;st.cache.set('run-large',emptyEntry());");
  context.fetch=async(url)=>{
    const route=url.pathname;
    if(route==='/api/snapshot')return {ok:true,json:async()=>({run_id:'run-large',status:'running',events_count:100000,last_event:{seq:100000,kind:'tool',tool:'Read',summary:'LATEST'}})};
    if(route==='/api/events'){eventOffsets.push(Number(url.searchParams.get('after')));return {ok:true,json:async()=>({events:[{seq:100000,kind:'tool',summary:'LATEST'}],next_cursor:100000,has_more:false})};}
    return {ok:true,json:async()=>({})};
  };
  await run('selTick()');
  assert.deepEqual(eventOffsets,[99700],'a 100000-event log must resume from its tail window, not from zero');
  assert.equal(run('conn.snapshotOk'),true);
  assert.equal(run('conn.eventsOk'),true);

  context.fetch=async(url)=>{ if(url.pathname==='/api/cli-maintenance') throw new Error('maintenance unavailable'); return {ok:true,json:async()=>({})}; };
  run("conn.status='connecting';");
  await run('maintenanceTick()');
  assert.match(run("$('drawerMaintenance').textContent"),/维护状态暂不可用/);
  assert.notEqual(run('conn.status'),'offline','a failed local maintenance read must never degrade connection state');

  // --- switching the selected run mid-flight discards the stale response (epoch/connGen guard) ---
  const requests=[];
  context.fetch=(url,{signal})=>new Promise((resolve,reject)=>{
    requests.push(url.pathname);
    signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});
  });
  run("st.cache.set('slow-run',mergeSummary(undefined,{run_id:'slow-run',task_id:'t-slow',cwd:'/fixture',revision:1,status:'running'},Date.now()));");
  run("st.run='slow-run';st.epoch=5;conn.connGen=0;st.selInFlight=false;");
  const pending=run('selTick()');
  await Promise.resolve();await Promise.resolve();
  run("st.cache.set('new-run',mergeSummary(undefined,{run_id:'new-run',task_id:'t-new',cwd:'/fixture',revision:1,status:'running'},Date.now()));");
  run("selectRun('new-run')");
  context.fetch=async(url)=>{
    requests.push(url.pathname);
    if(url.pathname==='/api/snapshot')return {ok:true,json:async()=>({run_id:'new-run',task_id:'t-new',cwd:'/fixture',revision:1,status:'running',last_event:{seq:0}})};
    return {ok:true,json:async()=>({events:[],next_cursor:0,has_more:false})};
  };
  await run('selTick()');
  await pending;
  assert.equal(run('st.selInFlight'),false);
  assert.deepEqual(requests,['/api/snapshot','/api/snapshot','/api/events'],'the old request must not block the newly selected run');
  assert.equal(run('st.run'),'new-run');

  // --- a 400 on the selected run clears the valid-selection flag without touching the list/connection ---
  run("conn.validSelection=true;conn.status='connecting';st.run='bad-run';st.epoch=9;conn.connGen=0;st.cache.set('bad-run',mergeDetail(undefined,{run_id:'bad-run',task_id:'bad',cwd:'/fixture',revision:1,status:'reported',decision:null},Date.now()));st.selInFlight=false;");
  context.fetch=async(url)=>{
    if(url.pathname==='/api/snapshot')return {ok:false,status:400,json:async()=>({error:'run_id was not found'})};
    return {ok:true,json:async()=>({})};
  };
  await run('selTick()');
  assert.equal(run('conn.validSelection'),false);
  assert.match(run("$('detailTitle').textContent"),/该执行记录无法读取/);
  assert.notEqual(run('conn.status'),'offline','an invalid selected run must not be reported as a connection failure');

  // --- connection recovery requires home+snapshot+events all fresh when there is a valid selection ---
  run("conn.status='offline';conn.validSelection=true;conn.homeOk=false;conn.snapshotOk=false;conn.eventsOk=false;conn.retryDelay=15000;");
  run("noteCriticalSuccess('runs')");
  assert.equal(run('conn.status'),'offline','home alone must not recover a valid selection');
  run("noteCriticalSuccess('snapshot-selected')");
  run("noteCriticalSuccess('events-selected')");
  assert.equal(run('conn.status'),'recovered');
  const bannerText=run("$('bannerArea').children.length?$('bannerArea').children[0].children.map(c=>c.textContent||'').join(' '):''");
  assert.match(bannerText,/已恢复读取/);
  assert.ok(!bannerText.includes('连续')&&!bannerText.includes('无遗漏'),'recovery text must not claim unbroken continuity');

  // --- offline banner text preserves the "not proof of stopping" and no-redispatch guidance ---
  run("conn.status='offline';conn.lastCriticalSyncAt=Date.now();renderBanner();");
  const offlineText=run("$('bannerArea').children[0].children[0].textContent");
  assert.match(offlineText,/不代表执行已停止/);
  assert.match(offlineText,/不要据此重派/);

  // --- sync time never masquerades as activity time once a card goes stale (60s) ---
  run("conn.status='connecting';st.cache.clear();");
  run("st.cache.set('run-stale',mergeSummary(undefined,{run_id:'run-stale',task_id:'stale-task',cwd:'/fixture',revision:1,status:'reported',decision:{decision:'accepted'},last_activity_at:'2026-09-22T00:02:03Z'},Date.now()-70000));");
  run("st.loadedOrder=['run-stale'];st.loadedSet=new Set(st.loadedOrder);");
  run("st.filter='all';st.search='';");
  run('renderList()');
  const meta=findByClass(findByClass(byId('taskGroups'),'task-card'),'card-meta');
  const metaText=meta?meta.textContent:null;
  assert.match(metaText,/同步于/,'a card not refreshed within 60s must show a sync stamp, not a fabricated activity time');

  run("conn.status='online';conn.lastCriticalSyncAt=Date.now()-5000;renderConnection();");
  assert.match(run("$('connection').textContent"),/已连接 · 同步于/);

  // --- live pagination keeps its cursor when a new first page arrives ---
  run("st.cache.clear();st.loadedOrder=[];st.loadedSet=new Set();st.pagesLoaded=false;st.baseline=null;st.seenRunIds=new Set();st.knownTaskKeys=new Set();st.run='';st.invalidRun=null;st.nextOffset=0;st.hasMore=false;");
  const row=id=>({run_id:id,task_id:id,cwd:'/fixture',revision:1,status:'reported',decision:null,superseded_by:null});
  run(`onHomeSuccess(${JSON.stringify({runs:[row('r2'),row('r1')],next_offset:2,has_more:true})},100)`);
  const offsets=[];
  context.fetch=async(url)=>{
    if(url.pathname==='/api/runs'){
      const offset=Number(url.searchParams.get('offset'));offsets.push(offset);
      return {ok:true,json:async()=>({runs:[row('r'+(offset+1))],next_offset:offset+2,has_more:true})};
    }
    return {ok:true,json:async()=>({})};
  };
  await run('loadMore()');
  run(`onHomeSuccess(${JSON.stringify({runs:[row('r0'),row('r2')],next_offset:2,has_more:true})},102)`);
  await run('loadMore()');
  assert.deepEqual(offsets,[2,4],'home polling must not reset the older-page cursor');
  run('st.loadedOrder=Array.from({length:500},(_,i)=>"cap"+i);st.loadedSet=new Set(st.loadedOrder);st.hasMore=true;renderList()');
  assert.equal(byId('loadMoreBtn').disabled,true);
  assert.equal(byId('loadMoreBtn').textContent,'已达到载入上限');

  // --- backfill and observer share the two-request cycle, and validate every hop ---
  run("st.cache.clear();st.loadedOrder=['old','obs'];st.loadedSet=new Set(st.loadedOrder);st.recentHomeIds=new Set();st.prefetchQueue=new Set();st.backfillQueue=new Set();st.backfillStopped=new Set();st.visitedByTask=new Map();st.backfillFailedAt=new Map();st.run='';conn.status='online';");
  run("st.cache.set('old',mergeSummary(undefined,{run_id:'old',task_id:'task',cwd:'/fixture',revision:1,status:'reported',superseded_by:'next'},Date.now()));st.cache.set('obs',mergeSummary(undefined,{run_id:'obs',task_id:'observer',cwd:'/fixture',revision:1,status:'running',superseded_by:null},Date.now()));");
  const bgRequests=[];
  context.fetch=async(url)=>{
    if(url.pathname==='/api/snapshot'){
      const id=url.searchParams.get('run_id');bgRequests.push(id);
      return {ok:true,json:async()=>id==='next'?{run_id:'next',task_id:'task',cwd:'/fixture',revision:2,status:'running',superseded_by:null}:{run_id:'obs',task_id:'observer',cwd:'/fixture',revision:1,status:'running',superseded_by:null}};
    }
    return {ok:true,json:async()=>({})};
  };
  await run('bgTick()');
  assert.deepEqual(bgRequests,['next','obs'],'backfill consumes one slot and observation keeps one slot');
  assert.equal(run("currentRoundOf(buildTasks().get(taskKey(viewOf(st.cache.get('old')).view)),st.cache).run.run_id"),'next');
  run("st.cache.clear();st.loadedOrder=['old'];st.loadedSet=new Set(st.loadedOrder);st.backfillQueue=new Set();st.backfillStopped=new Set();st.visitedByTask=new Map();st.backfillFailedAt=new Map();st.cache.set('old',mergeSummary(undefined,{run_id:'old',task_id:'task',cwd:'/fixture',revision:1,status:'reported',superseded_by:'wrong'},Date.now()));");
  context.fetch=async(url)=>({ok:true,json:async()=>({run_id:url.searchParams.get('run_id'),task_id:'alien',cwd:'/fixture',revision:2,status:'running',superseded_by:null})});
  await run('bgTick()');
  assert.equal(run("st.backfillStopped.has('task\\u0000/fixture')"),true,'a foreign task cannot complete the supersession chain');
  assert.equal(run("st.cache.has('wrong')"),false);

  // A failed observer still yields its place; the 41st candidate is eventually read.
  run("st.cache.clear();st.loadedOrder=Array.from({length:41},(_,i)=>'observe-'+i);st.loadedSet=new Set(st.loadedOrder);st.recentHomeIds=new Set();st.prefetchQueue=new Set();st.backfillQueue=new Set();st.backfillStopped=new Set();st.run='';for(const id of st.loadedOrder)st.cache.set(id,mergeSummary(undefined,{run_id:id,task_id:id,cwd:'/fixture',revision:1,status:'running',superseded_by:null},Date.now()));");
  const observed=[];
  context.fetch=async(url)=>{observed.push(url.searchParams.get('run_id'));throw new Error('local snapshot unavailable');};
  for(let i=0;i<21;i++)await run('bgTick()');
  assert.ok(observed.includes('observe-40'),'failed early candidates must not starve the 41st observer');
  assert.ok(run('st.observerOverflow')>=1,'the UI must disclose candidates outside the active 40-run window');
  run("document.hidden=true");
  const observedBeforeHidden=observed.length;
  await run('bgTick()');
  assert.equal(observed.length,observedBeforeHidden,'hidden workbench pauses background snapshots');
  run("document.hidden=false");

  // A five-hop chain stops without reading a sixth pointer.
  run("st.cache.clear();st.loadedOrder=['hop-0'];st.loadedSet=new Set(st.loadedOrder);st.recentHomeIds=new Set();st.prefetchQueue=new Set();st.backfillQueue=new Set();st.backfillStopped=new Set();st.visitedByTask=new Map();st.backfillFailedAt=new Map();st.run='';st.cache.set('hop-0',mergeSummary(undefined,{run_id:'hop-0',task_id:'hops',cwd:'/fixture',revision:1,status:'reported',superseded_by:'hop-1'},Date.now()));");
  const hops=[];
  context.fetch=async(url)=>{
    const id=url.searchParams.get('run_id');hops.push(id);const n=Number(id.split('-')[1]);
    return {ok:true,json:async()=>({run_id:id,task_id:'hops',cwd:'/fixture',revision:n+1,status:'reported',superseded_by:'hop-'+(n+1)})};
  };
  for(let i=0;i<7;i++)await run('bgTick()');
  assert.deepEqual(hops,['hop-1','hop-2','hop-3','hop-4','hop-5']);
  assert.equal(run("st.backfillStopped.has('hops\\u0000/fixture')"),true);

  // --- action copy is checked again at click time; 401 stops automatic retries ---
  let copies=0;context.navigator.clipboard={writeText:async()=>{copies++;}};
  run("st.cache.clear();st.run='copy';st.epoch=20;st.invalidRun=null;conn.status='online';conn.validSelection=true;conn.homeOk=true;conn.snapshotOk=true;conn.eventsOk=true;st.cache.set('copy',mergeDetail(undefined,{run_id:'copy',task_id:'copy',cwd:'/fixture',revision:1,status:'reported',decision:null,superseded_by:null},Date.now()));");
  await byId('copyPromptBtn').onclick();
  assert.equal(copies,1);
  run("st.cache.get('copy').detailAt=Date.now()-70000");
  await byId('copyPromptBtn').onclick();
  assert.equal(copies,1,'an old snapshot must not authorize a copied instruction');
  run("st.cache.get('copy').detailAt=Date.now();conn.status='offline'");
  await byId('copyPromptBtn').onclick();
  assert.equal(copies,1,'offline reads must not authorize a copied instruction');
  run("conn.status='online';st.selInFlight=false");
  context.fetch=async(url)=>({ok:false,status:401,json:async()=>({error:'expired'})});
  const beforeTimers=scheduled.length;
  await run('selTick()');
  assert.equal(run('conn.status'),'token_invalid');
  assert.equal(scheduled.length,beforeTimers,'401 must stop selected-run retry scheduling');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        source = 'const SOURCE=' + __import__('json').dumps(script) + ';\n' + harness
        result = subprocess.run([node, '-'], input=source, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
