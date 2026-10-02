"""Full isolated review evidence must be readable without trusting provider paths."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from viewer import Viewer, read_artifact
import test_review_regressions as dashboard_tests


class ReviewReportViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.text = json.dumps({'summary': '完整报告', 'evidence': ['中文证据😀\n' * 40000]}, ensure_ascii=False)
        self.data = self.text.encode('utf-8')
        self.path = self.folder / 'review-report.json'
        self.path.write_bytes(self.data)
        self.report = {'status': 'delivered', 'path': self.path.name, 'bytes': len(self.data),
                       'sha256': hashlib.sha256(self.data).hexdigest()}
        outer = self
        class Records:
            def snapshot(self, run_id):
                return {'run_dir': str(outer.folder), 'result': {'review_report': outer.report}}
        self.records = Records()

    def test_full_large_multibyte_report_reconstructs_exactly(self):
        self.assertGreater(len(self.data), 300000)
        offset, pieces = 0, []
        while offset is not None:
            page = read_artifact(self.records, 'r', 'review_report', offset=offset, limit=65536)
            self.assertTrue(page['available'], page)
            self.assertLessEqual(page['page_bytes'], 65536)
            pieces.append(page['content'])
            offset = page['next_offset_bytes']
        self.assertEqual(''.join(pieces), self.text)
        self.assertEqual(json.loads(''.join(pieces)), json.loads(self.text))

    def test_every_page_rechecks_full_capture_hash(self):
        first = read_artifact(self.records, 'r', 'review_report', limit=4096)
        changed = bytearray(self.data)
        changed[-3] = ord('x')
        self.path.write_bytes(changed)
        page = read_artifact(self.records, 'r', 'review_report', offset=first['next_offset_bytes'])
        self.assertEqual(page['state'], 'integrity_mismatch')
        self.assertIsNone(page['content'])

    def test_arbitrary_recorded_path_and_symlink_are_never_served(self):
        for path in ('../secret.json', str(self.path), 'another.json'):
            with self.subTest(path=path):
                self.report['path'] = path
                page = read_artifact(self.records, 'r', 'review_report')
                self.assertEqual(page['state'], 'record_invalid')
                self.assertIsNone(page['content'])
        self.report['path'] = self.path.name
        target = self.folder / 'secret.json'
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertEqual(read_artifact(self.records, 'r', 'review_report')['state'], 'unreadable')

    def test_hardlink_and_invalid_capture_metadata_refuse_content(self):
        for field, value in (("bytes", True), ("bytes", None), ("sha256", None)):
            original = self.report[field]
            self.report[field] = value
            with self.subTest(field=field, value=value):
                page = read_artifact(self.records, 'r', 'review_report')
                self.assertEqual(page['state'], 'record_invalid')
                self.assertIsNone(page['content'])
            self.report[field] = original
        os.link(self.path, self.folder / 'alias.json')
        self.assertEqual(read_artifact(self.records, 'r', 'review_report')['state'], 'unreadable')

    def test_unrecorded_report_has_no_unverified_fallback(self):
        self.report = None
        page = read_artifact(self.records, 'r', 'review_report')
        self.assertEqual(page['state'], 'not_recorded')
        self.assertIsNone(page['content'])

    def test_http_pagination_preserves_auth_and_validates_boundaries(self):
        view = Viewer(self.records).start()
        self.addCleanup(view.close)
        url = view.url().split('#')[0] + 'api/artifact?run_id=r&name=review_report'
        with self.assertRaises(HTTPError) as error:
            urlopen(url, timeout=5)
        self.assertEqual(error.exception.code, 401)
        headers = {'Authorization': 'Bearer ' + view.token}
        with urlopen(Request(url + '&limit=64', headers=headers), timeout=5) as response:
            page = json.load(response)
        self.assertEqual(page['total_bytes'], len(self.data))
        self.assertLessEqual(page['page_bytes'], 64)
        # The first Chinese byte is a valid offset; its continuation byte is not.
        continuation = self.data.index('完'.encode()) + 1
        for query in (f'offset={continuation}', 'limit=0', 'index=1'):
            with self.subTest(query=query), self.assertRaises(HTTPError) as error:
                urlopen(Request(url + '&' + query, headers=headers), timeout=5)
            self.assertEqual(error.exception.code, 400)


class ReviewReportDashboardTests(unittest.TestCase):
    def test_report_pages_and_drawer_http_failure(self):
        harness = dashboard_tests.HARNESS.split('(async()=>{', 1)[0] + r'''
(async()=>{
  run("conn.status='online';markCriticalFailure('runs',{status:500});");
  byId('maintenanceBtn').onclick();
  assert.equal(byId('drawerConnection').textContent,'同步失败');
  run("markCriticalFailure('runs',new Error('network')); ");
  assert.equal(byId('drawerConnection').textContent,'连接已断开','open drawer updates with connection state');
  const view={run_id:'r',result:{review_report:{status:'delivered',bytes:6,sha256:'abc'}}};
  context.reviewView=view;
  run("renderReviewReport($('reportBody'),reviewView,null)");
  assert.match(allText(byId('reportBody')),/完整审查报告/);
  assert.match(allText(byId('reportBody')),/加载完整报告/);
  const requests=[];
  context.fetch=async u=>{
    requests.push(u);
    const offset=Number(u.searchParams.get('offset'));
    return {ok:true,json:async()=>({available:true,offset_bytes:offset,content:offset===0?'中文':'😀',
      next_offset_bytes:offset===0?6:null,total_bytes:10,sha256:offset===3?'changed':'abc'})};
  };
  await run("loadWorkflowPage('r',0,'review_report')");
  await run("loadWorkflowPage('r',0,'review_report')");
  assert.equal(requests[0].searchParams.get('name'),'review_report');
  assert.equal(requests[1].searchParams.get('offset'),'6');
  assert.equal(run("st.workflowPages.get(workflowPageKey('r',0,'review_report')).text"),'中文😀');
  assert.equal(run("st.workflowPages.has(workflowPageKey('r',0))"),false,'legacy workflow report cache remains separate');
  run("st.workflowPages.set(workflowPageKey('bad',0,'review_report'),{text:'old',next:3,total:10,sha:'abc',loading:false,error:''})");
  await run("loadWorkflowPage('bad',0,'review_report')");
  assert.equal(run("st.workflowPages.get(workflowPageKey('bad',0,'review_report')).text"),'old','changed response cannot be appended');
  assert.match(run("st.workflowPages.get(workflowPageKey('bad',0,'review_report')).error"),/分页与已载入部分不一致/);
  run("$('reportBody').replaceChildren();renderReviewReport($('reportBody'),reviewView,null)");
  assert.match(allText(byId('reportBody')),/已载入全文 10 字节/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        dashboard_tests.DashboardRegressionTests.run_harness(self, harness)
