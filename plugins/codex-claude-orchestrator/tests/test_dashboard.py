"""Run the workbench script in Node against a minimal fake DOM and the 1.0 viewer payloads."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "assets/dashboard.html"

HARNESS = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
class Text { constructor(t) { this.nodeType = 3; this.textContent = String(t); } }
class El {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {}; this.listeners = {};
    this.className = ''; this.hidden = false; this.disabled = false; this.type = ''; this._text = ''; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get innerHTML() { throw new Error('innerHTML must not be used'); }
  set innerHTML(v) { throw new Error('innerHTML must not be used'); }
  append(...xs) { for (const x of xs) { if (!(x instanceof El || x instanceof Text)) throw new Error('append() got a non-node: ' + typeof x); this.children.push(x); } }
  replaceChildren(...xs) { this._text = ''; this.children = []; this.append(...xs); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.hasOwn(this.attrs, k) ? this.attrs[k] : null; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  click() { for (const f of this.listeners.click || []) f({target: this}); }
}
const ids = new Map();
const byId = id => { if (!ids.has(id)) ids.set(id, new El('div')); return ids.get(id); };
const pageText = () => [...ids.values()].map(n => n.textContent).join('\n');
const all = (node, pred, out = []) => { if (node instanceof El) { if (pred(node)) out.push(node); node.children.forEach(c => all(c, pred, out)); } return out; };
const docListeners = {};
const doc = {hidden: false, getElementById: byId, createElement: t => new El(t), createTextNode: t => new Text(t),
  addEventListener(t, f) { (docListeners[t] = docListeners[t] || []).push(f); }};
const pending = [], calls = [], timers = [], clip = [];
function fakeFetch(url, opts) { calls.push({url, opts}); return new Promise((resolve, reject) => pending.push({url, resolve, reject})); }
const loc = {hash: '#' + HASH, reload() { loc.reloaded = true; }};
const context = vm.createContext({URLSearchParams, fetch: fakeFetch, location: loc, document: doc,
  setTimeout: (f, ms) => { timers.push({f, ms}); return timers.length; }, clearTimeout() {},
  history: {replaceState(a, b, url) { loc.hash = url; }}, navigator: {clipboard: {writeText: async t => { clip.push(t); }}},
  window: {addEventListener() {}}});
const tick = () => new Promise(r => setImmediate(r));
async function respond(match, status, body) {
  await tick();
  const index = pending.findIndex(p => p.url.includes(match));
  if (index < 0) throw new Error('no pending request matching ' + match + '; pending: ' + pending.map(p => p.url).join(' | '));
  const [p] = pending.splice(index, 1);
  p.resolve({ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(body))});
  await tick(); await tick();
}
const run = s => vm.runInContext(s, context);
const out = s => JSON.parse(JSON.stringify(run(s)));
async function show(snap) {
  run(`selectRun(${JSON.stringify(snap.run_id)})`);
  await respond('/api/snapshot?run_id=' + encodeURIComponent(snap.run_id), 200, snap);
}
vm.runInContext(SOURCE, context);
'''

DECISION = {"evidence": ["git apply --check"], "item_decisions": [{"disposition": "accepted", "id": "F1"}], "next": "next_round",
            "note": "缺少 CSV", "protected_confirmed": ["migrations/1.sql"], "recorded_at": 1791362967.5917819,
            "verdict": "accepted_with_corrections"}
APPLY_HINT = "git -C /tmp/fx/source apply /tmp/fx/state/v1/runs/run-gIatU6GfOd3jo3xk/changes.patch"
FINISHED = {
    "claimed_status": "completed", "continue_from": None, "decision": DECISION, "effort": "low", "ended_at": 1791362967.5634089,
    "kind": "implement", "model": "sonnet", "process_stopped": "confirmed", "profile": "copy", "round": 1,
    "run_id": "run-gIatU6GfOd3jo3xk", "started_at": 1791362966.545501, "status": "ok", "summary": "导出已改为异步",
    "task_id": "T-1", "timeout_seconds": 60.0, "title": "把导出改成异步，失败要提示原因", "updated_at": 1791362967.592202,
    "warnings": ["protected_touched", "questions_for_user"], "active": False, "elapsed_seconds": 1.0, "events_count": 7,
    "last_event": {"seq": 7, "received_at": 1791362967.592448, "kind": "decision",
                   "summary": "Codex verdict: accepted_with_corrections; next: next_round", "status": "accepted_with_corrections"},
    "last_meaningful_event": {"seq": 7, "received_at": 1791362967.592448, "kind": "decision",
                              "summary": "Codex verdict: accepted_with_corrections; next: next_round", "status": "accepted_with_corrections"},
    "outcome": {
        "brief": {"path": "brief.md", "sha256": "62f85f7cb88b386c34be89e921e8b173393043a307f4792d999e74d52019e520"},
        "changes": {"apply_hint": APPLY_HINT, "delivery_patch": "delivery.patch", "delivery_file_count": 2, "deletions": 1, "file_count": 2, "insertions": 2,
                    "files": [{"class": "protected", "deletions": 0, "insertions": 1, "path": "migrations/1.sql", "status": "A"},
                              {"class": "in_scope", "deletions": 1, "insertions": 1, "path": "src/app.py", "status": "M"}],
                    "patch": "changes.patch"},
        "claimed_status": "completed", "cost_usd": 0.0123, "duration_seconds": 0.9, "ended_at": 1791362967.5634089,
        "instruction_layer": {"files": 1, "hooks": 1, "path": "instruction-layer.json", "plugins": ["fixture-plugin"]},
        "items": 1, "kind": "implement", "models": ["fixture-model"], "note": None, "process_stopped": "confirmed",
        "profile": "copy", "report": {"bytes": 40, "path": "report.md",
                                      "sha256": "bc6b1a89ddaadb670592d5467e4a9c34350884327a7fcbb9cdf3d3f1602ffdcb"},
        "result": {"path": "result.json", "valid": True}, "round": 1, "run_id": "run-gIatU6GfOd3jo3xk", "run_outcome": "ok",
        "summary": "导出已改为异步", "task_id": "T-1",
        "warnings": [{"code": "protected_touched", "detail": "the patch changes protected paths; claude_decide must confirm each",
                      "paths": ["migrations/1.sql"]},
                     {"code": "questions_for_user", "detail": "Claude asks questions only the user can decide; relay them verbatim",
                      "items": ["要不要保留旧接口？"]}]},
    "decision_history_count": 1,
}
READONLY = {
    "claimed_status": "partial", "continue_from": None, "decision": None, "effort": "low", "ended_at": 1791362967.7108521,
    "kind": "analyze", "model": "sonnet", "process_stopped": "confirmed", "profile": "readonly", "round": 1,
    "run_id": "run-JX_nxIwzbPyAibxQ", "started_at": 1791362967.604083, "status": "ok", "summary": "可行但有风险",
    "task_id": "T-2", "timeout_seconds": 60.0, "title": "这个设计可行吗？", "updated_at": 1791362967.7164109, "warnings": [],
    "active": False, "elapsed_seconds": 0.1, "events_count": 6,
    "last_event": {"seq": 6, "received_at": 1791362967.711064, "kind": "finished", "summary": "run ended: ok", "status": "ok"},
    "last_meaningful_event": {"seq": 6, "received_at": 1791362967.711064, "kind": "finished", "summary": "run ended: ok", "status": "ok"},
    "outcome": {
        "brief": {"path": "brief.md", "sha256": "9040f7afd77f9b5eddfe97c148e4efc3983ece33bcbbae46316305d1b147cf38"},
        "changes": None, "claimed_status": "partial", "cost_usd": 0.0123, "duration_seconds": 0.1, "ended_at": 1791362967.7108521,
        "instruction_layer": {"files": 1, "hooks": 1, "path": "instruction-layer.json", "plugins": ["fixture-plugin"]},
        "items": 0, "kind": "analyze", "models": ["fixture-model"], "note": None, "process_stopped": "confirmed",
        "profile": "readonly", "report": {"bytes": 28, "path": "report.md",
                                          "sha256": "c8dca0a78a7cbe4cae76fb66eacecec6d2f3710de1b39178c59fb0b5dc88ef8d"},
        "result": {"path": "result.json", "valid": True}, "round": 1, "run_id": "run-JX_nxIwzbPyAibxQ", "run_outcome": "ok",
        "summary": "可行但有风险", "task_id": "T-2", "warnings": []},
    "decision_history_count": 0,
}
RUNNING = {
    "continue_from": None, "decision": None, "effort": "low", "ended_at": None, "kind": "implement", "model": "sonnet",
    "profile": "copy", "round": 1, "run_id": "run-POaJz2M5Ml3AAjij", "started_at": 1791362967.827256, "status": "running",
    "task_id": "T-3", "timeout_seconds": 60.0, "title": "把导出改成异步，失败要提示原因", "updated_at": 1791362967.827256,
    "active": True, "elapsed_seconds": 0.8, "events_count": 4,
    "last_event": {"seq": 4, "received_at": 1791362968.587539, "kind": "provider", "summary": "provider lifecycle event", "status": "init"},
    "last_meaningful_event": {"seq": 3, "received_at": 1791362968.568383, "kind": "executing", "summary": "Claude started",
                              "status": "executing"},
    "outcome": None,
    "live": {"session_id": "501b02c4-076b-435e-82aa-a1c25fb716eb", "initialized_model": "fixture-model", "models": [],
             "provider_response_observed": False},
    "decision_history_count": 0,
}
EVENTS = {"events": [
    {"seq": 1, "received_at": 1791362967.860445, "kind": "preflight", "summary": "checking the local Claude CLI", "status": "preflight"},
    {"seq": 2, "received_at": 1791362967.915446, "kind": "preflight", "summary": "preparing the workspace", "status": "preflight"},
    {"seq": 3, "received_at": 1791362968.568383, "kind": "executing", "summary": "Claude started", "status": "executing"},
    {"seq": 4, "received_at": 1791362968.587539, "kind": "provider", "summary": "provider lifecycle event", "status": "init"}],
    "next_cursor": 4, "has_more": False}
SNAPSHOT_ONLY = {"active", "elapsed_seconds", "events_count", "last_event", "last_meaningful_event", "outcome", "live",
                 "decision_history_count"}
RUNS = {"runs": [{k: v for k, v in snap.items() if k not in SNAPSHOT_ONLY} for snap in (RUNNING, READONLY, FINISHED)],
        "next_offset": 3, "has_more": False}
WARNING_CODES = ["original_changed_during_run", "protected_touched", "outside_hint", "tool_denied", "source_not_read",
                 "workflow_incomplete", "report_missing", "result_missing", "result_malformed", "residual_processes_stopped",
                 "process_stop_unconfirmed", "sensitive_inputs_skipped", "resume_fallback", "questions_for_user",
                 "disputes_present", "not_verified_present", "provider_error", "patch_unavailable",
                 "carried_already_in_original", "finalize_error", "undelivered_files_now_ignored"]


class DashboardTests(unittest.TestCase):
    def run_node(self, body: str, hash_value: str = "token=fixture&run=", **values):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the executable UI regression")
        script = re.search(r"<script>(.*?)</script>", PAGE.read_text(encoding="utf-8"), re.S).group(1)
        constants = {"SOURCE": script, "HASH": hash_value, "FINISHED": FINISHED, "READONLY": READONLY, "RUNNING": RUNNING,
                     "EVENTS": EVENTS, "RUNS": RUNS, **values}
        source = "".join(f"const {name}={json.dumps(value, ensure_ascii=False)};\n" for name, value in constants.items())
        source += HARNESS + "(async () => {\n" + body + "\n})().catch(e => { console.error(e && e.stack || e); process.exit(1); });\n"
        completed = subprocess.run([node, "-"], input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_page_is_self_contained_and_never_parses_data_as_html(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertLessEqual(len(page.splitlines()), 900)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "console."):
            self.assertNotIn(banned, page)
        self.assertIsNone(re.search(r"https?://", page), "the CSP allows no external resources")
        self.assertEqual(len(re.findall(r"<script", page)), 1)

    def test_tasks_group_by_task_id_and_order_by_newest_activity(self):
        round_two = dict(RUNS["runs"][2], run_id="run-T1roundTwo00", round=2, started_at=1791362990.0,
                         updated_at=1791362990.0, status="running", decision=None, continue_from="run-gIatU6GfOd3jo3xk")
        decided_later = dict(RUNS["runs"][1], decision=dict(DECISION, verdict="accepted", next="done", recorded_at=1791363000.0))
        self.run_node(r'''
const order = rows => out(`groupTasks(${JSON.stringify(rows)})`).map(g => g.task_id);
assert.deepEqual(order(RUNS.runs), ['T-3', 'T-2', 'T-1']);
const groups = out(`groupTasks(${JSON.stringify([...RUNS.runs, ROUND_TWO])})`);
assert.deepEqual(groups.map(g => g.task_id), ['T-1', 'T-3', 'T-2']);
assert.deepEqual(groups[0].runs.map(r => r.round), [1, 2]);
assert.equal(groups[0].latest.run_id, 'run-T1roundTwo00');
assert.deepEqual(order([RUNS.runs[0], DECIDED_LATER, RUNS.runs[2]]), ['T-2', 'T-3', 'T-1']);

await respond('/api/runs?limit=100&offset=0', 200, {...RUNS, runs: [ROUND_TWO, ...RUNS.runs]});
const cards = byId('taskList').children;
assert.equal(cards.length, 3);
assert.match(cards[0].textContent, /T-1 · 实现 \/ 独立副本 · 第 2 轮（已载入 2 轮）/);
assert.match(cards[0].textContent, /运行中/);
assert.equal(run('S.sel'), 'run-T1roundTwo00', 'the newest task is opened when the link names no run');
await respond('run_id=run-T1roundTwo00', 200, {...ROUND_TWO, active: true, outcome: null, last_event: null});
const tabs = byId('roundTabs').children;
assert.deepEqual(tabs.map(t => t.textContent), ['第 1 轮 · 修正后采纳', '第 2 轮 · 运行中']);
assert.deepEqual(tabs.map(t => t.getAttribute('aria-selected')), ['false', 'true']);
assert.match(byId('runFacts').textContent, /接续自 run-gIatU6GfOd3jo3xk/);
''', ROUND_TWO=round_two, DECIDED_LATER=decided_later)

    def test_status_and_verdict_texts_keep_report_verdict_and_next_step_apart(self):
        self.run_node(r'''
assert.equal(run('verdictText(null)'), '待核验');
assert.equal(run("verdictText({verdict:'accepted_with_corrections',next:'next_round'})"), '修正后采纳 · 需要下一轮');
assert.equal(run("verdictText({verdict:'accepted',next:'done'})"), '采纳 · 任务完成');
assert.equal(run("verdictText({verdict:'rejected',next:'codex_finishes'})"), '不采纳 · 由 Codex 收尾');
assert.equal(run("statusText({status:'lost'})"), '失联');
assert.equal(run("statusText({status:'shiny_new'})"), 'shiny_new');

await respond('/api/runs', 200, RUNS);
const cards = Object.fromEntries(byId('taskList').children.map(c => [c.textContent.match(/T-\d/)[0], c.textContent]));
assert.match(cards['T-1'], /正常结束/);
assert.match(cards['T-1'], /修正后采纳 · 需要下一轮/);
assert.match(cards['T-2'], /正常结束.*待核验/);
assert.match(cards['T-3'], /运行中.*待核验/);
for (const text of Object.values(cards)) assert.doesNotMatch(text, /通过/);

await respond('run_id=run-POaJz2M5Ml3AAjij', 200, RUNNING);
assert.doesNotMatch(pageText(), /通过/);
await show(READONLY);
assert.match(byId('boxDecision').textContent, /尚未核验/);
assert.doesNotMatch(byId('boxDecision').textContent, /采纳|通过/);
assert.match(byId('boxClaim').textContent, /部分完成.*可行但有风险.*不是核验结论/);
assert.doesNotMatch(pageText(), /通过/);
await show(FINISHED);
const verdict = byId('boxDecision').textContent;
assert.match(verdict, /报告：修正后采纳/);
assert.match(verdict, /下一步需要下一轮/);
assert.match(verdict, /说明缺少 CSV/);
assert.match(verdict, /git apply --check/);
assert.match(verdict, /F1：采纳/);
assert.match(verdict, /已确认受保护路径migrations\/1\.sql/);
assert.equal(byId('boxDecision').className, 'box t-warn');
assert.equal(byId('boxRun').className.trim(), 'box', 'a clean run outcome is not coloured like an accepted verdict');
''')

    def test_warning_labels_cover_every_code_and_fall_back_to_the_raw_code(self):
        unknown = dict(READONLY, run_id="run-unknownWarn1", outcome=dict(READONLY["outcome"], warnings=[
            {"code": "brand_new_code", "detail": "something new", "items": ["<b>保留</b>\n  原样"]},
            {"code": "sensitive_inputs_skipped", "paths": ["/x/.env (sensitive name)"],
             "detail": "these paths were not copied for Claude (sensitive name, symlink or size limit)"}]))
        self.run_node(r'''
for (const code of CODES) {
  const text = run(`warnLabel(${JSON.stringify(code)})`);
  assert.notEqual(text, code, code + ' needs a Chinese label');
  assert.match(text, /[一-鿿]/);
}
assert.equal(run("warnLabel('brand_new_code')"), 'brand_new_code');
await show(UNKNOWN);
const box = byId('warnList');
assert.equal(box.children.length, 2);
assert.match(box.children[0].textContent, /^brand_new_code brand_new_codesomething new/);
const items = all(box, n => n.className === 'verbatim').map(n => n.textContent);
assert.deepEqual(items, ['<b>保留</b>\n  原样']);
assert.match(box.children[1].textContent, /部分原件未复制给 Claude.*\/x\/\.env \(sensitive name\)/);
await show(FINISHED);
assert.match(byId('warnList').textContent, /改动了受保护路径 protected_touched.*migrations\/1\.sql/);
assert.match(byId('warnList').textContent, /Claude 有需要用户决定的问题.*要不要保留旧接口？/);
''', CODES=WARNING_CODES, UNKNOWN=unknown)

    def test_the_three_backend_samples_render(self):
        self.run_node(r'''
await show(RUNNING);
assert.equal(byId('runTitle').textContent, '把导出改成异步，失败要提示原因');
assert.match(byId('runFacts').textContent, /任务 T-3.*运行 run-POaJz2M5Ml3AAjij.*实现 \/ 独立副本 · 第 1 轮/);
assert.match(byId('runFacts').textContent, /请求 sonnet · effort low · 实际 fixture-model/);
assert.match(byId('runFacts').textContent, /用时 0\.8 秒 \/ 上限 1 分 0 秒/);
assert.match(byId('boxRun').textContent, /运行中.*最近活动Claude started/);
assert.match(byId('boxClaim').textContent, /运行中，尚未回报/);
assert.match(byId('boxDecision').textContent, /尚未核验.*运行结束后由 Codex 核验/);
assert.match(byId('changeBody').textContent, /运行结束后生成改动清单/);
assert.match(byId('warnList').textContent, /运行结束后汇总提醒/);
assert.equal(run('S.artName'), 'events');
await respond('/api/events?run_id=run-POaJz2M5Ml3AAjij&after=0', 200, EVENTS);
assert.match(byId('artMeta').textContent, /共 4 条公开活动/);
assert.match(byId('artBody').textContent, /checking the local Claude CLI.*Claude started/);

await show(FINISHED);
assert.match(byId('runFacts').textContent, /实际 fixture-model/);
assert.match(byId('boxRun').textContent, /正常结束.*进程已确认停止.*耗时0\.9 秒.*费用\$0\.0123（CLI 估算）/);
assert.match(byId('boxClaim').textContent, /已完成.*导出已改为异步/);
const changes = byId('changeBody');
assert.match(changes.textContent, /2 个文件 · \+2 −1/);
const rows = all(changes, n => n.tagName === 'TR').slice(1).map(r => r.textContent);
assert.deepEqual(rows, ['migrations/1.sql新增+1 −0受保护', 'src/app.py修改+1 −1范围内']);
assert.equal(all(changes, n => n.className === 'badge t-bad')[0].textContent, '受保护');
const copy = all(changes, n => n.tagName === 'BUTTON' && n.textContent === '复制命令')[0];
copy.click();
await tick();
assert.deepEqual(clip, [FINISHED.outcome.changes.apply_hint]);
assert.match(changes.textContent, /已复制/);
assert.ok(all(byId('artTabs'), n => n.textContent === '累计 Patch').length === 0);

await show(READONLY);
assert.match(byId('runFacts').textContent, /分析 \/ 只读/);
assert.match(byId('changeBody').textContent, /只读运行，没有改动/);
assert.match(byId('warnList').textContent, /没有提醒/);
const tabNames = byId('artTabs').children.map(t => t.textContent);
assert.deepEqual(tabNames, ['报告', 'Claude 收到的任务书', '结果头', 'Patch', '用户原话', '原件', '指令层', '最终回复', '活动']);
''')

    def test_stale_snapshot_events_and_artifacts_are_discarded_after_switching_runs(self):
        self.run_node(r'''
const A = {...FINISHED, run_id: 'run-AAAAAAAAAA', title: 'A 的标题'};
const B = {...READONLY, run_id: 'run-BBBBBBBBBB', title: 'B 的标题'};
run("selectRun('run-AAAAAAAAAA')");
run("selectRun('run-BBBBBBBBBB')");
await respond('/api/snapshot?run_id=run-BBBBBBBBBB', 200, B);
await respond('/api/snapshot?run_id=run-AAAAAAAAAA', 200, A);
assert.equal(byId('runTitle').textContent, 'B 的标题');
assert.equal(run('S.snap.run_id'), 'run-BBBBBBBBBB');
assert.doesNotMatch(byId('boxDecision').textContent, /修正后采纳/);

run("selectRun('run-AAAAAAAAAA')");
await respond('run_id=run-BBBBBBBBBB&name=report', 200, {name: 'report', available: true, size_bytes: 6, sha256: 'b'.repeat(64),
  offset: 0, next_offset: 6, end_of_artifact: true, content: 'B 报告'});
await respond('/api/events?run_id=run-BBBBBBBBBB', 200, {events: [{seq: 1, received_at: 1, kind: 'preflight', summary: 'B 的活动'}],
  next_cursor: 1, has_more: false});
assert.equal(run('S.art'), null);
assert.equal(run('S.events.length'), 0);
assert.doesNotMatch(pageText(), /B 报告|B 的活动/);
await respond('/api/snapshot?run_id=run-AAAAAAAAAA', 200, A);
await respond('run_id=run-AAAAAAAAAA&name=report', 200, {name: 'report', available: true, size_bytes: 6, sha256: 'a'.repeat(64),
  offset: 0, next_offset: 6, end_of_artifact: true, content: 'A 报告'});
assert.equal(byId('artBody').textContent, 'A 报告');
assert.equal(byId('runTitle').textContent, 'A 的标题');
''')

    def test_artifacts_load_in_pages_and_join_in_order(self):
        sha = READONLY["outcome"]["report"]["sha256"]
        packet = {"task_id": "T-2", "user_messages": [
            {"text": '把导出改成异步，<b>失败</b>要提示原因\n  第二行保留缩进 & "引号"', "source": "human", "at": "2026-10-07 10:00"},
            {"text": "补充：CSV 也要", "source": "relayed"}], "no_user_words_reason": None}
        self.run_node(r'''
await show(READONLY);
assert.equal(run('S.artName'), 'report');
await respond('run_id=run-JX_nxIwzbPyAibxQ&name=report&offset=0', 200, {name: 'report', available: true, size_bytes: 13,
  sha256: SHA, offset: 0, next_offset: 6, end_of_artifact: false, content: '第一页|'});
const more = byId('artMore');
assert.equal(more.hidden, false);
assert.match(more.textContent, /加载更多/);
assert.match(byId('artMeta').textContent, new RegExp('大小 13 B · 已载入 6 B.*' + SHA + '.*与运行记录一致'));
more.click();
assert.match(pending.at(-1).url, /name=report&offset=6&limit=200000$/);
assert.equal(more.disabled, true);
await respond('name=report&offset=6', 200, {name: 'report', available: true, size_bytes: 13, sha256: SHA, offset: 6,
  next_offset: 13, end_of_artifact: true, content: '第二页'});
assert.equal(byId('artBody').textContent, '第一页|第二页');
assert.equal(more.hidden, true);

const text = JSON.stringify(PACKET);
const cut = text.indexOf('第二行');
const userTab = byId('artTabs').children.find(t => t.textContent === '用户原话');
userTab.click();
await respond('name=packet&offset=0', 200, {name: 'packet', available: true, size_bytes: 400, sha256: 'c'.repeat(64), offset: 0,
  next_offset: 200, end_of_artifact: false, content: text.slice(0, cut)});
more.click();
await respond('name=packet&offset=200', 200, {name: 'packet', available: true, size_bytes: 400, sha256: 'c'.repeat(64), offset: 200,
  next_offset: 400, end_of_artifact: true, content: text.slice(cut)});
const words = all(byId('artBody'), n => n.className === 'verbatim').map(n => n.textContent);
assert.deepEqual(words, PACKET.user_messages.map(m => m.text));
assert.match(byId('artBody').textContent, /第 1 条 · 用户本人 · 2026-10-07 10:00.*第 2 条 · 他人转述/s);

byId('artTabs').children.find(t => t.textContent === '用户原话').click();
await respond('name=packet&offset=0', 200, {name: 'packet', available: true, size_bytes: 80, sha256: 'd'.repeat(64), offset: 0,
  next_offset: 80, end_of_artifact: true, content: {user_messages: [], no_user_words_reason: '用户只发了截图\n没有文字'}});
assert.deepEqual(all(byId('artBody'), n => n.className === 'verbatim').map(n => n.textContent), ['用户只发了截图\n没有文字']);

byId('artTabs').children.find(t => t.textContent === 'Claude 收到的任务书').click();
await respond('name=brief', 200, {name: 'brief', available: true, size_bytes: 9, sha256: 'e'.repeat(64), offset: 0,
  next_offset: 9, end_of_artifact: true, content: '# 任务书'});
assert.match(byId('artMeta').textContent, /Claude 实际收到的任务书.*与运行记录的 sha256 不一致/);
byId('artTabs').children.find(t => t.textContent === '最终回复').click();
await respond('name=final_message', 200, {name: 'final_message', available: false, content: null});
assert.match(byId('artBody').textContent, /本轮没有这份材料/);
''', SHA=sha, PACKET=packet)

    def test_polling_uses_the_token_slows_down_when_hidden_and_reads_events_incrementally(self):
        self.run_node(r'''
await respond('/api/runs?limit=100&offset=0', 200, RUNS);
assert.equal(calls[0].opts.headers.Authorization, 'Bearer fixture');
assert.equal(timers.at(-1).ms, 5000);
const listTick = timers.at(-1).f;
assert.equal(run('S.sel'), 'run-POaJz2M5Ml3AAjij');
assert.match(loc.hash, /^#token=fixture&run=run-POaJz2M5Ml3AAjij$/);
await respond('/api/snapshot?run_id=run-POaJz2M5Ml3AAjij', 200, RUNNING);
await respond('/api/events?run_id=run-POaJz2M5Ml3AAjij&after=0', 200, EVENTS);
assert.equal(timers.at(-1).ms, 2000);
const runTick = timers.at(-1).f;

doc.hidden = true;
listTick();
await respond('/api/runs', 200, RUNS);
assert.equal(timers.at(-1).ms, 30000);
runTick();
const later = {...RUNNING, elapsed_seconds: 5.2, last_event: {seq: 6, received_at: 1791362970, kind: 'tool', summary: 'Read src/app.py'}};
await respond('/api/snapshot', 200, later);
assert.match(pending.at(-1).url, /after=4&limit=200$/);
await respond('/api/events', 200, {events: [{seq: 5, received_at: 1791362969, kind: 'tool', summary: 'Read brief.md', tool: 'Read'},
  {seq: 6, received_at: 1791362970, kind: 'tool', summary: 'Read src/app.py', tool: 'Read'}], next_cursor: 6, has_more: false});
assert.equal(run('S.events.length'), 6);
assert.equal(run('S.cursor'), 6);
assert.match(byId('artBody').textContent, /Read brief\.md.*Read src\/app\.py/);
assert.equal(timers.at(-1).ms, 15000);
''')

    def test_rejected_link_shows_the_reconnect_banner_and_stops_polling(self):
        self.run_node(r'''
await respond('/api/runs', 401, {error: 'Reopen the details link supplied by Claude tools.'});
assert.equal(byId('banner').hidden, false);
assert.equal(byId('banner').textContent, '链接失效，请从 Codex 重新获取');
assert.equal(byId('conn').textContent, '链接失效');
assert.equal(timers.length, 0);
run("selectRun('run-CCCCCCCCCC')");
await tick();
assert.equal(pending.length, 0, 'no further requests after the token was rejected');
''')
        self.run_node(r'''
assert.equal(calls.length, 0);
assert.equal(byId('banner').textContent, '链接失效，请从 Codex 重新获取');
''', hash_value="run=run-CCCCCCCCCC")
        self.run_node(r'''
const p = pending.shift();
p.reject(new TypeError('Failed to fetch'));
await tick(); await tick();
assert.match(byId('banner').textContent, /^连接中断，正在重试/);
assert.equal(timers.at(-1).ms, 5000, 'a network failure keeps polling');
timers.at(-1).f();
await respond('/api/runs', 200, RUNS);
assert.equal(byId('banner').hidden, true);
''')


if __name__ == "__main__":
    unittest.main(verbosity=2)
