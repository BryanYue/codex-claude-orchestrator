"""Run the workbench script in Node against a minimal fake DOM and the 1.0 viewer payloads."""
import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "assets/dashboard.html"
PRODUCERS = (ROOT / "skills/codex-claude-orchestrator/scripts", ROOT / "scripts")

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
KNOWN_WARNING_CODES = {"original_changed_during_run", "protected_touched", "outside_hint", "tool_denied", "source_not_read",
                       "workflow_incomplete", "report_missing", "result_missing", "result_malformed", "residual_processes_stopped",
                       "process_stop_unconfirmed", "sensitive_inputs_skipped", "resume_fallback", "questions_for_user",
                       "disputes_present", "not_verified_present", "provider_error", "patch_unavailable",
                       "carried_already_in_original", "finalize_error", "undelivered_files_now_ignored",
                       "background_commands_stopped", "turn_interrupted", "questions_unanswered", "interactions_unavailable"}
EVENT_KINDS = {"question": "Claude 提问", "permission": "请求授权", "note": "Claude 说明", "dispute": "Claude 异议",
               "answer": "Codex 回答", "update": "Codex 补充", "interrupt": "请求打断", "hold": "暂停执行等回答",
               "released": "恢复执行", "resumed": "重新接上会话", "turn": "一轮回复结束"}


def produced_warning_codes():
    """Every code the plugin writes: {"code": "<literal>"} dicts and `for ..., code, ... in ((...), ...)` tables."""
    codes = set()
    for path in sorted(path for folder in PRODUCERS for path in folder.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                codes.update(value.value for key, value in zip(node.keys, node.values)
                             if isinstance(key, ast.Constant) and key.value == "code"
                             and isinstance(value, ast.Constant) and isinstance(value.value, str))
            elif isinstance(node, ast.For) and isinstance(node.target, ast.Tuple) and isinstance(node.iter, (ast.Tuple, ast.List)):
                names = [getattr(target, "id", None) for target in node.target.elts]
                if "code" in names:
                    index = names.index("code")
                    codes.update(row.elts[index].value for row in node.iter.elts
                                 if isinstance(row, ast.Tuple) and isinstance(row.elts[index], ast.Constant))
    return codes


def interaction(interaction_id, kind, status, request, blocking=True, answer=None, answered_by=None):
    return {"interaction_id": interaction_id, "kind": kind, "blocking": blocking, "status": status, "request": request,
            "answer": answer, "answered_by": answered_by, "created_at": 1791363001.0,
            "closed_at": None if status == "pending" else 1791363002.0, "hold": None}


def list_row(snap):
    return {k: v for k, v in snap.items() if k not in SNAPSHOT_ONLY | {"interactions", "control_state"}}


def at(row, run_id, round_no, started, **extra):
    return dict(row, run_id=run_id, round=round_no, started_at=started, updated_at=started, ended_at=None, **extra)


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
assert.match(cards[0].textContent, /动手改 · 第 2 轮 · /);
assert.equal(all(cards[0], n => n.tagName === 'SPAN' && /badge/.test(n.className)).length, 1, 'one state badge per card');
assert.match(cards[0].textContent, /进行中/);
assert.equal(byId('listCount').textContent, '3');
assert.equal(byId('listMeta').textContent, '共 3 个');
assert.equal(run('S.sel'), 'run-T1roundTwo00', 'the newest task is opened when the link names no run');
await respond('run_id=run-T1roundTwo00', 200, {...ROUND_TWO, active: true, outcome: null, last_event: null});
const tabs = byId('roundTabs').children;
assert.deepEqual(tabs.map(t => t.textContent), ['第 1 轮 · 改后通过', '第 2 轮 · 进行中']);
assert.deepEqual(tabs.map(t => t.getAttribute('aria-selected')), ['false', 'true']);
const back = all(byId('runFacts'), n => n.tagName === 'BUTTON')[0];
assert.equal(back.textContent, '查看上一轮');
back.click();
assert.equal(run('S.sel'), 'run-gIatU6GfOd3jo3xk');
''', ROUND_TWO=round_two, DECIDED_LATER=decided_later)

    def test_task_list_drawer_opens_closes_and_closes_on_pick(self):
        self.run_node(r'''
await respond('/api/runs?limit=100&offset=0', 200, RUNS);
byId('listToggle').click();
assert.equal(byId('layout').className, 'layout open');
assert.equal(byId('listToggle').getAttribute('aria-expanded'), 'true');
byId('scrim').click();
assert.equal(byId('layout').className, 'layout');
byId('listToggle').click();
docListeners.keydown[0]({key: 'Escape'});
assert.equal(byId('layout').className, 'layout');
byId('emptyOpen').click();
byId('taskList').children[1].click();
assert.equal(byId('layout').className, 'layout', 'picking a task closes the drawer');
assert.equal(run('S.sel'), 'run-JX_nxIwzbPyAibxQ');
''')

    def test_status_and_verdict_texts_keep_report_verdict_and_next_step_apart(self):
        self.run_node(r'''
assert.equal(run('verdictText(null)'), '待验收');
assert.equal(run("verdictText({verdict:'accepted_with_corrections',next:'next_round'})"), '改后通过 · 再做一轮');
assert.equal(run("verdictText({verdict:'accepted',next:'done'})"), '通过 · 已完成');
assert.equal(run("verdictText({verdict:'rejected',next:'codex_finishes'})"), '退回 · Codex 收尾');
assert.equal(run("statusText({status:'lost'})"), '失联');
assert.equal(run("statusText({status:'shiny_new'})"), 'shiny_new');
assert.equal(run("stateBadge({status:'timeout'}).textContent"), '超时 · 待验收');
assert.equal(run("stateBadge({status:'ok'}).textContent"), '待验收');

await respond('/api/runs', 200, RUNS);
const [t3, t2, t1] = byId('taskList').children.map(c => c.textContent);
assert.match(t1, /改后通过 · 再做一轮/);
assert.doesNotMatch(t1, /正常结束/, 'a verdict replaces the run status on the card');
assert.match(t2, /待验收/);
assert.match(t3, /进行中/);
for (const text of [t2, t3]) assert.doesNotMatch(text, /通过/);

const detailText = () => ['roundTabs', 'hero', 'askBox', 'boxRun', 'boxClaim', 'boxDecision', 'warnList', 'changeBody']
  .map(id => byId(id).textContent).join('\n');
await respond('run_id=run-POaJz2M5Ml3AAjij', 200, RUNNING);
assert.doesNotMatch(detailText(), /通过/);
await show(READONLY);
assert.match(byId('hero').textContent, /Claude 已交回，等 Codex 验收Claude 自评：没做完/);
assert.match(byId('boxDecision').textContent, /待验收/);
assert.doesNotMatch(byId('boxDecision').textContent, /采纳|通过/);
assert.match(byId('boxClaim').textContent, /没做完.*可行但有风险.*不算验收/);
assert.doesNotMatch(detailText(), /通过/);
await show(FINISHED);
assert.match(byId('hero').textContent, /Codex 验收：改后通过下一步：再做一轮/);
const verdict = byId('boxDecision').textContent;
assert.match(verdict, /改后通过/);
assert.match(verdict, /下一步再做一轮/);
assert.match(verdict, /备注缺少 CSV/);
assert.match(verdict, /git apply --check/);
assert.match(verdict, /F1：采纳/);
assert.match(verdict, /放行文件migrations\/1\.sql/);
assert.equal(byId('boxDecision').className, 'box t-warn lead');
assert.equal(byId('boxRun').className.trim(), 'box', 'a clean run outcome is not coloured like an accepted verdict');
''')

    def test_warning_labels_cover_every_code_and_fall_back_to_the_raw_code(self):
        codes = produced_warning_codes()
        self.assertLessEqual(KNOWN_WARNING_CODES, codes, "the source scan lost codes it used to find")
        unknown = dict(READONLY, run_id="run-unknownWarn1", outcome=dict(READONLY["outcome"], warnings=[
            {"code": "brand_new_code", "detail": "something new", "items": ["<b>保留</b>\n  原样"]},
            {"code": "sensitive_inputs_skipped", "paths": ["/x/.env (sensitive name)"],
             "detail": "these paths were not copied for Claude (sensitive name, symlink or size limit)"}]))
        self.run_node(r'''
for (const code of CODES) {
  const [title, desc] = out(`warnInfo(${JSON.stringify(code)})`);
  assert.notEqual(title, code, code + ' needs a Chinese title');
  assert.notEqual(title, '其他提醒', code + ' is produced by the plugin, so it is not an unknown code');
  assert.match(title, /[一-鿿]/);
  assert.match(desc, /[一-鿿]/, code + ' needs a Chinese description');
}
assert.deepEqual(out("warnInfo('brand_new_code')"), ['其他提醒', 'brand_new_code']);
await show(UNKNOWN);
const box = byId('warnList');
assert.equal(box.children.length, 2);
assert.match(box.children[0].textContent, /^其他提醒brand_new_code.*技术细节brand_new_codesomething new$/s);
const items = all(box, n => n.className === 'verbatim').map(n => n.textContent);
assert.deepEqual(items, ['<b>保留</b>\n  原样']);
assert.match(box.children[1].textContent, /部分附件没给 Claude.*\/x\/\.env \(sensitive name\)/);
await show(FINISHED);
assert.match(byId('askBox').textContent, /Claude 有问题问你.*要不要保留旧接口？/);
assert.doesNotMatch(byId('warnList').textContent, /有问题问你/, 'questions for the user sit above, not in the list');
assert.match(byId('warnList').textContent, /动了受保护的文件需要 Codex 逐个放行.*migrations\/1\.sql/);
const tech = all(byId('warnList'), n => n.tagName === 'DETAILS');
assert.equal(tech.length, 1);
assert.match(tech[0].textContent, /^技术细节protected_touched.*claude_decide must confirm each/);
''', CODES=sorted(codes), UNKNOWN=unknown)

    def test_activity_texts_are_translated_and_tool_steps_named(self):
        self.run_node(r'''
assert.equal(run("evText({kind:'tool', tool:'Edit', file:'src/a.py'})"), '修改 src/a.py');
assert.equal(run("evText({kind:'tool', tool:'Bash', text:'Run tests'})"), '运行命令：Run tests');
assert.equal(run("evText({kind:'tool', tool:'Brand', summary:'Brand'})"), '使用 Brand');
assert.equal(run("evText({summary:'run ended: timeout'})"), '运行结束：超时');
assert.equal(run("evText({summary:'Codex verdict: rejected; next: codex_finishes'})"), 'Codex 验收：退回 · Codex 收尾');
assert.equal(run("evText({summary:'something Claude said'})"), 'something Claude said');
run(`S.events = ${JSON.stringify([1, 2, 3].map(seq => ({seq, received_at: seq, kind: 'provider', summary: 'provider lifecycle event'})))}`);
assert.match(run('eventsView().textContent'), /模型服务状态 · 共 3 条/);
''')

    def test_the_three_backend_samples_render(self):
        self.run_node(r'''
await show(RUNNING);
assert.equal(byId('runTitle').textContent, '把导出改成异步，失败要提示原因');
assert.match(byId('runFacts').textContent, /^动手改副本模式模型 fixture-model（指定 sonnet）思考强度 low用时 0\.8 秒（上限 1 分钟）开始于 /);
assert.match(byId('hero').textContent, /Claude 正在做Claude 开始工作 · 已用 0\.8 秒/);
assert.match(byId('boxRun').textContent, /进行中.*正在做Claude 开始工作/);
assert.match(byId('boxClaim').textContent, /还在做，没交回/);
assert.match(byId('boxDecision').textContent, /待验收.*做完后 Codex 会来验收/);
assert.match(byId('changeBody').textContent, /结束后列出改动/);
assert.match(byId('warnList').textContent, /结束后汇总/);
assert.match(byId('idBody').textContent, /任务号T-3.*运行号run-POaJz2M5Ml3AAjij/);
assert.equal(run('S.artName'), 'events');
await respond('/api/events?run_id=run-POaJz2M5Ml3AAjij&after=0', 200, EVENTS);
assert.match(byId('artMeta').textContent, /共 4 条/);
assert.match(byId('artBody').textContent, /检查本机 Claude.*准备工作目录.*Claude 开始工作.*模型服务状态/);

await show(FINISHED);
assert.match(byId('runFacts').textContent, /模型 fixture-model/);
assert.match(byId('boxRun').textContent, /正常结束.*进程已确认停止.*用时0\.9 秒.*费用约 \$0\.0123（Claude 估算）/);
assert.match(byId('boxClaim').textContent, /做完了.*导出已改为异步/);
const changes = byId('changeBody');
assert.match(changes.textContent, /2 个文件 · \+2 −1/);
const rows = all(changes, n => n.className === 'file').map(r => r.textContent);
assert.deepEqual(rows, ['migrations/1.sql新增 · +1 −0受保护', 'src/app.py修改 · +1 −1预期内']);
assert.equal(all(changes, n => n.className === 'badge t-bad')[0].textContent, '受保护');
const apply = all(changes, n => n.tagName === 'DETAILS')[0];
assert.match(apply.textContent, /^手动合入想自己合入？复制这条命令，含之前没合入的改动，共 2 个文件/);
const copy = all(apply, n => n.tagName === 'BUTTON' && n.textContent === '复制命令')[0];
copy.click();
await tick();
assert.deepEqual(clip, [FINISHED.outcome.changes.apply_hint]);
assert.match(changes.textContent, /已复制/);
const tabNames = () => byId('artTabs').children.filter(t => t.tagName === 'BUTTON').map(t => t.textContent);
assert.deepEqual(tabNames(), ['报告', '本轮改动', '过程记录', '更多']);
byId('artTabs').children.find(t => t.textContent === '更多').click();
assert.ok(tabNames().includes('累计改动'));
byId('artTabs').children.find(t => t.textContent === '收起').click();
assert.deepEqual(tabNames(), ['报告', '本轮改动', '过程记录', '更多']);

await show(READONLY);
assert.match(byId('runFacts').textContent, /^只分析只读模式/);
assert.match(byId('changeBody').textContent, /只读模式，没有改动/);
assert.match(byId('warnList').textContent, /没有提醒/);
assert.deepEqual(tabNames(), ['报告', '过程记录', '更多'], 'no patch tab for a run without changes');
byId('artTabs').children.find(t => t.textContent === '更多').click();
assert.deepEqual(tabNames(), ['报告', '过程记录', '收起', '任务书', '你的原话', '附件', 'Claude 配置', '结果数据', '最后回复']);
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
assert.doesNotMatch(byId('boxDecision').textContent, /改后通过/);

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
assert.equal(byId('artMeta').textContent, '已加载 6 B / 共 13 B校验一致校验码' + SHA);
more.click();
assert.match(pending.at(-1).url, /name=report&offset=6&limit=200000$/);
assert.equal(more.disabled, true);
await respond('name=report&offset=6', 200, {name: 'report', available: true, size_bytes: 13, sha256: SHA, offset: 6,
  next_offset: 13, end_of_artifact: true, content: '第二页'});
assert.equal(byId('artBody').textContent, '第一页|第二页');
assert.equal(more.hidden, true);

const text = JSON.stringify(PACKET);
const cut = text.indexOf('第二行');
const tab = name => byId('artTabs').children.find(t => t.textContent === name);
tab('更多').click();
tab('你的原话').click();
await respond('name=packet&offset=0', 200, {name: 'packet', available: true, size_bytes: 400, sha256: 'c'.repeat(64), offset: 0,
  next_offset: 200, end_of_artifact: false, content: text.slice(0, cut)});
more.click();
await respond('name=packet&offset=200', 200, {name: 'packet', available: true, size_bytes: 400, sha256: 'c'.repeat(64), offset: 200,
  next_offset: 400, end_of_artifact: true, content: text.slice(cut)});
const words = all(byId('artBody'), n => n.className === 'verbatim').map(n => n.textContent);
assert.deepEqual(words, PACKET.user_messages.map(m => m.text));
assert.match(byId('artBody').textContent, /第 1 条 · 你说的 · 2026-10-07 10:00.*第 2 条 · 别人转述/s);

tab('你的原话').click();
await respond('name=packet&offset=0', 200, {name: 'packet', available: true, size_bytes: 80, sha256: 'd'.repeat(64), offset: 0,
  next_offset: 80, end_of_artifact: true, content: {user_messages: [], no_user_words_reason: '用户只发了截图\n没有文字'}});
assert.deepEqual(all(byId('artBody'), n => n.className === 'verbatim').map(n => n.textContent), ['用户只发了截图\n没有文字']);

tab('任务书').click();
await respond('name=brief', 200, {name: 'brief', available: true, size_bytes: 9, sha256: 'e'.repeat(64), offset: 0,
  next_offset: 9, end_of_artifact: true, content: '# 任务书'});
assert.match(byId('artMeta').textContent, /这是 Claude 实际收到的原文.*校验不一致，内容和记录对不上/);
tab('最后回复').click();
await respond('name=final_message', 200, {name: 'final_message', available: false, content: null});
assert.match(byId('artBody').textContent, /这一轮没有这份资料/);
tab('收起').click();
assert.equal(run('S.artName'), 'report', 'folding the extra tabs returns to the report');
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
const step = {seq: 6, received_at: 1791362970, kind: 'tool', summary: 'Read', tool: 'Read', file: 'src/app.py'};
await respond('/api/snapshot', 200, {...RUNNING, elapsed_seconds: 5.2, last_event: step, last_meaningful_event: step});
assert.match(byId('hero').textContent, /读取 src\/app\.py · 已用 5\.2 秒/);
assert.match(pending.at(-1).url, /after=4&limit=200$/);
await respond('/api/events', 200, {events: [{seq: 5, received_at: 1791362969, kind: 'tool', summary: 'Read', tool: 'Read', file: 'brief.md'},
  step], next_cursor: 6, has_more: false});
assert.equal(run('S.events.length'), 6);
assert.equal(run('S.cursor'), 6);
assert.match(byId('artBody').textContent, /读取 brief\.md.*读取 src\/app\.py/);
assert.equal(timers.at(-1).ms, 15000);
''')

    def test_rejected_link_shows_the_reconnect_banner_and_stops_polling(self):
        self.run_node(r'''
await respond('/api/runs', 401, {error: 'Reopen the details link supplied by Claude tools.'});
assert.equal(byId('banner').hidden, false);
assert.equal(byId('banner').textContent, '链接已过期，请回 Codex 重新打开');
assert.equal(byId('conn').textContent, '链接过期');
assert.equal(timers.length, 0);
run("selectRun('run-CCCCCCCCCC')");
await tick();
assert.equal(pending.length, 0, 'no further requests after the token was rejected');
''')
        self.run_node(r'''
assert.equal(calls.length, 0);
assert.equal(byId('banner').textContent, '链接已过期，请回 Codex 重新打开');
''', hash_value="run=run-CCCCCCCCCC")
        self.run_node(r'''
const p = pending.shift();
p.reject(new TypeError('Failed to fetch'));
await tick(); await tick();
assert.match(byId('banner').textContent, /^暂时连不上，正在重试/);
assert.equal(timers.at(-1).ms, 5000, 'a network failure keeps polling');
timers.at(-1).f();
await respond('/api/runs', 200, RUNS);
assert.equal(byId('banner').hidden, true);
''')

    def test_same_task_id_in_two_chats_is_two_tasks_with_their_own_rounds(self):
        base = dict(RUNS["runs"][1], task_id="T-9")
        c1 = {"namespace": "n1", "chat": "c1"}
        rows = [at(base, "run-chat1round1", 1, 1791363100.0, context=c1),
                at(base, "run-chat1round2", 2, 1791363300.0, context=c1),
                at(base, "run-chat2round1", 1, 1791363200.0, context={"namespace": "n1", "chat": "c2"})]
        self.run_node(r'''
const groups = out(`groupTasks(${JSON.stringify(ROWS)})`);
assert.deepEqual(groups.map(g => [g.key, g.task_id, g.runs.map(r => r.run_id)]),
  [['n1/c1/T-9', 'T-9', ['run-chat1round1', 'run-chat1round2']], ['n1/c2/T-9', 'T-9', ['run-chat2round1']]]);
const legacy = out(`groupTasks(${JSON.stringify([{...ROWS[0], context: null}, {...ROWS[2], context: undefined}])})`);
assert.deepEqual(legacy.map(g => g.key), ['T-9'], 'rows without a chat still group by task_id');

await respond('/api/runs?limit=100&offset=0', 200, {runs: ROWS, next_offset: 3, has_more: false});
const cards = byId('taskList').children;
assert.equal(cards.length, 2);
assert.deepEqual(cards.map(c => c.children[0].textContent), ['这个设计可行吗？', '这个设计可行吗？']);
assert.equal(run('S.sel'), 'run-chat1round2');
await respond('run_id=run-chat1round2', 200, {...READONLY, ...ROWS[1]});
assert.deepEqual(byId('roundTabs').children.map(t => t.textContent), ['第 1 轮 · 待验收', '第 2 轮 · 待验收']);
assert.deepEqual(cards.map(c => c.getAttribute('aria-current')), ['true', 'false']);
byId('taskList').children[1].click();
await respond('run_id=run-chat2round1', 200, {...READONLY, ...ROWS[2]});
const tabs = byId('roundTabs').children;
assert.deepEqual(tabs.map(t => t.textContent), ['第 1 轮 · 待验收'], 'the other chat\'s rounds are not mixed in');
assert.equal(tabs[0].getAttribute('aria-selected'), 'true');
assert.deepEqual(byId('taskList').children.map(c => c.getAttribute('aria-current')), ['false', 'true']);
''', ROWS=rows)

    def test_title_and_round_title_show_in_card_header_and_tabs_and_old_rows_keep_their_title(self):
        long_round = "补上 CSV 导出和失败原因提示，并且把这一轮的说明写得足够长，长到需要靠样式截断显示"
        current = at(dict(RUNS["runs"][0], task_id="T-7", title="导出改异步", round_title=long_round, title_source="explicit",
                          cwd="/Users/me/work/shop-app/", context={"namespace": None, "chat": "c1"}, interactive=True,
                          pending_interactions=0, revision=3), "run-titledRound2", 2, 1791363100.0)
        first = at(current, "run-titledRound1", 1, 1791363050.0, round_title=None, status="ok", decision=DECISION)
        snap = dict(current, active=True, outcome=None, last_event=None, last_meaningful_event=None, elapsed_seconds=1.0,
                    interactions=[], control_state="running")
        self.run_node(r'''
await respond('/api/runs?limit=100&offset=0', 200, {runs: [FIRST, CURRENT, RUNS.runs[2]], next_offset: 3, has_more: false});
const cards = byId('taskList').children;
assert.equal(cards.length, 2);
assert.equal(cards[0].children[0].textContent, '导出改异步');
assert.match(cards[0].children[1].textContent, /^动手改 · 第 2 轮 · 补上 CSV 导出和失败原因提示.*截断显示 · \d+\/\d+ \d\d:\d\d$/);
assert.deepEqual(all(cards[0], n => /card-proj/.test(n.className)).map(n => n.textContent), ['shop-app']);
assert.equal(cards[1].children[0].textContent, '把导出改成异步，失败要提示原因', 'a 1.0 row keeps showing its stored title');
assert.match(cards[1].children[1].textContent, /^动手改 · 第 1 轮 · \d+\/\d+ \d\d:\d\d$/);
assert.equal(all(cards[1], n => /card-proj/.test(n.className)).length, 0);

await respond('run_id=run-titledRound2', 200, SNAP);
assert.equal(byId('runTitle').textContent, '导出改异步');
assert.equal(byId('runRound').hidden, false);
assert.equal(byId('runRound').textContent, '第 2 轮：' + LONG);
assert.match(byId('runFacts').textContent, /^项目 shop-app动手改副本模式/);
const tabs = byId('roundTabs').children;
assert.deepEqual(tabs.map(t => t.textContent), ['第 1 轮 · 改后通过', '第 2 轮 · ' + LONG + ' · 进行中']);
assert.deepEqual(all(tabs[1], n => n.className === 'tab-sub').map(n => n.textContent), [' · ' + LONG], 'the full text stays; CSS truncates it');
assert.equal(tabs[1].title, LONG);
assert.match(byId('idBody').textContent, /项目目录\/Users\/me\/work\/shop-app\/.*对话c1/);
assert.equal(byId('askBox').children.length, 0);

await show(FINISHED);
assert.equal(byId('runTitle').textContent, '把导出改成异步，失败要提示原因');
assert.equal(byId('runRound').hidden, true);
assert.equal(byId('runRound').textContent, '');
assert.deepEqual(byId('roundTabs').children.map(t => t.textContent), ['第 1 轮 · 改后通过']);
assert.match(byId('runFacts').textContent, /^动手改副本模式/);
assert.doesNotMatch(byId('idBody').textContent, /项目目录|对话/);
''', CURRENT=current, FIRST=first, SNAP=snap, LONG=long_round)

    def test_pending_questions_are_shown_as_text_with_the_reply_hint(self):
        question = interaction("i-1", "question", "pending", {"questions": [{
            "question": "<img src=x onerror=alert(1)>用哪个数据库？", "header": "存储", "multiSelect": False,
            "options": [{"label": "PostgreSQL", "description": "已有运维"}, {"label": "<b>SQLite</b>", "description": ""}]}]})
        permission = interaction("i-2", "permission", "pending", {"tool_name": "Bash", "summary": "rm -rf build/"})
        note = interaction("i-3", "note", "acknowledged", {"text": "先跳过了 lint，原因见报告", "scope": "src/"}, blocking=False)
        answered = interaction("i-4", "question", "answered", {"questions": [{"question": "要保留旧接口吗？", "header": "",
                                                                              "options": [], "multiSelect": False}]},
                               answer={"text": "保留，标记弃用"}, answered_by="coordinator")
        held = dict(RUNNING, run_id="run-heldQuestion1", interactive=True, pending_interactions=2, control_state="held", revision=4,
                    interactions=[question, permission, note, answered])
        self.run_node(r'''
await respond('/api/runs?limit=100&offset=0', 200, {runs: [ROW], next_offset: 1, has_more: false});
const badges = all(byId('taskList').children[0], n => /badge/.test(n.className));
assert.deepEqual(badges.map(b => [b.textContent, b.className]), [['等回答', 'badge t-warn']]);
await respond('run_id=run-heldQuestion1', 200, HELD);
const ask = byId('askBox');
assert.deepEqual(ask.children.map(n => [n.tagName, n.className]), [['SECTION', 'ask attn'], ['SECTION', 'ask attn'], ['SECTION', 'ask'], ['DETAILS', 'done']]);
const [q, perm, notes, done] = ask.children.map(n => n.textContent);
assert.ok(q.startsWith('Claude 在等回答存储<img src=x onerror=alert(1)>用哪个数据库？'), q);
assert.match(q, /PostgreSQL · 已有运维<b>SQLite<\/b>执行已暂停 · 请在 Codex 里回复，工作台只读$/);
assert.equal(all(ask, n => n.tagName === 'IMG' || n.tagName === 'B' && n.textContent.includes('SQLite')).length, 0);
assert.deepEqual(all(ask.children[0], n => n.tagName === 'LI').map(n => n.textContent), ['PostgreSQL · 已有运维', '<b>SQLite</b>']);
assert.equal(perm, '请求授权Bash rm -rf build/执行已暂停 · 请在 Codex 里回复，工作台只读');
assert.equal(notes, 'Claude 的说明说明先跳过了 lint，原因见报告范围：src/');
assert.equal(done, '已处理的问答（1）提问 · 已回答要保留旧接口吗？Codex 回答：保留，标记弃用');
assert.match(byId('hero').textContent, /^Claude 在等回答，执行已暂停/);
assert.deepEqual(byId('roundTabs').children.map(t => t.textContent), ['第 1 轮 · 等回答']);

await show({...HELD, run_id: 'run-softQuestion1', control_state: 'running', interactions: [{...HELD.interactions[0], blocking: false}]});
assert.equal(ask.children.length, 1);
assert.match(ask.textContent, /请在 Codex 里回复/);
assert.doesNotMatch(ask.textContent + byId('hero').textContent, /执行已暂停/);
await show(FINISHED);
assert.deepEqual(ask.children.map(n => n.className), ['ask'], 'the 1.0 questions_for_user box is unchanged');
assert.match(ask.textContent, /^Claude 有问题问你.*要不要保留旧接口？/);
await show(READONLY);
assert.equal(ask.children.length, 0);
''', ROW=list_row(held), HELD=held)

    def test_chat_link_lists_only_that_chat_and_keeps_it_in_the_address(self):
        self.run_node(r'''
assert.equal(calls[0].url, '/api/runs?limit=100&offset=0&chat=c%201&namespace=n1');
assert.equal(calls[0].opts.headers.Authorization, 'Bearer t');
assert.equal(byId('listHead').textContent, '本对话的任务');
await respond('/api/runs', 200, RUNS);
assert.equal(loc.hash, '#token=t&run=run-POaJz2M5Ml3AAjij&chat=c%201&ns=n1');
timers.find(t => t.ms === 5000).f();
assert.match(pending.at(-1).url, /^\/api\/runs\?limit=100&offset=0&chat=c%201&namespace=n1$/);
''', hash_value="token=t&chat=c%201&ns=n1")
        self.run_node(r'''
assert.equal(calls[0].url, '/api/runs?limit=100&offset=0&chat=c1');
''', hash_value="token=t&chat=c1")
        self.run_node(r'''
assert.equal(calls[0].url, '/api/runs?limit=100&offset=0');
assert.equal(byId('listHead').textContent, '全部任务');
await respond('/api/runs', 200, RUNS);
assert.equal(loc.hash, '#token=fixture&run=run-POaJz2M5Ml3AAjij');
''')

    def test_collaboration_event_kinds_are_translated(self):
        self.run_node(r'''
for (const [kind, text] of Object.entries(KINDS)) {
  assert.equal(run(`evText(${JSON.stringify({kind, summary: 'english summary of ' + kind, status: 'held'})})`), text);
  assert.match(out(`label(L.ev, ${JSON.stringify(kind)}, '')`), /^[一-鿿]+$/, kind + ' needs a short Chinese kind label');
}
run(`S.events = ${JSON.stringify([{seq: 1, received_at: 1, kind: 'hold', summary: 'execution held for an answer', status: 'held'},
  {seq: 2, received_at: 2, kind: 'answer', summary: 'coordinator answered', status: 'delivered', text: '用 SQLite'}])}`);
const view = run('eventsView()');
assert.match(view.textContent, /暂停执行等回答.*Codex 回答.*用 SQLite/);
assert.doesNotMatch(view.textContent, /execution held|coordinator answered|held|delivered/);
assert.deepEqual(all(view, n => n.className === 'k muted').map(n => n.textContent), ['暂停', '回答']);
''', KINDS=EVENT_KINDS)


    def test_structured_answers_remain_visible_without_free_text(self):
        mapped = interaction("i-map", "question", "answered", {"questions": []},
                             answer={"answers": {"Output format?": "JSON", "Keep timestamps?": "yes"}},
                             answered_by="coordinator")
        both = dict(mapped, answer={**mapped["answer"], "text": "Keep existing formatting"})
        self.run_node(r'''
const selected = run('ixAnswer(' + JSON.stringify(MAPPED) + ')');
assert.match(selected, /Codex 回答/);
assert(selected.includes('Output format?：JSON' + String.fromCharCode(10) + 'Keep timestamps?：yes'));
const explained = run('ixAnswer(' + JSON.stringify(BOTH) + ')');
assert(explained.includes('Output format?：JSON'));
assert(explained.includes('Keep existing formatting'));
''', MAPPED=mapped, BOTH=both)


    def test_collaboration_warnings_show_chinese_titles_and_keep_the_raw_detail(self):
        ended = dict(READONLY, run_id="run-collabWarn001", outcome=dict(READONLY["outcome"], warnings=[
            {"code": "questions_unanswered", "items": ["ix-1"],
             "detail": "Claude's blocking question(s) were still waiting for Codex when the run ended"},
            {"code": "turn_interrupted",
             "detail": "1 earlier turn(s) ended with an error or interrupt; the last turn's result is the final one"},
            {"code": "interactions_unavailable", "detail": "OperationalError: database is locked"}]))
        self.run_node(r'''
await show(RUN);
const cards = byId('warnList').children;
assert.deepEqual(cards.map(c => c.children[0].textContent), ['提问没等到回答', '中途有回合中断', '问答记录没读到']);
RUN.outcome.warnings.forEach((w, i) => {
  assert.match(cards[i].children[1].textContent, /[一-鿿]/, w.code + ' needs a Chinese description');
  assert.ok(!cards[i].children[1].textContent.includes(w.code), 'the raw code stays in the technical details');
  assert.equal(all(cards[i], n => n.tagName === 'DETAILS')[0].textContent, '技术细节' + w.code + w.detail);
});
assert.deepEqual(all(cards[0], n => n.className === 'verbatim').map(n => n.textContent), ['ix-1']);
''', RUN=ended)

    def test_one_lineage_is_one_card_across_the_upgrade_and_same_name_lineages_stay_apart(self):
        root = "/state/v1/runs/run-upgradeRound1"
        old = dict(list_row(FINISHED), task_id="T-OLD", run_id="run-upgradeRound1", run_dir=root, lineage_root=root, context=None)
        new = at(dict(old, title="T-OLD", title_source="task_id", context={"namespace": "default", "chat": "c1"}, decision=None,
                      continue_from=old["run_id"], run_dir="/state/v1/runs/run-upgradeRound2"),
                 "run-upgradeRound2", 2, old["started_at"] + 100)
        fresh = at(dict(new, continue_from=None, run_dir="/state/v1/runs/run-freshLineage1", lineage_root="/state/v1/runs/run-freshLineage1"),
                   "run-freshLineage1", 1, old["started_at"] + 50)
        other = at(dict(fresh, context={"namespace": "default", "chat": "c2"}, run_dir="/state/v1/runs/run-otherChat00001",
                        lineage_root="/state/v1/runs/run-otherChat00001"), "run-otherChat00001", 1, old["started_at"] + 60)
        rows = {"OLD": old, "NEW": new, "FRESH": fresh, "OTHER": other}
        self.run_node(r'''
const groups = out(`groupTasks(${JSON.stringify([OLD, NEW, FRESH, OTHER])})`);
assert.deepEqual(groups.map(g => g.runs.map(r => r.run_id)),
  [['run-upgradeRound1', 'run-upgradeRound2'], ['run-otherChat00001'], ['run-freshLineage1']]);
await respond('/api/runs', 200, {runs: [NEW, FRESH, OLD], next_offset: 3, has_more: false});
const cards = () => byId('taskList').children, tabs = () => byId('roundTabs').children;
assert.equal(cards().length, 2, 'the chat-less first round joins its lineage; a fresh lineage of the same name stays apart');
assert.equal(run('S.sel'), 'run-upgradeRound2');
await respond('run_id=run-upgradeRound2', 200, {...FINISHED, ...NEW});
assert.deepEqual(tabs().map(t => t.textContent), ['第 1 轮 · 改后通过', '第 2 轮 · 待验收']);
tabs()[0].click();
await respond('run_id=run-upgradeRound1', 200, {...FINISHED, ...OLD});
assert.equal(cards().length, 2);
assert.deepEqual(tabs().map(t => t.getAttribute('aria-selected')), ['true', 'false']);
assert.equal(byId('runTitle').textContent, '把导出改成异步，失败要提示原因', 'the 1.0 round keeps its stored title');
''', hash_value="token=t&chat=c1&ns=default", **rows)
        self.run_node(r'''
await respond('/api/runs', 200, {runs: [NEW], next_offset: 1, has_more: false});
await respond('run_id=run-upgradeRound2', 200, {...FINISHED, ...NEW});
all(byId('runFacts'), n => n.tagName === 'BUTTON')[0].click();
await respond('run_id=run-upgradeRound1', 200, {...FINISHED, ...OLD});
assert.equal(byId('taskList').children.length, 1, 'an earlier round opened through its continue_from joins the same card');
assert.deepEqual(byId('roundTabs').children.map(t => t.textContent), ['第 1 轮 · 改后通过', '第 2 轮 · 待验收']);
''', hash_value="token=t&chat=c1&ns=default", **rows)

    def test_update_and_interrupt_rows_show_their_text_and_origin(self):
        said = "Codex sent an update"
        stop = "Codex asked Claude to stop the current step"
        events = [{"seq": 1, "received_at": 1, "kind": "update", "summary": said, "status": "coordinator", "text": "另外 CSV 也要导出"},
                  {"seq": 2, "received_at": 2, "kind": "update", "summary": said, "status": "human", "text": "先别动公开接口"},
                  {"seq": 3, "received_at": 3, "kind": "interrupt", "summary": stop, "status": "coordinator", "text": "停下，改用队列实现"},
                  {"seq": 4, "received_at": 4, "kind": "interrupt", "summary": stop, "status": "human"},
                  {"seq": 5, "received_at": 5, "kind": "answer", "summary": "Codex answered Claude", "status": "coordinator"},
                  {"seq": 6, "received_at": 6, "kind": "update", "summary": said}]
        self.run_node(r'''
run(`S.events = ${JSON.stringify(EVENTS_IN)}`);
const rows = run('eventsView()').children.map(r => r.children[2]);
assert.deepEqual(rows.map(r => r.textContent), ['Codex 补充 · Codex 协调者另外 CSV 也要导出', 'Codex 补充 · 用户原话先别动公开接口',
  '请求打断 · Codex 协调者停下，改用队列实现', '请求打断 · 用户原话', 'Codex 回答 · Codex 协调者', 'Codex 补充']);
assert.deepEqual(rows.map(r => all(r, n => n.className === 'muted verbatim').map(n => n.textContent)),
  [['另外 CSV 也要导出'], ['先别动公开接口'], ['停下，改用队列实现'], [], [], []]);
''', EVENTS_IN=events)

    def test_a_dead_link_keeps_retrying_and_says_to_get_a_new_link_in_codex(self):
        self.assertNotIn("window.open", PAGE.read_text(encoding="utf-8"))
        self.run_node(r'''
pending.shift().reject(new TypeError('Failed to fetch'));
await tick(); await tick();
const banner = byId('banner');
assert.match(banner.textContent, /^暂时连不上，正在重试：连不上本机工作台。.*Codex 重启过.*这个链接已失效.*在 Codex 里.*取新链接$/);
assert.equal(byId('conn').textContent, '已断开');
assert.equal(timers.at(-1).ms, 5000, 'a short outage is still retried');
timers.at(-1).f();
await respond('/api/runs', 200, RUNS);
assert.equal(banner.hidden, true);
''')

if __name__ == "__main__":
    unittest.main(verbosity=2)
