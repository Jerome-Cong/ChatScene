"""Real offline Chromium tests; opt in with BUS_BENCHMARK_BROWSER_TESTS=1."""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from bus_benchmark.jsonio import read_json, read_jsonl
from bus_benchmark.review_packet import export_packet, validate_browser_submission
from bus_benchmark.review_wire import wire_hash

ROOT=Path(__file__).resolve().parents[2]


def wait(page, expression):
    # Playwright's retry evaluator uses eval under strict CSP in this release.
    # CDP evaluate preserves the page CSP; never add unsafe-eval to the app.
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        if page.evaluate(expression):
            return
        time.sleep(0.025)
    raise AssertionError("browser condition did not become true: "+expression)


@unittest.skipUnless(os.environ.get('BUS_BENCHMARK_BROWSER_TESTS')=='1', 'run explicitly in the project Playwright environment')
class OfflineBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop()

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='bw04-browser-')
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.library=read_jsonl(ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl')[:2]
        self.oracle=read_jsonl(ROOT/'benchmark_artifacts/drafts/dev_oracle_draft.jsonl')[:2]
        self.library[0]['query_text']+=' </script><img src=x onerror="window.attacked=true"> 中文🚍'
        exported=export_packet(self.library,self.oracle,'synthetic-browser-test',self.root/'packet')
        self.packet=read_json(exported['assignment']);self.url=Path(exported['html']).as_uri()
        self.context=self.browser.new_context(offline=True)
        self.addCleanup(self.context.close)
        self.page=self.context.new_page()
        self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)))

    def open(self):
        self.page.goto(self.url)
        wait(self.page, 'window.ReviewWorkbench && ReviewWorkbench.ready()')
        wait(self.page, "document.getElementById('storage').textContent.includes('已保存') || document.getElementById('storage').textContent.includes('仅保存在内存')")

    def backup(self):
        return self.page.evaluate('ReviewWorkbench.backup()')

    def test_offline_edit_confirm_export_import_and_text_safety(self):
        requests=[];self.page.on('request',lambda request:requests.append(request.url))
        self.open()
        for index,item in enumerate(self.packet['items']):
            self.page.locator('#queue').select_option(str(index))
            if item['task']['query_record']['surface_style']=='precise':
                card=self.page.locator('.card').first
                card.get_by_label('要求处理',exact=True).select_option('modify')
                card.get_by_label('数量',exact=True).fill('2')
            self.page.locator('#notes').fill('人工测试备注：中文🚍 </script><img src=x onerror="window.attacked=true">')
            self.page.locator('#confirm').click()
            wait(self.page, f"ReviewWorkbench.getState().entries[{index}].status==='submitted'")
        backup=self.backup()
        result=validate_browser_submission(self.packet,backup,self.library,self.oracle)
        self.assertEqual(result['subjects_submitted'],2)
        self.assertFalse(result['human_gold'])
        self.assertIsNone(self.page.evaluate('window.attacked'))
        self.assertEqual(self.page.locator('img').count(),0)
        self.assertFalse(any(url.startswith(('https:','http:')) for url in requests))
        with self.page.expect_download() as download:
            self.page.locator('#export').click()
        target=self.root/'exported.json';download.value.save_as(target)
        self.assertEqual(json.loads(target.read_text())['backup_sha256'],backup['backup_sha256'])
        self.assertEqual(self.errors,[])

    def test_reload_stale_and_foreign_backups_preserve_newer_work(self):
        self.open();self.page.locator('#notes').fill('first edit')
        wait(self.page, "document.getElementById('storage').textContent.startsWith('已保存')")
        old=self.backup();self.page.locator('#notes').fill('newer edit')
        wait(self.page, "document.getElementById('storage').textContent.startsWith('已保存')")
        current=self.backup()
        outcome=self.page.evaluate('(b)=>ReviewWorkbench.restore(b).then(()=>"accepted",e=>e.message)',old)
        self.assertIn('较旧',outcome)
        other=copy.deepcopy(current);other['reviewer_id']='other';other['backup_sha256']=wire_hash({k:v for k,v in other.items() if k!='backup_sha256'})
        outcome=self.page.evaluate('(b)=>ReviewWorkbench.restore(b).then(()=>"accepted",e=>e.message)',other)
        self.assertIn('审阅者',outcome)
        self.page.reload();wait(self.page, 'ReviewWorkbench.ready()')
        self.assertEqual(self.page.locator('#notes').input_value(),'newer edit')
        self.assertEqual(self.backup()['generation'],current['generation'])

    def test_second_tab_cannot_edit_until_first_closes(self):
        self.open();second=self.context.new_page();second.goto(self.url)
        wait(second, "document.getElementById('storage').textContent.includes('另一标签页')")
        self.assertFalse(second.evaluate('ReviewWorkbench.ready()'))
        self.assertTrue(second.locator('#notes').is_disabled())
        self.assertTrue(second.locator('#confirm').is_disabled())
        self.page.close();second.reload();wait(second, 'ReviewWorkbench.ready()')

    def test_quota_failure_is_memory_only_but_exportable(self):
        self.context.add_init_script("Storage.prototype.setItem=function(){throw new DOMException('quota','QuotaExceededError')}")
        self.open();self.page.locator('#notes').fill('memory only')
        self.page.locator('#confirm').click()
        wait(self.page, "document.getElementById('storage').textContent.includes('仅保存在内存')")
        backup=self.backup()
        self.assertEqual(backup['entries'][0]['form']['notes'],'memory only')
        self.assertIn('仅在内存',self.page.locator('#message').inner_text())
        self.assertEqual(validate_browser_submission(self.packet,backup,self.library,self.oracle)['subjects_submitted'],1)

    def test_corrupt_local_state_is_retained_without_overwrite(self):
        self.open();key='bus-review:'+self.packet['packet_id']+':'+self.packet['reviewer_id']
        self.page.evaluate('(key)=>localStorage.setItem(key,"{broken")',key)
        self.page.reload();wait(self.page, "document.getElementById('storage').textContent.includes('损坏')")
        self.assertFalse(self.page.evaluate('ReviewWorkbench.ready()'))
        self.assertEqual(self.page.evaluate('(key)=>localStorage.getItem(key)',key),'{broken')

    def test_defer_and_edit_after_submit_have_no_valid_confirmation(self):
        self.open();self.page.locator('#confirm').click();wait(self.page, "ReviewWorkbench.getState().entries[0].status==='submitted'")
        self.page.locator('#notes').fill('changed after confirmation')
        state=self.page.evaluate('ReviewWorkbench.getState()')
        self.assertEqual(state['entries'][0]['status'],'draft');self.assertEqual(state['entries'][0]['receipts'],[])
        self.page.locator('#issue').select_option('source_error');self.page.locator('#defer').click()
        state=self.page.evaluate('ReviewWorkbench.getState()')
        self.assertEqual(state['entries'][0]['status'],'requires_source_fix')
        self.assertEqual(state['entries'][0]['form']['notes'],'changed after confirmation')
        result=validate_browser_submission(self.packet,self.backup(),self.library,self.oracle)
        self.assertEqual(result['subjects_submitted'],0)

    def test_python_browser_hash_vectors_and_corrupt_restore(self):
        self.open()
        for value in (None,True,1,8.0,-0.0,1e-7,1e21,'中文🚍\u2028',{'😀':[1,2.5], '\ue000':'x'}):
            self.assertEqual(self.page.evaluate('(v)=>ReviewWorkbench.hash(v)',value),wire_hash(value))
        original=self.backup();broken=copy.deepcopy(original);broken['generation']+=1;broken['entries'][0]['form']['injected']='bad';broken['backup_sha256']=wire_hash({k:v for k,v in broken.items() if k!='backup_sha256'})
        result=self.page.evaluate('(b)=>ReviewWorkbench.restore(b).then(()=>"accepted",e=>e.message)',broken)
        self.assertIn('表单',result)
        self.assertEqual(self.backup(),original)

    def test_unchanged_modify_and_duplicate_split_stay_draft_until_corrected(self):
        self.open()
        index=next(i for i,item in enumerate(self.packet['items']) if item['task']['query_record']['surface_style']=='precise')
        self.page.locator('#queue').select_option(str(index))
        card=self.page.locator('.card').first
        card.get_by_label('要求处理',exact=True).select_option('modify')
        self.page.locator('#confirm').click()
        self.assertIn('没有实际语义修改',self.page.locator('#message').inner_text())
        card.get_by_label('要求处理',exact=True).select_option('split')
        self.page.locator('#confirm').click()
        self.assertIn('拆分目标重复',self.page.locator('#message').inner_text())
        self.assertEqual(self.page.evaluate(f'ReviewWorkbench.getState().entries[{index}].status'),'draft')
        card.get_by_label('参与者角色',exact=True).nth(1).fill('synthetic_second_actor')
        self.page.locator('#confirm').click()
        wait(self.page,f"ReviewWorkbench.getState().entries[{index}].status==='submitted'")
        self.assertEqual(validate_browser_submission(self.packet,self.backup(),self.library,self.oracle)['subjects_submitted'],1)

    def test_merge_requires_real_change_and_uses_one_shared_editor(self):
        self.open()
        index=next(i for i,item in enumerate(self.packet['items']) if item['task']['query_record']['surface_style']=='precise')
        self.page.locator('#queue').select_option(str(index))
        card=self.page.locator('.card').nth(2)
        card.get_by_label('要求处理',exact=True).select_option('merge')
        card.locator('select').nth(1).select_option('3')
        self.page.locator('#confirm').click()
        self.assertIn('没有实际语义修改',self.page.locator('#message').inner_text())
        self.assertIn('共用第',self.page.locator('.card').nth(3).inner_text())
        card.get_by_label('取值',exact=True).fill('synthetic_different_road')
        self.page.locator('#confirm').click()
        wait(self.page,f"ReviewWorkbench.getState().entries[{index}].status==='submitted'")
        self.assertEqual(validate_browser_submission(self.packet,self.backup(),self.library,self.oracle)['subjects_submitted'],1)

    def test_embedded_guide_and_practice_do_not_change_answers(self):
        self.open();before=self.backup()
        self.page.locator('#help').click()
        self.assertTrue(self.page.locator('#guide').is_visible())
        self.assertEqual(self.page.locator('#practice article').count(),6)
        self.page.get_by_role('button',name='显示参考解释').first.click()
        self.page.locator('#close-guide').click()
        self.assertEqual(self.backup(),before)

    def test_literal_proposal_application_requires_explicit_resolution_and_confirmation(self):
        self.library=self.library[:1];self.oracle=self.oracle[:1]
        self.library[0]['query_text']='Two cyclists cross.'
        # A new synthetic query has no original numeric text span to preserve.
        self.oracle[0]['atoms']=[a for a in self.oracle[0]['atoms'] if a['provenance']['source']!='query_text_regex']
        exported=export_packet(self.library,self.oracle,'synthetic-browser-test',self.root/'suggestions',with_suggestions=True)
        self.packet=read_json(exported['assignment']);self.url=Path(exported['html']).as_uri()
        self.open();self.page.locator('#confirm').click()
        self.assertIn('仍有疑问',self.page.locator('#message').inner_text())
        self.page.get_by_role('button',name='采用此修订草稿').first.click()
        state=self.page.evaluate('ReviewWorkbench.getState()')
        self.assertEqual(state['entries'][0]['edit_sources'][-1]['origin'],'machine_proposal')
        self.assertFalse(state['entries'][0]['human_gold'])
        self.assertEqual(state['entries'][0]['receipts'],[])
        self.assertEqual(json.loads(state['entries'][0]['form']['atom_decisions'][0]['replacement_atoms_json'])[0]['arguments']['count'],'2')
        while self.page.get_by_role('button',name='我已核对这条疑问，使用当前编辑').count():
            self.page.get_by_role('button',name='我已核对这条疑问，使用当前编辑').first.click()
        self.page.locator('#confirm').click()
        wait(self.page,"ReviewWorkbench.getState().entries[0].status==='submitted'")
        result=validate_browser_submission(self.packet,self.backup(),self.library,self.oracle)
        self.assertEqual(result['subjects_submitted'],1)
        reason=result['responses'][0]['response']['atom_decisions'][0]['reason']
        self.assertIn('Human attestation:',reason)
        self.assertIn('Machine rationale:',reason)

    def test_unknown_predicate_named_like_object_property_remains_visible(self):
        from bus_benchmark.atoms import make_atom
        atom=self.oracle[0]['atoms'][0]
        self.oracle[0]['atoms'][0]=make_atom(atom['category'],'constructor',atom['arguments'],layer=atom['layer'],polarity=atom['polarity'],provenance=atom['provenance'])
        exported=export_packet(self.library,self.oracle,'synthetic-browser-test',self.root/'unknown-predicate')
        self.packet=read_json(exported['assignment']);self.url=Path(exported['html']).as_uri()
        self.open()
        index=next(i for i,item in enumerate(self.packet['items']) if item['task']['query_record']['surface_style']=='precise')
        self.page.locator('#queue').select_option(str(index))
        self.assertIn('未登记要求',self.page.locator('.card').first.inner_text())
        self.page.locator('#confirm').click()
        self.assertIn('未登记要求类型',self.page.locator('#message').inner_text())
        self.assertEqual(self.errors,[])

    def test_migrated_confirmations_restore_as_carried_and_edits_reopen(self):
        from bus_benchmark.review_migration import migrate_review
        from bus_benchmark.jsonio import write_json
        from test_review_packet import browser_backup
        old_assignment=self.root/'packet/assignment.json';old_backup=self.root/'old-progress.json'
        write_json(old_backup,browser_backup(self.packet,True))
        migrate_review(old_submission_path=old_backup,old_assignment_path=old_assignment,old_library=self.library,old_oracle=self.oracle,new_library=self.library,new_oracle=self.oracle,output_dir=self.root/'migration')
        self.packet=read_json(self.root/'migration/packet/assignment.json');self.url=(self.root/'migration/packet/review.html').as_uri()
        backup=read_json(self.root/'migration/progress.json')
        self.open();self.page.evaluate('(value)=>ReviewWorkbench.restore(value)',backup)
        self.assertIn('沿用原确认',self.page.locator('#subject').inner_text())
        self.assertEqual(validate_browser_submission(self.packet,self.backup(),self.library,self.oracle)['subjects_submitted'],2)
        self.page.locator('#notes').fill('new edit after migration')
        self.assertEqual(self.page.evaluate('ReviewWorkbench.getState().entries[0].status'),'draft')
        self.assertEqual(self.page.evaluate('ReviewWorkbench.getState().entries[0].receipts'),[])
        self.assertNotIn('沿用原确认',self.page.locator('#subject').inner_text())
        self.assertEqual(validate_browser_submission(self.packet,self.backup(),self.library,self.oracle)['subjects_submitted'],1)

    def test_changed_source_keeps_old_notes_read_only_and_requires_new_review(self):
        from bus_benchmark.review_migration import migrate_review
        from bus_benchmark.jsonio import write_json
        self.open();self.page.locator('#notes').fill('old human edit to retain')
        self.page.locator('#confirm').click();wait(self.page,"ReviewWorkbench.getState().entries[0].status==='submitted'")
        old=self.backup()
        # Model an earlier expert CPD edit in this synthetic confirmed backup.
        from bus_benchmark.review_packet import browser_snapshot
        entry=old['entries'][0];source=self.packet['items'][0]
        policy=copy.deepcopy(source['task']['oracle_draft']['cpd_policy']);policy['cross_platform_judgeable']=False
        entry['form']['cpd_decision'].update(verdict='revise',reason='unique old CPD revision reason',replacement_policy_json=json.dumps(policy))
        entry['revision']+=1;entry['edit_sources'].append({'revision':entry['revision'],'origin':'human'})
        entry['receipts'][0]['revision']=entry['revision']
        entry['receipts'][0]['content_sha256']=wire_hash(browser_snapshot(self.packet['packet_id'],source['task'],source['proposal'],entry,source.get('revision_proposals'),source.get('surface_diff')))
        old['generation']+=1;old['backup_sha256']=wire_hash({k:v for k,v in old.items() if k!='backup_sha256'})
        old_path=self.root/'old-progress.json';write_json(old_path,old)
        changed=copy.deepcopy(self.library);qid=self.packet['items'][0]['task']['subject_id']
        next(row for row in changed if row['query_id']==qid)['query_text']+=' Corrected source wording.'
        migrate_review(old_submission_path=old_path,old_assignment_path=self.root/'packet/assignment.json',old_library=self.library,old_oracle=self.oracle,new_library=changed,new_oracle=self.oracle,output_dir=self.root/'changed-migration')
        self.packet=read_json(self.root/'changed-migration/packet/assignment.json');self.url=(self.root/'changed-migration/packet/review.html').as_uri();self.library=changed
        self.open();self.page.evaluate('(value)=>ReviewWorkbench.restore(value)',read_json(self.root/'changed-migration/progress.json'))
        self.page.locator('#prior-review > details > summary').click()
        self.assertIn('old human edit to retain',self.page.locator('#prior-review').inner_text())
        self.page.get_by_text('完整旧表单（只读技术记录，含全部理由与 CPD 修订）',exact=True).click()
        self.assertIn('unique old CPD revision reason',self.page.locator('#prior-review pre').inner_text())
        self.assertIn('required_check_decisions',self.page.locator('#prior-review pre').inner_text())
        self.assertEqual(self.page.evaluate('ReviewWorkbench.getState().entries[0].status'),'deferred')
        self.assertEqual(self.page.evaluate('ReviewWorkbench.getState().entries[0].receipts'),[])
        self.page.locator('#confirm').click()
        self.assertIn('仍有疑问',self.page.locator('#message').inner_text())
