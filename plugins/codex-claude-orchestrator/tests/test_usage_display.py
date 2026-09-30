"""Render usage through the actual dashboard script: scopes stay labelled and totals come from the CLI."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts"))
import usage  # noqa: E402

HARNESS = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
function makeClassList(){const set=new Set();return {add(...n){for(const x of n)set.add(x);},remove(...n){for(const x of n)set.delete(x);},
  toggle(name,force){if(force===undefined){if(set.has(name)){set.delete(name);return false;}set.add(name);return true;}
    if(force){set.add(name);return true;}set.delete(name);return false;},contains(name){return set.has(name);}};}
let doc=null;
class Node {
  constructor(){this.children=[];this.dataset={};this.attrs={};this.style={};this._value='';this.textContent='';this.disabled=false;this.classList=makeClassList();}
  get value(){return this._value;} set value(v){this._value=v;}
  get options(){return this.children;}
  append(...x){this.children.push(...x);} replaceChildren(...x){this.children=x;}
  setAttribute(name,v){this.attrs[name]=String(v);} getAttribute(name){return Object.hasOwn(this.attrs,name)?this.attrs[name]:null;}
  addEventListener(){} querySelector(){return new Node();} querySelectorAll(){return [];} focus(){if(doc)doc.activeElement=this;}
}
function allText(node){if(!node)return '';let t=node.textContent||'';for(const c of (node.children||[]))t+=' '+allText(c);return t;}
const nodes=new Map();const byId=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
doc={documentElement:new Node(),activeElement:new Node(),body:new Node(),hidden:false,
  getElementById:byId,createElement:()=>new Node(),createTextNode:text=>({textContent:String(text)}),querySelectorAll:()=>[],addEventListener(){}};
const context=vm.createContext({URL,URLSearchParams,AbortSignal,AbortController,console,setTimeout(){return 0;},clearTimeout(){},
  getComputedStyle:()=>({display:'none'}),location:{hash:'#token=fixture&run=',origin:'http://127.0.0.1'},history:{replaceState(){}},
  navigator:{},window:{addEventListener(){},open(){}},document:doc});
vm.runInContext(SOURCE,context);
const run=s=>vm.runInContext(s,context);
run("conn.status='online';conn.validSelection=true;conn.homeOk=true;");
const row=(id,extra)=>JSON.stringify({run_id:id,task_id:id,cwd:'/fx',revision:1,status:'reported',decision:null,superseded_by:null,objective:'Task '+id,...extra});
function shown(id,extra){run(`st.cache.set('${id}',mergeDetail(undefined,${row(id,extra)},Date.now()));st.run='';selectRun('${id}')`);return byId('usageText').textContent;}
'''

MODEL_USAGE = {
    "claude-opus-5-5": {"inputTokens": 12, "outputTokens": 2413, "cacheReadInputTokens": 88420,
                        "cacheCreationInputTokens": 18049, "costUSD": 0.21038400000000002},
    "claude-sonnet-5-5": {"inputTokens": 12, "outputTokens": 2879, "cacheReadInputTokens": 99890,
                          "cacheCreationInputTokens": 26186, "costUSD": 0.114257},
}
FINAL = {"usage": {"input_tokens": 4, "cache_creation_input_tokens": 3516, "cache_read_input_tokens": 26004, "output_tokens": 1544},
         "modelUsage": MODEL_USAGE, "total_cost_usd": 0.32464100000000007}


class UsageDisplayTests(unittest.TestCase):
    def run_node(self, body: str, **values):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the executable UI regression")
        script = re.search(r"<script>(.*?)</script>", (ROOT / "assets/dashboard.html").read_text(encoding="utf-8"), re.S).group(1)
        constants = "".join(f"const {name}={json.dumps(value)};\n" for name, value in values.items())
        source = "const SOURCE=" + json.dumps(script) + ";\n" + HARNESS + constants + body
        completed = subprocess.run([node, "-"], input=source, text=True, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_summary_only_usage_does_not_claim_the_provider_omitted_it(self):
        self.run_node(r'''
run("renderReport({run_id:'summary',status:'reported'},false)");
assert.equal(byId('usageText').textContent,'详情未载入，用量待读取。');
run("renderReport({run_id:'detail',status:'reported',result:{}},true)");
assert.equal(byId('usageText').textContent,'Claude CLI 尚未提供可展示的用量。');
''')

    def test_reconciled_unknown_cards_agree_on_stopped_but_unaccepted(self):
        self.run_node(r'''
const item={run_id:'recovered',status:'unknown',reconciliation:{outcome:'confirmed_stopped'},result:{structured:{status:'completed',summary:'saved',evidence:[],checks:[],unresolved:[]}}};
context.recovered=item;
assert.match(run("reasonLine({state:'known',run:recovered},true).text"),/已核对停止.*未验收/);
assert.match(run("reportFact(recovered,true).label"),/已核对停止.*未验收/);
assert.doesNotMatch(run("reasonLine({state:'known',run:recovered},true).text"),/状态未知|暂不要重派/);
assert.match(run("reasonLine({state:'known',run:{status:'unknown'}},true).text"),/暂不要重派/);
''')

    def test_public_counterexample_shows_per_model_totals_and_labelled_final_usage(self):
        report = usage.usage_report(FINAL, result_events=2, resumed=False)
        result = {"structured": {"status": "completed", "summary": "ok", "evidence": [], "checks": [], "unresolved": []},
                  "usage": FINAL["usage"], "usage_summary": FINAL["usage"], "total_cost_usd": FINAL["total_cost_usd"],
                  "model_usage": MODEL_USAGE, "usage_report": report,
                  "content_binding": {"digest": "ab" * 32, "mode": "active", "content_version": "2.0"}}
        self.run_node(r'''
const text=shown('run-usage',{result:RESULT,content_binding:RESULT.content_binding});
const lines=text.split('\n');
assert.equal(lines[0],'CLI 会话累计估算（含子代理，按模型）：');
assert.equal(lines[1],'· claude-opus-5-5：未缓存输入 12 · 输出 2413 · 缓存读取 88420 · 缓存写入 18049 · 估算 $0.2104');
assert.equal(lines[2],'· claude-sonnet-5-5：未缓存输入 12 · 输出 2879 · 缓存读取 99890 · 缓存写入 26186 · 估算 $0.1143');
assert.equal(lines[3],'合计：未缓存输入 24 · 输出 5292 · 缓存读取 188310 · 缓存写入 44235 · 估算 $0.3246');
assert.equal(lines[4],'主代理最终回报（不含子代理，不是整任务总量）：未缓存输入 4 · 输出 1544 · 缓存读取 26004 · 缓存写入 3516');
assert.match(lines.at(-1),/客户端估算，不是实际扣费或订阅剩余额度/);
assert.doesNotMatch(text,/\$0\.6493|6836|Provider 回报/,'repeated results or the two scopes are never added together');
assert.doesNotMatch(text,/续跑/);
assert.match(allText(byId('identityBody')),/协调内容\s+已审查的动态内容 · 2\.0 · abababababab/);
''', RESULT=result)

    def test_resumed_session_and_incomplete_values_stay_explicit(self):
        final = {"usage": {"input_tokens": 0}, "total_cost_usd": 0,
                 "modelUsage": {"m1": {"inputTokens": 0, "outputTokens": 5, "costUSD": 0}}}
        report = usage.usage_report(final, result_events=1, resumed=True)
        no_models = usage.usage_report({"usage": {"input_tokens": 2}, "total_cost_usd": 0.5}, result_events=1, resumed=False)
        self.run_node(r'''
const text=shown('run-resume',{result:{usage_report:RESUMED}});
assert.match(text,/· m1：未缓存输入 0 · 输出 5 · 缓存读取 未知 · 缓存写入 未知 · 估算 \$0\.0000/);
assert.match(text,/合计：未缓存输入 0 · 输出 5 · 缓存读取 未知 · 缓存写入 未知 · 估算 \$0\.0000/);
assert.match(text,/本轮为续跑：CLI 会话累计可能包含此前轮次的支出，不是本轮新增，也不能与其他轮相加。/);
assert.match(text,/主代理最终回报（不含子代理，不是整任务总量）：未缓存输入 0 · 输出 未知/);
const missing=shown('run-no-models',{result:{usage_report:NO_MODELS}});
assert.match(missing,/CLI 会话累计估算（含子代理）：\$0\.5000；CLI 未提供按模型统计，整任务 token 合计未知。/);
assert.doesNotMatch(missing,/合计：/);
assert.equal(shown('run-no-result',{result:{usage_report:null}}),'Claude CLI 尚未提供可展示的用量。');
''', RESUMED=report, NO_MODELS=no_models)

    def test_legacy_record_is_not_turned_into_a_task_total(self):
        legacy = {"usage_summary": {"input_tokens": 4, "output_tokens": 1544}, "total_cost_usd": 0.32464100000000007}
        self.run_node(r'''
const text=shown('run-legacy',{result:LEGACY});
assert.match(text,/主代理最终回报（旧记录，统计范围未确认，不是整任务总量）：未缓存输入 4 · 输出 1544 · 缓存读取 未知 · 缓存写入 未知/);
assert.match(text,/CLI 会话累计估算 \$0\.3246（旧记录：可能含子代理，与上面的 token 不是同一统计范围；未记录按模型分项）/);
assert.doesNotMatch(text,/合计/);
assert.equal(shown('run-empty',{result:{}}),'Claude CLI 尚未提供可展示的用量。');
assert.match(allText(byId('identityBody')),/协调内容\s+未固定（本轮记录未提供）/);
''', LEGACY=legacy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
