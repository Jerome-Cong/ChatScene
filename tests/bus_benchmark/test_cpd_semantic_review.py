import copy
import tempfile
import unittest
from pathlib import Path
from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import read_jsonl, write_json, read_json, write_jsonl
from bus_benchmark.review_packet import build_packet, validate_browser_submission, browser_snapshot, import_packet
from bus_benchmark.review_cpd_semantics import technical_template, validate_technical_review, latest_review
from bus_benchmark.review_wire import wire_hash
from bus_benchmark.review_issues import export_issues
from bus_benchmark.review_migration import migrate_review
from test_review_packet import browser_backup, rehash_backup

ROOT=Path(__file__).resolve().parents[2]

class CPDSemanticReviewTests(unittest.TestCase):
    def setUp(self):
        self.lib=read_jsonl(ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl')[:2]
        self.ora=read_jsonl(ROOT/'benchmark_artifacts/drafts/dev_oracle_draft.jsonl')[:2]
        self.packet=build_packet(self.lib,self.ora,'synthetic-cpd-reviewer')

    def rebind(self,b):
        for e,i in zip(b['entries'],self.packet['items']):
            if e['receipts']:e['receipts'][0]['content_sha256']=wire_hash(browser_snapshot(self.packet['packet_id'],i['task'],i['proposal'],e,i.get('revision_proposals'),i.get('surface_diff')))
        return rehash_backup(b)

    def test_unanswered_disputed_stale_and_duplicate_choices_fail_even_with_rehashed_receipts(self):
        for change in ('missing','restricted','stale','duplicate'):
            with self.subTest(change=change):
                b=browser_backup(self.packet,True);e=b['entries'][0];r=latest_review(e)
                if change=='missing':e['edit_sources']=[]
                elif change=='restricted':r['answers'][0]['choice']='restricted'
                elif change=='stale':e['form']['atom_decisions'][0]['verdict']='reject'
                else:r['answers'].append(copy.deepcopy(r['answers'][0]))
                with self.assertRaisesRegex(ValidationError,'CPD'):
                    validate_browser_submission(self.packet,self.rebind(b),self.lib,self.ora)

    def test_visible_confirmation_does_not_create_formal_cpd_response(self):
        b=browser_backup(self.packet,True)
        result=validate_browser_submission(self.packet,b,self.lib,self.ora)
        self.assertEqual(result['responses'],[])
        self.assertEqual(result['subjects_submitted'],2)
        self.assertTrue(result['cpd_technical_review_required'])
        for record in result['semantic_responses']:
            self.assertEqual(record['response']['cpd_decision']['verdict'],'pending_technical_review')
        approval=technical_template(self.packet,b)
        with self.assertRaises(ValidationError):validate_technical_review(approval,self.packet,b)
        approval.update(approved=True,reviewer_id='synthetic-expert')
        validate_technical_review(approval,self.packet,b)
        changed=copy.deepcopy(b);changed['generation']+=1;rehash_backup(changed)
        with self.assertRaisesRegex(ValidationError,'another'):validate_technical_review(approval,self.packet,changed)

    def test_issue_export_and_source_or_guidance_migration_preserve_local_reasons(self):
        for change in ('source','guidance'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);packet=copy.deepcopy(self.packet)
                if change=='guidance':packet['dictionary']['cpd_catalog']['version']='prior-description';packet['packet_id']=wire_hash({k:v for k,v in packet.items() if k!='packet_id'})
                b=browser_backup(packet,True);e=b['entries'][0];r=latest_review(e);r['answers'][0].update(choice='uncertain',reason='独立的CPD原因')
                e.update(receipts=[],status='deferred');e['issues']=[{'code':'cpd_semantic_question','cpd_question_id':r['answers'][0]['id'],'cpd_choice':'uncertain','reason':'独立的CPD原因'}];rehash_backup(b)
                write_json(root/'assignment.json',packet);write_json(root/'progress.json',b)
                export_issues(root/'assignment.json',root/'progress.json',self.lib,self.ora,root/'issues')
                self.assertEqual(read_json(root/'issues/issues.json')['issues'][0]['reason'],'独立的CPD原因')
                changed=copy.deepcopy(self.lib)
                if change=='source':next(row for row in changed if row['query_id']==packet['items'][0]['task']['subject_id'])['query_text']+=' Changed.'
                migrate_review(old_submission_path=root/'progress.json',old_assignment_path=root/'assignment.json',old_library=self.lib,old_oracle=self.ora,new_library=changed,new_oracle=self.ora,output_dir=root/'migration')
                new=read_json(root/'migration/packet/assignment.json');progress=read_json(root/'migration/progress.json')
                record=new['migration']['subjects'][packet['items'][0]['task']['subject_id']]
                self.assertEqual(record['previous_cpd_review']['answers'][0]['reason'],'独立的CPD原因')
                self.assertIsNone(latest_review(progress['entries'][0]))
                validate_browser_submission(new,progress,changed,self.ora)
