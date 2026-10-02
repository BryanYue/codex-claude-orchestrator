"""Counterexamples from the 2026-09-29 review (F01-F06, F11-F20, C01).

Each test reproduces the reported trigger against production code and checks
the corrected behavior.  UI cases execute the real dashboard script in Node.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts"))
import bridge  # noqa: E402
import cli_store  # noqa: E402
import diagnostics  # noqa: E402

BRIDGE = ROOT / "skills/codex-claude-orchestrator/scripts/bridge.py"
FAKE_CLI = """#!/usr/bin/env python3
import json, os, pathlib, sys
a = sys.argv[1:]
if a == ['--version']: print('2.1.284 (Claude Code)'); raise SystemExit
if a == ['--help']: print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence'); raise SystemExit
if a == ['auth', 'status', '--json']: print(json.dumps({'loggedIn': True})); raise SystemExit
session = a[a.index('--session-id') + 1]
sys.stdin.buffer.read()
target = os.environ.get('FAKE_WRITE')
if target: pathlib.Path(target).write_text('written by fixture\\n')
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': session, 'model': 'fixture'}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': session,
                  'structured_output': {'status': 'completed', 'summary': 'done', 'evidence': [], 'checks': [], 'unresolved': []}}))
"""


class StoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / "store"
        self.env = {"HOME": str(self.base), "PATH": "/usr/bin:/bin", "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.store)}

    def tearDown(self):
        self.tmp.cleanup()


    def test_f02_version_probe_failure_keeps_exit_code_signal_and_bounded_stderr(self):
        failing = self.base / "claude-fail"
        failing.write_text("#!/bin/sh\necho 'version helper failed token=sk-ant-SECRET' >&2\nexit 3\n")
        failing.chmod(0o755)
        report = bridge.check_environment(self.base, selection={"path": str(failing), "source": "CLAUDE_BIN"})
        self.assertEqual(report["status"], "cli_unavailable")
        probe = report["cli"]["version_probe"]
        self.assertEqual((probe["exit_code"], probe["error_kind"]), (3, "nonzero_exit"))
        self.assertIn("version helper failed", probe["stderr_excerpt"])
        self.assertNotIn("sk-ant-SECRET", json.dumps(report))

    def retained_identity(self, version: str, content: bytes, *, record_size: bool) -> str:
        digest = hashlib.sha256(content).hexdigest()
        identity_id = cli_store._identity_id_for("darwin", "arm64", version, digest)
        folder = self.store / "versions" / identity_id
        folder.mkdir(parents=True)
        (folder / "claude").write_bytes(content)
        (folder / "claude").chmod(0o700)
        metadata = {"schema_version": 1, "id": identity_id, "kind": "native_macho", "version": version,
                    "sha256": digest, "platform": "darwin", "machine": "arm64", "source": "fixture",
                    "created_at": 1.0, "codesign_verified": True}
        if record_size:
            metadata["size"] = len(content)
        (folder / "identity.json").write_text(json.dumps(metadata))
        return identity_id

    def test_f06_retained_file_size_reaches_inventory_and_diagnostics(self):
        measured = self.retained_identity("2.1.278", b"x" * 4096, record_size=True)
        legacy = self.retained_identity("2.1.277", b"y" * 100, record_size=False)
        records = cli_store.legacy_records(self.env)
        by_id = {item["id"]: item for item in records["versions"]}
        self.assertEqual(by_id[measured]["size"], 4096)
        self.assertEqual(by_id[legacy]["size_status"], "not_recorded")
        self.assertEqual(records["storage_summary"]["retained_bytes"], 4096)
        self.assertEqual(records["storage_summary"]["unmeasured_versions"], 1)
        with patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.store)}):
            summary = diagnostics._legacy_records()["storage_summary"]
        self.assertEqual(summary["retained_bytes"], 4096)


class SubdirectoryScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "package").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "t@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "T"], check=True)
        (self.repo / "package" / "keep.txt").write_text("keep\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.requirement = self.root / "spec.md"; self.requirement.write_text("spec\n")
        self.fake = self.root / "claude"; self.fake.write_text(FAKE_CLI); self.fake.chmod(0o755)
        self.env = {**os.environ, "CLAUDE_BIN": str(self.fake), "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
                    "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "store")}
        self.env.pop("FAKE_WRITE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_bridge(self, name: str, role: str, owned: list[str], write: str | None = None) -> Path:
        packet = {"task_id": name, "revision": 1, "role": role, "cwd": str(self.repo / "package"), "objective": "fixture",
                  "requirement_sources": [str(self.requirement)], "constraints": [], "acceptance": [],
                  "owned_files": owned, "protected_files": [], "model": "fixture", "effort": "low"}
        packet_path = self.root / f"{name}.json"; packet_path.write_text(json.dumps(packet))
        run_dir = self.root / name
        environment = dict(self.env)
        if write:
            environment["FAKE_WRITE"] = write
        subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path), "--run-dir", str(run_dir),
                        "--timeout", "10"], env=environment, capture_output=True, text=True)
        return run_dir

    def test_f03_owned_file_in_git_subdirectory_is_in_scope_and_hashed(self):
        run = self.run_bridge("owned", "implement", ["outside.txt"], write="outside.txt")
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported",
                         (run / "result.json").read_text() if (run / "result.json").exists() else "")
        after = json.loads((run / "workspace_after.json").read_text())
        self.assertEqual([entry["path"] for entry in after["status_entries"]], ["outside.txt"])
        self.assertNotEqual(after["dirty_content_hashes"]["outside.txt"], "missing")
        self.assertEqual(after["git_prefix"], "package/")

    def test_f03_change_outside_the_subdirectory_is_still_out_of_scope(self):
        run = self.run_bridge("escape", "implement", ["outside.txt"], write="../escape.txt")
        result = json.loads((run / "result.json").read_text())
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "failed")
        self.assertIn("../escape.txt", result["scope_error"])

    def test_f03_read_only_review_in_subdirectory_still_reports(self):
        (self.repo / "dirty.txt").write_text("pre-existing root change\n")
        run = self.run_bridge("review", "review", [])
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")
        before = json.loads((run / "workspace_before.json").read_text())
        self.assertEqual(before["status_entries"][0]["path"], "../dirty.txt")
        self.assertNotEqual(before["dirty_content_hashes"]["../dirty.txt"], "missing")


class ServerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_f04_created_run_survives_status_timeout(self):
        import server
        rt = Mock()
        rt.start.return_value = {"run_id": "run-created", "status": "starting", "claude_started": False}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(server, "runtime", rt), \
             patch.object(server, "viewer", Mock(url=lambda *_: "http://127.0.0.1/fixture")), \
             patch.object(server.cli_updates, "status", side_effect=subprocess.TimeoutExpired(["sysctl"], 5)):
            created = await server.claude_start({"workspace_kind": "artifacts", "cwd": tmp})
        self.assertEqual(created["run_id"], "run-created")
        self.assertNotIn("cli_maintenance", created)

    async def test_f05_shutdown_closes_viewer_even_when_runtime_close_fails(self):
        import server
        runtime_instance = Mock(); runtime_instance.close.side_effect = RuntimeError("runtime close failed")
        viewer_instance = Mock(); viewer_instance.start.return_value = viewer_instance
        self.assertFalse(hasattr(server, "maintain_supported_cli"))
        with patch.object(server, "Runtime", return_value=runtime_instance), \
             patch.object(server, "Viewer", return_value=viewer_instance):
            with self.assertRaisesRegex(RuntimeError, "runtime close failed"):
                async with server.lifespan(None):
                    pass
        runtime_instance.close.assert_called_once()
        viewer_instance.close.assert_called_once()

    async def test_f05_viewer_start_failure_still_closes_runtime(self):
        import server
        runtime_instance = Mock()
        failing_viewer = Mock(); failing_viewer.start.side_effect = OSError("port unavailable")
        with patch.object(server, "Runtime", return_value=runtime_instance), \
             patch.object(server, "Viewer", return_value=failing_viewer):
            with self.assertRaises(OSError):
                async with server.lifespan(None):
                    pass
        runtime_instance.close.assert_called_once()


class DocumentationRegressionTests(unittest.TestCase):
    ENTRY_DOCS = ["README.md", "README.en.md", "docs/advanced-usage.zh-CN.md", "docs/getting-started.zh-CN.md",
                  "docs/getting-started.en.md", "docs/install-and-recovery.md", "docs/git-marketplace.md"]
    SKILL_DOCS = ["skills/codex-claude-orchestrator/SKILL.md", "skills/codex-claude-orchestrator/references/guide.md",
                  "skills/codex-claude-orchestrator/references/bridge.md",
                  "skills/codex-claude-orchestrator/references/runtime-design.md"]

    def texts(self):
        for relative in self.ENTRY_DOCS:
            yield relative, (REPO / relative).read_text(encoding="utf-8")
        for relative in self.SKILL_DOCS:
            yield relative, (ROOT / relative).read_text(encoding="utf-8")

    def test_f11_no_entry_point_instructs_enabling_latest_or_managed_versions(self):
        for relative, text in self.texts():
            with self.subTest(document=relative):
                for forbidden in ("auto_qualify", "channel=latest", "claude_cli_update(action=prepare",
                                  "claude_cli_update prepare", "cli_profile_unverified"):
                    self.assertNotIn(forbidden, text)

    def test_local_cli_policy_is_stated_in_user_and_skill_entry_points(self):
        for relative in ("README.md", "docs/advanced-usage.zh-CN.md", "docs/getting-started.zh-CN.md"):
            self.assertIn("本机", (REPO / relative).read_text(encoding="utf-8"), relative)
        for relative in ("README.en.md", "docs/install-and-recovery.md", "docs/getting-started.en.md"):
            self.assertIn("local claude", (REPO / relative).read_text(encoding="utf-8").lower(), relative)
        for relative in ("skills/codex-claude-orchestrator/SKILL.md", "skills/codex-claude-orchestrator/references/guide.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("本机", text, relative)
            self.assertIn("fresh", text, relative)

    def test_advanced_usage_describes_the_collapsible_workbench_not_removed_modes(self):
        text = (REPO / "docs/advanced-usage.zh-CN.md").read_text(encoding="utf-8")
        for removed in ("简洁页", "完整页", "简洁卡片", "切完整模式", "默认简洁详情", "**完整模式**"):
            self.assertNotIn(removed, text)
        self.assertIn("可展开", text)
        self.assertIn("三项事实", text)

    def test_install_entry_points_match_the_release_and_preserve_old_ref_boundary(self):
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        release_ref = "v" + manifest["version"].split("+", 1)[0]
        documents = ["README.md", "docs/getting-started.zh-CN.md", "README.en.md",
                     "docs/getting-started.en.md", "docs/git-marketplace.md"]
        for relative in documents:
            with self.subTest(document=relative):
                text = (REPO / relative).read_text(encoding="utf-8")
                self.assertIn("v0.5.0", text, "old fixed refs must remain distinguished from the new release")
                refs = set(re.findall(r"--ref (\S+)", text))
                self.assertIn(release_ref, refs)
                self.assertLessEqual(refs, {release_ref, "FULL_COMMIT_SHA_OR_TAG"},
                                     "installation must match the packaged release, never a moving branch")
                self.assertNotIn("尚未发布", text)
                self.assertNotIn("unreleased", text)



HARNESS = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
function makeClassList(){
  const set=new Set();
  return {add(...n){for(const x of n)set.add(x);},remove(...n){for(const x of n)set.delete(x);},
    toggle(name,force){if(force===undefined){if(set.has(name)){set.delete(name);return false;}set.add(name);return true;}
      if(force){set.add(name);return true;}set.delete(name);return false;},
    contains(name){return set.has(name);}};
}
let doc=null;
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
  focus(){if(doc)doc.activeElement=this;}
}
function allText(node){if(!node)return '';let t=node.textContent||'';for(const c of (node.children||[]))t+=' '+allText(c);return t;}
const nodes=new Map();const byId=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
let scheduled=[];const windowListeners={};
doc={documentElement:new Node(),activeElement:new Node(),body:new Node(),hidden:false,
  getElementById:byId,createElement:()=>new Node(),createTextNode:text=>({textContent:String(text)}),querySelectorAll:()=>[],addEventListener(){}};
const location={hash:'#token=fixture&run=',origin:'http://127.0.0.1'};
const context=vm.createContext({URL,URLSearchParams,AbortSignal,AbortController,console,
  setTimeout(fn,ms){scheduled.push({fn,ms});return scheduled.length;},clearTimeout(){},
  getComputedStyle:()=>({display:'none'}),location,history:{replaceState(){}},
  navigator:{},window:{addEventListener(name,fn){windowListeners[name]=fn;},open(){}},document:doc});
vm.runInContext(SOURCE,context);
const run=s=>vm.runInContext(s,context);
const row=(id,extra)=>JSON.stringify({run_id:id,task_id:id,cwd:'/fx',revision:1,status:'reported',decision:null,superseded_by:null,objective:'Task '+id,...extra});
(async()=>{
  run("conn.status='online';conn.validSelection=true;conn.homeOk=true;");

  // C01: switching to an unloaded or invalid run clears the previous report and identity.
  run(`st.cache.set('run-a',mergeDetail(undefined,${row('run-a',{result:{structured:{summary:'OLD REPORT',evidence:[],checks:[],unresolved:[]}}})},Date.now()));st.loadedOrder=['run-a'];st.loadedSet=new Set(st.loadedOrder);`);
  run("selectRun('run-a')");
  assert.ok(allText(byId('reportBody')).includes('OLD REPORT'));
  run("selectRun('run-missing')");
  assert.equal(byId('detailTitle').textContent,'正在读取执行记录');
  assert.match(byId('runIdLabel').textContent,/run-missing/);
  assert.ok(!allText(byId('reportBody')).includes('OLD REPORT'),'another run report must not remain visible');
  assert.equal(byId('identityBody').children.length,0);
  assert.equal(byId('technicalFields').children.length,0);
  run("st.invalidRun='run-missing';renderDetail();");
  assert.equal(byId('detailTitle').textContent,'该执行记录无法读取');
  assert.ok(!allText(byId('reportBody')).includes('OLD REPORT'));
  assert.ok(!allText(byId('technicalFields')).includes('run-a'));

  // F13: unknown process state with a loaded structured report shows the recorded content.
  run(`st.cache.set('run-u',mergeDetail(undefined,${row('run-u',{status:'unknown',result:{structured:{summary:'RECORDED UNKNOWN REPORT',evidence:['e1'],checks:[],unresolved:[]}}})},Date.now()));`);
  assert.equal(run("reportFact(viewOf(st.cache.get('run-u')).view,true).kind"),'unknown_with_content');
  assert.equal(run("reportFact({status:'unknown'},false).kind"),'not_loaded');
  run("selectRun('run-u')");
  const unknownText=allText(byId('reportBody'));
  assert.ok(unknownText.includes('RECORDED UNKNOWN REPORT'));
  assert.ok(!unknownText.includes('未载入'),'loaded content must not be called not loaded');
  assert.match(unknownText,/不能据此验收/);
  assert.match(run("nextStepFor({state:'known',run:{run_id:'run-u',status:'unknown'}},true).prompt"),/暂不重派/);

  // F16: permission denial objects are readable, with a bounded JSON fallback.
  run(`st.cache.set('run-d',mergeDetail(undefined,${row('run-d',{status:'failed',result:{structured:null,permission_denials:[{type:'result',permission_denials:[{tool_name:'Read',tool_use_id:'t1',tool_input:{file_path:'/outside/secret.txt'}}]},{unknown_shape:true}]}})},Date.now()));selectRun('run-d')`);
  const denialText=allText(byId('reportBody'));
  assert.ok(!denialText.includes('[object Object]'));
  assert.match(denialText,/Read · \/outside\/secret\.txt/);
  assert.match(denialText,/\{"unknown_shape":true\}/);

  // F19: execution evidence source is shown without upgrading lifecycle facts.
  for(const [value,expected] of [['provider_event',/已观察到 Claude CLI 响应消息；不单独证明模型调用成功/],['bridge_lifecycle',/只有本机执行管理记录，尚未观察到 Claude 响应/],
      ['unconfirmed',/证据不足，未确认/],['surprise_kind',/未确认 · 未识别的证据类型/],[undefined,/未确认 · 本轮记录未提供/]]){
    run(`st.cache.set('run-e',mergeDetail(undefined,${row('run-e',{})},Date.now()));st.cache.get('run-e').detail.execution_evidence=${JSON.stringify(value??null)};st.run='';selectRun('run-e')`);
    const identity=allText(byId('identityBody'));
    assert.match(identity,expected);
    assert.doesNotMatch(identity,/provider_event|bridge_lifecycle|unconfirmed|surprise_kind/,'identity shows readable wording; the raw value stays in the technical record');
    if(value)assert.ok(allText(byId('technicalFields')).includes(value),'raw execution_evidence stays traceable: '+value);
  }
  assert.match(allText(byId('technicalFields')),/执行证据来源/);
  // A bound pre-dispatch lifecycle exists even when preflight prevented the child from starting.
  run(`st.cache.set('run-preflight-evidence',mergeDetail(undefined,${row('run-preflight-evidence',{status:'blocked',claude_started:false,receipt:{blocked_by:'preflight'},execution_evidence:'bridge_lifecycle'})},Date.now()));st.run='';selectRun('run-preflight-evidence')`);
  assert.match(allText(byId('facts')),/Claude 未启动/);
  assert.match(allText(byId('identityBody')),/只有本机执行管理记录/);
  assert.doesNotMatch(allText(byId('identityBody')),/已启动|执行中|已完成/,'management evidence must not contradict the preflight stop');

  run(`st.cache.set('run-i',mergeDetail(undefined,${row('run-i',{cli_identity:'identity-fixture',requested_model:'model-fixture',requested_effort:'high'})},Date.now()));st.run='';selectRun('run-i')`);
  const identityText=allText(byId('identityBody')),technicalText=allText(byId('technicalFields'));
  assert.match(identityText,/CLI 标识/);assert.match(identityText,/请求推理档位/);
  assert.doesNotMatch(identityText,/CLI identity|请求 effort/);
  assert.match(technicalText,/CLI identity（原始）\s+identity-fixture/);
  assert.match(technicalText,/requested model \/ effort（原始）\s+model-fixture \/ high/);

  // F15: display truncation and in-progress writes are disclosed.
  context.fetch=async()=>({ok:true,json:async()=>({name:'result',available:true,state:'available',content:'x'.repeat(300000),truncated:true,size_bytes:400000,display_limit_bytes:300000})});
  await run("loadArtifact('result',$('technicalArtifact'))");
  assert.match(byId('technicalArtifact').textContent,/展示已截断：仅显示前 300000 字节（共 400000 字节）；完整记录保留在本机/);
  assert.ok(!byId('technicalArtifact').textContent.includes('报告被截断'));
  context.fetch=async()=>({ok:true,json:async()=>({name:'packet',available:false,state:'writing',content:null,note:'Artifact is being written'})});
  await run("loadArtifact('packet',$('technicalArtifact'))");
  assert.match(byId('technicalArtifact').textContent,/正在写入/);
  context.fetch=async()=>({ok:true,json:async()=>({name:'receipt',available:false,state:'missing',content:null})});
  await run("loadArtifact('receipt',$('technicalArtifact'))");
  assert.match(byId('technicalArtifact').textContent,/尚未产生/);

  // F14: a failed status read survives opening the drawer; old data is labelled stale.
  run("st.maintenance={state:'local_cli',message:'OLD LOCAL STATUS'};st.maintenanceAt=Date.now()-60000;st.maintenanceFailure=null;");
  context.fetch=async()=>{throw new Error('network down');};
  await run('maintenanceTick()');
  assert.match(byId('drawerMaintenance').textContent,/版本维护状态暂不可用/);
  byId('maintenanceBtn').onclick();
  assert.match(byId('drawerMaintenance').textContent,/版本维护状态暂不可用/,'opening the drawer must not restore the old success');
  assert.match(byId('drawerMaintenanceDetail').textContent,/可能已过时.*OLD LOCAL STATUS/);
  assert.equal(byId('maintenanceDot').classList.contains('unavailable'),true);
  assert.notEqual(run('conn.status'),'offline');
  context.fetch=async()=>({ok:true,json:async()=>({state:'local_cli',message:'FRESH STATUS'})});
  await run('maintenanceTick()');
  assert.match(byId('drawerMaintenance').textContent,/FRESH STATUS/);

  // F20: a new token after 401 restarts local CLI polling; an old response cannot win.
  context.fetch=async()=>({ok:false,status:401,json:async()=>({error:'expired'})});
  await run('homeTick()');
  assert.equal(run('conn.status'),'token_invalid');
  let release;const pendingOld=new Promise(resolve=>{release=resolve;});
  context.fetch=async()=>{await pendingOld;return {ok:true,json:async()=>({state:'local_cli',message:'STALE GENERATION'})};};
  run("conn.status='online'");
  const oldTick=run('maintenanceTick()');
  run("conn.status='token_invalid'");
  scheduled=[];
  location.hash='#token=renewed&run=run-e';
  windowListeners.hashchange();
  const names=scheduled.map(item=>item.fn.name);
  assert.ok(names.includes('maintenanceTick'),'reconnect must schedule local CLI status: '+names.join(','));
  release();await oldTick;
  assert.ok(!byId('drawerMaintenance').textContent.includes('STALE GENERATION'));
  context.fetch=async()=>({ok:true,json:async()=>({state:'local_cli',message:'RENEWED STATUS'})});
  await scheduled.find(item=>item.fn.name==='maintenanceTick').fn();
  assert.match(byId('drawerMaintenance').textContent,/RENEWED STATUS/);

  // F12: a fresh home summary with a >60 s old detail is refreshed in the background.
  run("conn.status='online';st.run='';st.cache.clear();st.loadedOrder=['home-tip'];st.loadedSet=new Set(st.loadedOrder);st.extraIds=new Set();st.recentHomeIds=new Set(['home-tip']);st.prefetchQueue=new Set();st.backfillQueue=new Set();st.backfillStopped=new Set();st.bgInFlight=false;");
  run(`st.cache.set('home-tip',mergeDetail(undefined,${row('home-tip',{decision:{decision:'accepted'}})},Date.now()-61000));st.cache.set('home-tip',mergeSummary(st.cache.get('home-tip'),${row('home-tip',{decision:{decision:'accepted'}})},Date.now()));`);
  assert.equal(run("currentRoundOf(buildTasks().get(taskKey(viewOf(st.cache.get('home-tip')).view)),st.cache).state"),'pending');
  const refreshed=[];
  context.fetch=async(url)=>{refreshed.push(url.searchParams.get('run_id'));return {ok:true,json:async()=>JSON.parse(row('home-tip',{decision:{decision:'accepted'}}))};};
  await run('bgTick()');
  assert.deepEqual(refreshed,['home-tip']);
  assert.equal(run("currentRoundOf(buildTasks().get(taskKey(viewOf(st.cache.get('home-tip')).view)),st.cache).state"),'known');
  await run('bgTick()');
  assert.deepEqual(refreshed,['home-tip'],'a fresh detail is not refetched every turn');

  // F17: the list itself explains a first run with no tasks.
  run("st.cache.clear();st.loadedOrder=[];st.loadedSet=new Set();st.extraIds=new Set();st.baseline=null;st.search='';st.filter='all';renderList();");
  assert.match(allText(byId('taskGroups')),/正在读取任务记录/);
  run("onHomeSuccess({runs:[],next_offset:0,has_more:false},100000)");
  assert.match(allText(byId('taskGroups')),/还没有执行任务/);

  // F18: roving tabindex and arrow keys for the task list and round tabs.
  run(`for(const id of ['k1','k2','k3'])st.cache.set(id,mergeDetail(undefined,JSON.parse(${JSON.stringify(row('ID',{}))}.replaceAll('ID',id)),Date.now()));st.loadedOrder=['k1','k2','k3'];st.loadedSet=new Set(st.loadedOrder);st.run='';renderList();`);
  const cards=run('st.cardElements');
  assert.equal(cards.length,3);
  assert.deepEqual(Array.from(cards,c=>c.getAttribute('tabindex')),['0','-1','-1']);
  cards[0].focus();
  let prevented=false;
  byId('taskGroups').onkeydown({key:'ArrowDown',preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(doc.activeElement,cards[1]);
  assert.deepEqual(Array.from(cards,c=>c.getAttribute('tabindex')),['-1','0','-1']);
  byId('taskGroups').onkeydown({key:'End',preventDefault(){}});assert.equal(doc.activeElement,cards[2]);
  byId('taskGroups').onkeydown({key:'Home',preventDefault(){}});assert.equal(doc.activeElement,cards[0]);
  prevented=false;byId('taskGroups').onkeydown({key:'x',preventDefault(){prevented=true;}});assert.equal(prevented,false);
  run(`st.cache.set('r1',mergeDetail(undefined,{...${row('r1',{})},task_id:'rounds',superseded_by:'r2'},Date.now()));st.cache.set('r2',mergeDetail(undefined,{...${row('r2',{})},task_id:'rounds',revision:2},Date.now()));st.loadedOrder=['r1','r2'];st.loadedSet=new Set(st.loadedOrder);st.run='';selectRun('r2');`);
  const tabs=run('st.roundTabElements');
  assert.deepEqual(Array.from(tabs,t=>[t.dataset.runId,t.getAttribute('tabindex')]),[['r1','-1'],['r2','0']]);
  tabs[1].focus();
  byId('roundTabs').onkeydown({key:'ArrowLeft',preventDefault(){}});
  assert.equal(run('st.run'),'r1');
  assert.equal(doc.activeElement.dataset.runId,'r1','focus follows the newly selected round');
  byId('roundTabs').onkeydown({key:'End',preventDefault(){}});
  assert.equal(run('st.run'),'r2');

  // F18: an ordinary refresh re-renders the tabs and keeps focus on the same round only.
  run('st.roundTabElements')[1].focus();
  run('renderDetail()');
  assert.ok(run('st.roundTabElements').includes(doc.activeElement),'focus must not stay on a removed tab');
  assert.equal(doc.activeElement.dataset.runId,'r2');
  run('st.roundTabElements')[0].focus();
  run('renderDetail()');
  assert.ok(run('st.roundTabElements').includes(doc.activeElement));
  assert.equal(doc.activeElement.dataset.runId,'r1','refresh restores focus to the focused round, not the selected one');
  assert.equal(run('st.run'),'r2');
  const search=byId('searchInput');search.focus();
  run('renderDetail()');
  assert.equal(doc.activeElement,search,'refresh must not steal focus from other controls');
})().catch(error=>{console.error(error);process.exitCode=1});
'''


USABILITY_HARNESS = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
function makeClassList(){
  const set=new Set();
  return {add(...n){for(const x of n)set.add(x);},remove(...n){for(const x of n)set.delete(x);},
    toggle(name,force){if(force===undefined){if(set.has(name)){set.delete(name);return false;}set.add(name);return true;}
      if(force){set.add(name);return true;}set.delete(name);return false;},
    contains(name){return set.has(name);}};
}
let doc=null;const focusLog=[];
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
  // Records the scroll option: a browser scrolls a focused element into view unless preventScroll is set.
  focus(options){focusLog.push({node:this,preventScroll:Boolean(options&&options.preventScroll)});if(doc)doc.activeElement=this;}
}
function allText(node){if(!node)return '';let t=node.textContent||'';for(const c of (node.children||[]))t+=' '+allText(c);return t;}
const nodes=new Map();const byId=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
let scheduled=[];
doc={documentElement:new Node(),activeElement:new Node(),body:new Node(),hidden:false,
  getElementById:byId,createElement:()=>new Node(),createTextNode:text=>({textContent:String(text)}),querySelectorAll:()=>[],addEventListener(){}};
const context=vm.createContext({URL,URLSearchParams,AbortSignal,AbortController,console,
  setTimeout(fn,ms){scheduled.push({fn,ms});return scheduled.length;},clearTimeout(){},
  getComputedStyle:()=>({display:'none'}),location:{hash:'#token=fixture&run=',origin:'http://127.0.0.1'},history:{replaceState(){}},
  navigator:{},window:{addEventListener(){},open(){}},document:doc});
vm.runInContext(SOURCE,context);
const run=s=>vm.runInContext(s,context);
const row=(id,extra)=>JSON.stringify({run_id:id,task_id:id,cwd:'/fx',revision:1,status:'reported',decision:null,superseded_by:null,objective:'Task '+id,...extra});
const scrolls=()=>focusLog.map(f=>f.preventScroll?'kept':'scrolled');
const ready="conn.status='online';conn.validSelection=true;conn.homeOk=true;conn.snapshotOk=true;conn.eventsOk=true;";
const copyTimer=()=>{const found=scheduled.filter(t=>t.ms===2000);assert.ok(found.length,'copy feedback must schedule a 2 s reset');return found.at(-1);};
(async()=>{
  run(ready);
  run(`for(const id of ['k1','k2','k3'])st.cache.set(id,mergeDetail(undefined,JSON.parse(${JSON.stringify(row('ID',{}))}.replaceAll('ID',id)),Date.now()));st.loadedOrder=['k1','k2','k3'];st.loadedSet=new Set(st.loadedOrder);st.run='';st.filter='all';renderList();`);

  // WF-R11: arrow/Home/End navigation scrolls normally; a refresh restore never scrolls.
  let cards=run('st.cardElements');
  cards[0].focus();focusLog.length=0;
  for(const [key,index] of [['ArrowDown',1],['End',2],['Home',0]]){
    byId('taskGroups').onkeydown({key,preventDefault(){}});
    assert.equal(doc.activeElement,run('st.cardElements')[index]);
  }
  assert.deepEqual(scrolls(),['scrolled','scrolled','scrolled'],'deliberate card navigation keeps normal scrolling');
  byId('taskGroups').onkeydown({key:'ArrowDown',preventDefault(){}});
  const focusedTask=doc.activeElement.dataset.taskKey;
  for(let i=0;i<3;i++){
    focusLog.length=0;run('renderList()');
    assert.ok(run('st.cardElements').includes(doc.activeElement),'focus must move to the rebuilt card');
    assert.equal(doc.activeElement.dataset.taskKey,focusedTask);
    assert.deepEqual(scrolls(),['kept'],'refresh must restore card focus with preventScroll');
  }
  run(`st.cache.set('r1',mergeDetail(undefined,{...${row('r1',{})},task_id:'rounds',superseded_by:'r2'},Date.now()));st.cache.set('r2',mergeDetail(undefined,{...${row('r2',{})},task_id:'rounds',revision:2},Date.now()));st.loadedOrder=['k1','k2','k3','r1','r2'];st.loadedSet=new Set(st.loadedOrder);selectRun('r2');`);
  run('st.roundTabElements')[1].focus();
  for(const [key,expected] of [['ArrowLeft','r1'],['End','r2'],['Home','r1']]){
    focusLog.length=0;
    byId('roundTabs').onkeydown({key,preventDefault(){}});
    assert.equal(run('st.run'),expected);assert.equal(doc.activeElement.dataset.runId,expected);
    assert.deepEqual(scrolls(),['scrolled'],'deliberate round navigation keeps normal scrolling: '+key);
  }
  for(let i=0;i<2;i++){
    focusLog.length=0;run('renderDetail()');
    assert.ok(run('st.roundTabElements').includes(doc.activeElement));
    assert.equal(doc.activeElement.dataset.runId,'r1');
    assert.deepEqual(scrolls(),['kept'],'refresh must restore round focus with preventScroll');
  }

  // WF-R12: chips keep focus by key across refresh and after keyboard activation.
  run("selectRun('');st.filter='all';renderList();");
  const chip=key=>byId('filterChips').children.find(c=>c.dataset.focusKey===key);
  let before=chip('active');before.focus();focusLog.length=0;
  run('renderList()');
  assert.notEqual(chip('active'),before,'the fixture must really rebuild the chips');
  assert.equal(doc.activeElement,chip('active'));assert.deepEqual(scrolls(),['kept']);
  chip('attention').focus();chip('attention').onclick();
  assert.equal(run('st.filter'),'attention');
  for(let i=0;i<3;i++){
    run('renderList()');
    assert.equal(doc.activeElement,chip('attention'));assert.equal(doc.activeElement.getAttribute('aria-pressed'),'true');
  }
  const search=byId('searchInput');search.focus();
  run("st.filter='all';renderList();");
  assert.equal(doc.activeElement,search,'refresh must not move focus from another control onto a chip');

  // WF-R12: the new-task notice keeps its button focused; when it disappears focus lands on the selected card.
  run(`st.cache.set('n1',mergeDetail(undefined,${row('n1',{})},Date.now()));st.loadedOrder=['k1','k2','k3','n1'];st.loadedSet=new Set(st.loadedOrder);st.run='k1';st.newTasks=[taskKey(viewOf(st.cache.get('n1')).view)];renderList();`);
  const noticeButton=()=>byId('newTaskNotice').children.find(n=>n.dataset&&n.dataset.focusKey==='view');
  before=noticeButton();before.focus();focusLog.length=0;
  run('renderList()');
  assert.notEqual(noticeButton(),before);assert.equal(doc.activeElement,noticeButton());assert.deepEqual(scrolls(),['kept']);
  noticeButton().onclick();
  assert.equal(run('st.run'),'n1');
  assert.equal(byId('newTaskNotice').classList.contains('hidden'),true);
  assert.ok(run('st.cardElements').includes(doc.activeElement),'focus must not stay on the removed notice button');
  assert.equal(doc.activeElement.dataset.taskKey,run("taskKey(viewOf(st.cache.get('n1')).view)"));

  // WF-R12: artifact buttons keep focus by artifact name; a removed set falls back to its section summary.
  run(ready+'renderDetail()');
  const artifact=(root,name)=>byId(root).children.find(b=>b.dataset.focusKey===name);
  context.fetch=async()=>({ok:true,json:async()=>({name:'result',available:true,state:'available',content:'REPORT BODY',truncated:false})});
  artifact('technicalButtons','result').focus();
  await artifact('technicalButtons','result').onclick();
  assert.equal(byId('technicalArtifact').textContent,'REPORT BODY');
  for(let i=0;i<3;i++){
    before=doc.activeElement;focusLog.length=0;run('renderDetail()');
    assert.notEqual(artifact('technicalButtons','result'),before);
    assert.equal(doc.activeElement,artifact('technicalButtons','result'));assert.deepEqual(scrolls(),['kept']);
  }
  artifact('changesButtons','diff').focus();run('renderDetail()');
  assert.equal(doc.activeElement,artifact('changesButtons','diff'));
  const summary=new Node();byId('changesDetails').querySelector=()=>summary;
  run("st.invalidRun=st.run;renderDetail();");
  assert.equal(byId('changesButtons').children.length,0);
  assert.equal(doc.activeElement,summary,'a removed artifact button falls back to its section');
  assert.equal(focusLog.at(-1).preventScroll,true);
  run('st.invalidRun=null;');

  // WF-R13: copy feedback resets after 2 s, on selection change, and never lands on another run.
  context.navigator.clipboard={writeText:async()=>{}};
  run("selectRun('k1');"+ready+'renderDetail();');
  const runCopy=byId('copyRunId'),promptCopy=byId('copyPromptBtn');
  scheduled=[];await runCopy.onclick();
  assert.equal(runCopy.textContent,'已复制');
  copyTimer().fn();assert.equal(runCopy.textContent,'复制');
  context.navigator.clipboard={writeText:async()=>{throw new Error('denied');}};
  scheduled=[];await runCopy.onclick();
  assert.equal(runCopy.textContent,'请手动复制','clipboard failure still asks for a manual copy');
  copyTimer().fn();assert.equal(runCopy.textContent,'复制');
  context.navigator.clipboard={writeText:async()=>{}};
  scheduled=[];await runCopy.onclick();const oldTimer=copyTimer();
  run("selectRun('k2')");
  assert.equal(runCopy.textContent,'复制','switching runs resets the label immediately');
  scheduled=[];await runCopy.onclick();assert.equal(runCopy.textContent,'已复制');
  oldTimer.fn();
  assert.equal(runCopy.textContent,'已复制','an old timer must not cut short newer feedback');
  let release;context.navigator.clipboard={writeText:()=>new Promise(resolve=>{release=resolve;})};
  const pendingRunCopy=runCopy.onclick();
  run("selectRun('k3')");release();await pendingRunCopy;
  assert.equal(runCopy.textContent,'复制','a late clipboard result for the previous run must not label the new run');

  context.navigator.clipboard={writeText:async()=>{}};
  run("selectRun('k1');"+ready+'renderDetail();');
  scheduled=[];await promptCopy.onclick();
  assert.equal(promptCopy.textContent,'已复制');
  copyTimer().fn();assert.equal(promptCopy.textContent,'复制给 Codex');
  scheduled=[];await promptCopy.onclick();
  run("st.cache.set('k1',mergeDetail(st.cache.get('k1'),{...viewOf(st.cache.get('k1')).view,decision:{decision:'returned'}},Date.now()));renderDetail();");
  assert.equal(promptCopy.textContent,'复制给 Codex','a different next step must not inherit the old copy feedback');
  context.navigator.clipboard={writeText:()=>new Promise(resolve=>{release=resolve;})};
  const pendingPromptCopy=promptCopy.onclick();
  run("selectRun('k2')");release();await pendingPromptCopy;
  assert.equal(promptCopy.textContent,'复制给 Codex');
  run("selectRun('k1');"+ready+"st.cache.get('k1').detailAt=Date.now()-70000;");
  let copies=0;context.navigator.clipboard={writeText:async()=>{copies++;}};
  await promptCopy.onclick();
  assert.equal(copies,0,'the fresh-action gate still refuses an old snapshot');
  assert.equal(promptCopy.textContent,'复制给 Codex');

  run("conn.status='offline';conn.lastCriticalSyncAt=Date.now();renderBanner();");
  const recovery=byId('bannerArea').children[0].children.find(n=>n.textContent==='复制恢复指令');
  scheduled=[];await recovery.onclick();
  assert.equal(recovery.textContent,'已复制');
  copyTimer().fn();assert.equal(recovery.textContent,'复制恢复指令');
  run(ready);

  // WF-R14: a preflight block says "not started" only for claude_started=false, on all three surfaces.
  for(const started of [false,null,true]){
    const id='pre-'+String(started);
    run(`st.cache.set('${id}',mergeDetail(undefined,${row(id,{status:'blocked',receipt:{blocked_by:'preflight'},claude_started:started})},Date.now()));st.loadedOrder=['${id}'];st.loadedSet=new Set(st.loadedOrder);st.run='';selectRun('${id}');${ready}renderList();renderDetail();`);
    const card=run('st.cardElements')[0];
    const surfaces={facts:allText(byId('facts')),card:allText(card),nextStep:byId('nextStepBody').textContent};
    for(const [name,text] of Object.entries(surfaces)){
      if(started===false)assert.match(text,/Claude 未启动/,name);
      else{assert.doesNotMatch(text,/未启动/,`${name} for claude_started=${started}`);assert.match(text,/启动状态未确认/,name);}
    }
  }
})().catch(error=>{console.error(error);process.exitCode=1});
'''


ACTIVITY_HARNESS = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
function makeClassList(){
  const set=new Set();
  return {add(...n){for(const x of n)set.add(x);},remove(...n){for(const x of n)set.delete(x);},
    toggle(name,force){if(force===undefined){if(set.has(name)){set.delete(name);return false;}set.add(name);return true;}
      if(force){set.add(name);return true;}set.delete(name);return false;},
    contains(name){return set.has(name);}};
}
let doc=null;const focusLog=[];
class Node {
  constructor(tag){this.tag=tag||'';this.children=[];this.dataset={};this.attrs={};this.style={};this._value='';this.textContent='';
    this.disabled=false;this.open=false;this.listeners={};this.classList=makeClassList();}
  set innerHTML(v){throw new Error('innerHTML must not be used: '+v);}
  get value(){return this._value;} set value(v){this._value=v;}
  get options(){return this.children;}
  append(...x){this.children.push(...x);}
  replaceChildren(...x){this.children=x;}
  setAttribute(name,v){this.attrs[name]=String(v);}
  getAttribute(name){return Object.hasOwn(this.attrs,name)?this.attrs[name]:null;}
  addEventListener(name,fn){(this.listeners[name]=this.listeners[name]||[]).push(fn);}
  fire(name){for(const fn of this.listeners[name]||[])fn();}
  querySelector(){return new Node();}
  querySelectorAll(){return [];}
  focus(options){focusLog.push({node:this,preventScroll:Boolean(options&&options.preventScroll)});if(doc)doc.activeElement=this;}
}
function walk(node,fn){if(!node)return;fn(node);for(const c of node.children||[])walk(c,fn);}
function allText(node){if(!node)return '';let t=node.textContent||'';for(const c of (node.children||[]))t+=' '+allText(c);return t;}
const nodes=new Map();const byId=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
doc={documentElement:new Node(),activeElement:new Node(),body:new Node(),hidden:false,
  getElementById:byId,createElement:tag=>new Node(tag),createTextNode:text=>({textContent:String(text)}),querySelectorAll:()=>[],addEventListener(){}};
const context=vm.createContext({URL,URLSearchParams,AbortSignal,AbortController,console,
  setTimeout(){return 1;},clearTimeout(){},
  getComputedStyle:()=>({display:'none'}),location:{hash:'#token=fixture&run=',origin:'http://127.0.0.1'},history:{replaceState(){}},
  navigator:{},window:{addEventListener(){},open(){}},document:doc});
vm.runInContext(SOURCE,context);
const run=s=>vm.runInContext(s,context);
const J=expr=>JSON.parse(run(`JSON.stringify(${expr})`));
const info=e=>J(`activityInfo(${JSON.stringify(e)})`);
const detail=e=>J(`activityDetail(${JSON.stringify(e)})`);
const provider=status=>({seq:1,kind:'provider',summary:'provider lifecycle event',...(status===undefined?{}:{status})});
const row=(id,extra)=>JSON.stringify({run_id:id,task_id:id,cwd:'/fx',revision:1,status:'reported',decision:null,superseded_by:null,objective:'Task '+id,...extra});
(async()=>{
  // Every provider status recorded by the current and older bridges is described for what the CLI sent, nothing more.
  const providerCases={
    init:[/^Claude 会话已初始化$/,/不证明模型调用成功/],
    hook_started:[/^Claude CLI 钩子已开始$/,/不表示权限已验证通过/],
    hook_response:[/^Claude CLI 钩子已返回消息$/,/不表示权限已验证通过/],
    thinking_tokens:[/^生成用量已更新$/,/不含思考内容.*不表示分析已完成/],
    background_tasks_changed:[/^Claude CLI 后台任务有变化$/,/不表示任务成功/],
    task_updated:[/^Claude CLI 任务状态已更新$/,/不表示任务成功/],
    success:[/^Claude 已返回响应，等待插件检查$/,/不等于已回报、已验收或 Workflow 已完结/],
  };
  for(const [status,[text,note]] of Object.entries(providerCases)){
    const got=info(provider(status));
    assert.match(got.text,text,status);assert.match(got.note,note,status);
    assert.doesNotMatch(got.text,/未识别|验收|通过|权限|分析已完成/,`${status} wording must not overclaim`);
    assert.deepEqual(detail(provider(status)),[],'the English placeholder summary is not repeated as detail');
  }
  for(const status of ['brand_new_type','constructor','__proto__','toString']){
    const got=info(provider(status));
    assert.equal(got.text,`Claude CLI 状态消息：未识别类型（${status}）`,status);
    assert.match(got.note,/不推断其含义/);
  }
  assert.equal(info(provider(undefined)).text,'Claude CLI 状态消息：未识别类型');

  // A permission check is not a tool result; denials, Write and Edit are not hidden or mislabelled.
  const permit=(tool,extra)=>({seq:2,kind:'tool',summary:'tool path permitted',status:'allowed',tool,file:'a.txt',tool_use_id:'t1',...extra});
  for(const [tool,text] of [['Read','已允许读取'],['Glob','已允许查找文件'],['Grep','已允许搜索内容'],['Write','已允许写入'],['Edit','已允许编辑'],['Bash','已允许工具访问'],['constructor','已允许工具访问']]){
    const got=info(permit(tool));
    assert.equal(got.text,text,tool);
    assert.match(got.note,/权限检查结果，不证明工具已执行或成功/);
    assert.doesNotMatch(got.text,/成功|已执行|已完成/);
    assert.deepEqual(detail(permit(tool)),[tool,'a.txt']);
  }
  assert.equal(run("activityDescription({kind:'tool',summary:'tool path permitted',tool:'Grep'})"),'已允许搜索内容','legacy record without status still reads correctly');
  const workflowPermit={seq:3,kind:'tool',summary:'saved workflow permitted',tool:'Workflow',file:'review-flow',status:'allowed'};
  assert.equal(info(workflowPermit).text,'已允许运行指定 Workflow');
  assert.match(info(workflowPermit).note,/不代表 Workflow 已启动或完成/);
  assert.equal(info({kind:'tool',summary:'artifact file permitted',tool:'Read',status:'allowed'}).text,'已允许读取指定资料文件');
  assert.equal(info({kind:'tool',summary:'workflow structured output permitted',tool:'StructuredOutput',status:'allowed'}).text,'已允许提交结构化输出');
  const denied={seq:4,kind:'tool',summary:'tool path denied',tool:'Write',status:'denied',text:'write path is outside cwd',tool_use_id:'t2'};
  assert.equal(info(denied).text,'已拒绝工具访问');
  assert.deepEqual(detail(denied),['Write','拒绝原因：write path is outside cwd']);
  assert.equal(info({kind:'tool',summary:'something new'}).text,'未识别的工具事件');

  // Workflow lifecycle, sub-task label/state/tool/phase; repeated progress never reads as tool success or review pass.
  for(const [status,text,note] of [['task_started','Workflow 已启动',/不代表任务已完成/],['task_progress','Workflow 有进展',/不代表任务完成或审查通过/],
      ['completed','Workflow 通知已完成',/最终报告与验收仍须检查/],['task_notification','Workflow 收到任务通知',/未附状态/]]){
    const got=info({kind:'workflow',summary:'Review the module',status});
    assert.equal(got.text,text,status);assert.match(got.note,note,status);
    assert.deepEqual(detail({kind:'workflow',summary:'Review the module',status}),['Review the module']);
  }
  assert.match(info({kind:'workflow',status:'failed'}).text,/Workflow 状态未识别（failed）/);
  const agent=(state,extra)=>({seq:6,kind:'workflow_agent',summary:'reviewer-1',...(state===undefined?{}:{status:state}),...extra});
  assert.equal(info(agent('start')).text,'子任务「reviewer-1」已启动');
  assert.equal(info(agent('progress')).text,'子任务「reviewer-1」有进展');
  assert.equal(info(agent('done')).text,'子任务「reviewer-1」已交回');
  assert.match(info(agent('done')).note,/不代表其结果经过审查或通过/);
  assert.match(info(agent('progress')).note,/不代表工具成功或审查通过/);
  assert.equal(info(agent('exploded')).text,'子任务「reviewer-1」状态未识别（exploded）');
  assert.equal(info(agent(undefined)).text,'子任务「reviewer-1」状态未记录');
  assert.deepEqual(detail(agent('progress',{tool:'Read',text:'Phase A'})),['最近工具 Read','阶段 Phase A']);
  assert.deepEqual(detail(agent('progress')),[]);
  assert.doesNotMatch(info(agent('progress',{tool:'Read'})).text,/成功|通过|完成/);

  // Milestones that report trouble stay visible; an unknown kind is stated as unrecognised.
  for(const [kind,text] of [['blocked','已被阻止'],['failed','执行失败'],['cancelled','已取消或中断'],['timeout','已超时'],['unknown','状态未确认，需要核对'],['cancelling','已请求停止，尚待停止回执']]){
    assert.equal(info({kind,summary:'original wording',status:kind}).text,text,kind);
    assert.deepEqual(detail({kind,summary:'original wording'}),['original wording'],kind);
  }
  assert.equal(info({kind:'mystery_kind',summary:'did something'}).text,'未识别活动类型（mystery_kind）');
  assert.deepEqual(detail({kind:'mystery_kind',summary:'did something'}),['did something']);
  assert.equal(info({}).text,'未识别活动类型');
  assert.equal(info({kind:'reported'}).text,'Claude 已交回报告，等待核验');
  assert.equal(info({kind:'decision',status:'accepted'}).text,'Codex 已记录验收通过');

  // The task-card execution line uses the same wording.
  assert.match(run("execFact({status:'executing',last_meaningful_event:{seq:9,kind:'tool',summary:'tool path permitted',tool:'Write',status:'allowed',received_at:1790000000}},true).detail"),/已允许写入/);

  // Rendered rows: readable text, notes, traceable raw record, no private or unlisted fields, textContent only.
  run("conn.status='online';conn.validSelection=true;conn.homeOk=true;conn.snapshotOk=true;conn.eventsOk=true;");
  const events=[
    {seq:1,received_at:1790000001,kind:'provider',summary:'provider lifecycle event',status:'hook_started',thinking:'PRIVATE CHAIN OF THOUGHT',message:'PRIVATE MESSAGE'},
    {seq:2,received_at:1790000002,kind:'provider',summary:'provider lifecycle event',status:'success'},
    {seq:3,received_at:1790000003,kind:'tool',summary:'tool path permitted',tool:'Read',file:'README.md',status:'allowed',tool_use_id:'tu-1'},
    {seq:4,received_at:1790000004,kind:'workflow_agent',summary:'reviewer-1',status:'progress',tool:'Grep',text:'Phase A'},
    {seq:5,received_at:1790000005,kind:'tool',summary:'tool path denied',tool:'Write',status:'denied',text:'write path is outside cwd',tool_use_id:'tu-2'},
    {seq:6,received_at:1790000006,kind:'provider',summary:'provider lifecycle event',status:'weird_type'},
    {seq:7,received_at:1790000007,kind:'workflow',summary:'<img src=x onerror=alert(1)>',status:'task_started'},
  ];
  run(`st.cache.set('run-act',{...emptyEntry(),detail:{events_count:${events.length}},events:${JSON.stringify(events)}});st.run='run-act';`);
  const body=byId('activityBody'),filter=byId('activityFilter');
  body.tag='div'; // Match the real container, so the element allowlist checks the complete rendered subtree.
  const draw=(query)=>{filter.value=query;run("renderEvents(st.cache.get('run-act'))");return body.children.filter(c=>c.className==='event-row');};
  const seqsOf=rows=>rows.map(r=>Number(/seq=(\d+)/.exec(allText(r))[1]));
  let rows=draw('');
  assert.deepEqual(seqsOf(rows),[7,6,5,4,3,2,1],'newest first, every loaded record listed');
  assert.equal(byId('activityCount').textContent,'已载入 7 条 / 共 7 条','retained-record count semantics are unchanged');
  const all=allText(body);
  assert.doesNotMatch(all,/PRIVATE/,'unlisted fields such as thinking/message are never shown');
  assert.match(all,/Claude CLI 钩子已开始/);assert.match(all,/不表示权限已验证通过/);
  assert.match(all,/Claude 已返回响应，等待插件检查/);
  assert.match(all,/Claude CLI 状态消息：未识别类型（weird_type）/);
  assert.match(all,/已拒绝工具访问/);assert.match(all,/拒绝原因：write path is outside cwd/);
  assert.match(all,/子任务「reviewer-1」有进展/);assert.match(all,/最近工具 Grep · 阶段 Phase A/);
  assert.match(all,/<img src=x onerror=alert\(1\)>/,'workflow text is displayed literally');
  walk(body,n=>assert.ok(n.tag===undefined||['div','strong','time','details','summary','p'].includes(n.tag),'unexpected element '+n.tag));
  const rawOf=r=>{let found=null;walk(r,n=>{if(n.className==='raw-record')found=n;});return found;};

  const collapsedText=n=>n.tag==='details'&&!n.open?allText(n.children.find(c=>c.tag==='summary')):
    (n.textContent||'')+' '+(n.children||[]).map(collapsedText).join(' ');
  assert.doesNotMatch(collapsedText(body),/不表示权限已验证通过|不等于已回报/,'per-event qualifications stay in collapsed details');
  const providerRaw=allText(rawOf(rows.find(r=>/seq=1(?!\d)/.test(allText(r)))));
  assert.match(providerRaw,/原始记录/);
  assert.match(providerRaw,/kind=provider · status=hook_started · summary=provider lifecycle event/);
  assert.doesNotMatch(providerRaw,/PRIVATE|thinking|message=/);
  assert.match(allText(rawOf(rows.find(r=>/seq=3(?!\d)/.test(allText(r))))),/tool=Read · file=README\.md · summary=tool path permitted · tool_use_id=tu-1/);

  // Search matches the Chinese wording and the raw fields, case-insensitively, and never private or explanatory text.
  for(const [query,expected] of [['钩子',[1]],['hook_started',[1]],['已返回响应',[2]],['success',[2]],['README',[3]],['tu-1',[3]],['已允许读取',[3]],
      ['reviewer',[4]],['最近工具 grep',[4]],['phase a',[4]],['已拒绝',[5]],['outside cwd',[5]],['WEIRD_TYPE',[6]],['未识别类型',[6]],['task_started',[7]],['  钩子  ',[1]]]){
    assert.deepEqual(seqsOf(draw(query)),expected,`search ${query}`);
  }
  assert.equal(draw('private').length,0,'fields outside the public list are not searchable');
  assert.equal(draw('不表示权限已验证通过').length,0,'explanatory notes are not search text');
  assert.match(allText(body),/没有匹配的已载入活动/);
  draw('');

  // Refresh keeps an expanded raw record open and keeps focus on it without scrolling.
  const rawFor=seq=>{let found=null;for(const r of body.children.filter(c=>c.className==='event-row')){if(new RegExp(`seq=${seq}(?!\\d)`).test(allText(r)))found=rawOf(r);}return found;};
  let target=rawFor(3);
  target.open=true;target.fire('toggle');
  const summaryOf=raw=>raw.children.find(c=>c.tag==='summary');
  summaryOf(target).focus();focusLog.length=0;
  run("renderEvents(st.cache.get('run-act'))");
  const rebuilt=rawFor(3);
  assert.notEqual(rebuilt,target,'the fixture must really rebuild the rows');
  assert.equal(rebuilt.open,true,'an expanded raw record stays expanded across refresh');
  assert.equal(rawFor(2).open,false);
  assert.equal(doc.activeElement,summaryOf(rebuilt));
  assert.deepEqual(focusLog.map(f=>f.preventScroll),[true],'refresh restores focus with preventScroll');
  rebuilt.open=false;rebuilt.fire('toggle');
  run("renderEvents(st.cache.get('run-act'))");
  assert.equal(rawFor(3).open,false,'a collapsed raw record stays collapsed');
  doc.activeElement=byId('searchInput');focusLog.length=0;
  run("renderEvents(st.cache.get('run-act'))");
  assert.equal(focusLog.length,0,'refresh must not take focus from another control');

  // A provider success line and a permitted tool leave the run facts unverified and unreported.
  run(`st.cache.set('run-p',mergeDetail(undefined,${row('run-p',{status:'executing'})},Date.now()));st.cache.get('run-p').events=${JSON.stringify(events)};st.loadedOrder=['run-p'];st.loadedSet=new Set(st.loadedOrder);st.run='';selectRun('run-p');`);
  const facts=allText(byId('facts'));
  assert.match(facts,/执行中/);assert.match(facts,/尚无报告/);assert.match(facts,/未核验/);
  assert.doesNotMatch(facts,/通过|已验收|已回报/,'provider success and permitted tools are not acceptance or a report');
  assert.match(allText(byId('activityBody')),/Claude 已返回响应，等待插件检查/);
  assert.equal(run("latestPublicEvent({},[{seq:2,kind:'provider',status:'success'}])"),null,'a provider line is never the latest milestone');
})().catch(error=>{console.error(error);process.exitCode=1});
'''


class DashboardRegressionTests(unittest.TestCase):
    def run_harness(self, harness: str) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the executable UI regression")
        script = re.search(r"<script>(.*?)</script>", (ROOT / "assets/dashboard.html").read_text(), re.S).group(1)
        source = "const SOURCE=" + json.dumps(script) + ";\n" + harness
        result = subprocess.run([node, "-"], input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ui_counterexamples(self):
        self.run_harness(HARNESS)

    def test_usability_focus_copy_feedback_and_start_wording(self):
        self.run_harness(USABILITY_HARNESS)

    def test_activity_wording_evidence_limits_search_and_raw_traceability(self):
        self.run_harness(ACTIVITY_HARNESS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
