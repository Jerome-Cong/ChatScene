import copy
import tempfile
import unittest
from pathlib import Path

from bus_benchmark.errors import ValidationError
from bus_benchmark.human_workflow import export_query_review_bundle
from bus_benchmark.jsonio import read_json,read_jsonl,write_json,write_jsonl,sha256_bytes
from bus_benchmark.review_packet import build_packet,validate_browser_submission
from bus_benchmark.review_migration import migrate_review
from bus_benchmark.review_model import content_hash,make_proposal,new_draft,defer_draft
from bus_benchmark.review_issues import export_issues
from bus_benchmark.review_session import QueryReviewSession
from bus_benchmark.review_wire import wire_hash
from test_review_packet import browser_backup,rehash_backup

ROOT=Path(__file__).resolve().parents[2]


class ReviewMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.library=read_jsonl(ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl')[:2]
        self.oracle=read_jsonl(ROOT/'benchmark_artifacts/drafts/dev_oracle_draft.jsonl')[:2]
        self.lib=self.root/'old-library.jsonl';self.ora=self.root/'old-oracle.jsonl'
        write_jsonl(self.lib,self.library);write_jsonl(self.ora,self.oracle)
        self.packet=build_packet(self.lib,self.ora,'synthetic-migration-reviewer')
        self.assignment=self.root/'assignment.json';self.submission=self.root/'backup.json'

    def store_browser(self,packet=None,submitted=True):
        packet=packet or self.packet;write_json(self.assignment,packet)
        backup=browser_backup(packet,submitted);write_json(self.submission,backup)
        return backup

    def migrate(self,**overrides):
        args=dict(old_submission_path=self.submission,old_assignment_path=self.assignment,old_library=self.lib,old_oracle=self.ora,new_library=self.lib,new_oracle=self.ora,output_dir=self.root/'migrated')
        args.update(overrides)
        return migrate_review(**args)

    def test_ui_only_upgrade_carries_confirmation_with_a_distinct_origin(self):
        old=copy.deepcopy(self.packet)
        old['ui_sha256']['app.js']='0'*64
        for item in old['items']:item.pop('surface_diff',None)
        old['packet_id']=wire_hash({k:v for k,v in old.items() if k!='packet_id'})
        self.store_browser(old);before=self.submission.read_bytes()
        result=self.migrate()
        self.assertEqual(result['carried_confirmations'],2);self.assertFalse(result['human_gold'])
        new=read_json(self.root/'migrated/packet/assignment.json');backup=read_json(self.root/'migrated/progress.json')
        self.assertTrue(all(d['receipts'][0]['action']=='carried_confirmation' for d in backup['entries']))
        checked=validate_browser_submission(new,backup,self.lib,self.ora)
        self.assertEqual(checked['subjects_submitted'],2)
        self.assertTrue(all(v['kind']=='carried_confirmation' for v in checked['confirmation_origins'].values()))
        self.assertEqual(self.submission.read_bytes(),before)
        archive=self.root/'migrated/originals/original_submission.json'
        self.assertEqual(archive.read_bytes(),before);self.assertEqual(archive.stat().st_mode&0o222,0)
        with self.assertRaisesRegex(ValidationError,'output exists'):self.migrate()

    def test_one_changed_source_reopens_only_that_subject_and_preserves_old_edits(self):
        old=self.store_browser();new_lib=self.root/'new-library.jsonl'
        library=copy.deepcopy(self.library);library[0]['query_text']+=' Corrected source wording.';write_jsonl(new_lib,library)
        result=self.migrate(new_library=new_lib)
        self.assertEqual(result['carried_confirmations'],1);self.assertEqual(result['requires_review'],1)
        packet=read_json(self.root/'migrated/packet/assignment.json');backup=read_json(self.root/'migrated/progress.json')
        changed=next(d for d in backup['entries'] if d['task_binding']['subject_id']==library[0]['query_id'])
        self.assertEqual(changed['status'],'deferred');self.assertEqual(changed['receipts'],[])
        previous=packet['migration']['subjects'][library[0]['query_id']]['previous_form']
        self.assertEqual(previous,next(d['form'] for d in old['entries'] if d['task_binding']['subject_id']==library[0]['query_id']))
        self.assertEqual(validate_browser_submission(packet,backup,new_lib,self.ora)['subjects_submitted'],1)

    def test_changed_guide_cannot_preserve_complete_by_reusing_old_hash(self):
        old=copy.deepcopy(self.packet);old['guide']+='\nDifferent prior guidance.'
        for item in old['items']:
            p=item['proposal'];p['task_binding']['guide_sha256']=sha256_bytes(old['guide'].encode())
            p['proposal_id']=content_hash({k:v for k,v in p.items() if k!='proposal_id'})
            item['initial_draft']['task_binding']=copy.deepcopy(p['task_binding']);item['initial_draft']['proposal_id']=p['proposal_id']
        old['packet_id']=wire_hash({k:v for k,v in old.items() if k!='packet_id'})
        self.store_browser(old)
        result=self.migrate();self.assertEqual(result['carried_confirmations'],0);self.assertEqual(result['requires_review'],2)

    def test_actual_dictionary_change_cannot_hide_behind_a_stale_registry_hash(self):
        old=copy.deepcopy(self.packet)
        old['dictionary']['fields']['count'][0]='misleading count label'
        old['packet_id']=wire_hash({k:v for k,v in old.items() if k!='packet_id'})
        self.store_browser(old)
        result=self.migrate()
        self.assertEqual(result['carried_confirmations'],0)
        self.assertEqual(result['requires_review'],2)
        packet=read_json(self.root/'migrated/packet/assignment.json')
        self.assertTrue(all('/dictionary/fields/count/0' in r['changed_paths'] for r in packet['migration']['subjects'].values()))

    def test_cpd_catalog_change_requires_review(self):
        old=copy.deepcopy(self.packet)
        old['dictionary']['cpd_catalog']['preservation']='Different prior explanation'
        old['packet_id']=wire_hash({k:v for k,v in old.items() if k!='packet_id'})
        self.store_browser(old)
        result=self.migrate()
        self.assertEqual(result['carried_confirmations'],0)
        self.assertEqual(result['requires_review'],2)

    def test_pre_catalog_packet_preserves_drafts_but_reopens_confirmation(self):
        old=copy.deepcopy(self.packet)
        del old['dictionary']['cpd_catalog']
        old['packet_id']=wire_hash({k:v for k,v in old.items() if k!='packet_id'})
        self.store_browser(old)
        result=self.migrate()
        self.assertEqual(result['carried_confirmations'],0)
        self.assertEqual(result['requires_review'],2)
        migrated=read_json(self.root/'migrated/packet/assignment.json')
        self.assertTrue(all(r['previous_form'] for r in migrated['migration']['subjects'].values()))

    def test_legacy_checkpoint_preserves_forms_but_missing_guide_requires_confirmation(self):
        bundle=export_query_review_bundle(self.lib,self.ora,reviewer_id='synthetic-migration-reviewer')
        path=self.root/'legacy.json'
        session=QueryReviewSession(bundle,library_source=self.lib,oracle_source=self.ora,split='development',checkpoint_path=path)
        qid=session.ordered_query_ids[0];session.mark_complete(qid,session.mechanical_form(qid))
        other=session.ordered_query_ids[1];form=session.form(other);form['notes']='unfinished old edit';session.save_form(other,form)
        before=path.read_bytes()
        result=self.migrate(old_submission_path=path,old_assignment_path=None,reviewer_id='synthetic-migration-reviewer')
        self.assertEqual(result['carried_confirmations'],0)
        backup=read_json(self.root/'migrated/progress.json')
        self.assertEqual(next(d for d in backup['entries'] if d['task_binding']['subject_id']==other)['form']['notes'],'unfinished old edit')
        self.assertEqual(path.read_bytes(),before)

    def test_corruption_wrong_source_and_wrong_reviewer_leave_originals_untouched(self):
        backup=self.store_browser();backup['entries'][0]['form']['notes']='changed without receipt';write_json(self.submission,rehash_backup(backup));before=self.submission.read_bytes()
        with self.assertRaises(ValidationError):self.migrate()
        self.assertEqual(self.submission.read_bytes(),before);self.assertFalse((self.root/'migrated').exists())
        self.store_browser()
        with self.assertRaisesRegex(ValidationError,'reassign'):self.migrate(reviewer_id='another-reviewer')
        bad_lib=self.root/'wrong-source.jsonl';records=copy.deepcopy(self.library);records[0]['query_text']='wrong source';write_jsonl(bad_lib,records)
        with self.assertRaisesRegex(ValidationError,'source binding'):self.migrate(old_library=bad_lib)

    def test_browser_cannot_invent_or_edit_a_carried_confirmation(self):
        self.store_browser();self.migrate()
        packet=read_json(self.root/'migrated/packet/assignment.json');backup=read_json(self.root/'migrated/progress.json')
        backup['entries'][0]['form']['notes']='forged carried edit';rehash_backup(backup)
        with self.assertRaises(ValidationError):validate_browser_submission(packet,backup,self.lib,self.ora)
        plain=browser_backup(self.packet,True);plain['entries'][0]['receipts'][0].update(action='carried_confirmation',migration_id='0'*64,origin_confirmation_sha256='0'*64);rehash_backup(plain)
        with self.assertRaisesRegex(ValidationError,'invent'):validate_browser_submission(self.packet,plain,self.lib,self.ora)

    def test_defer_keeps_edits_and_exports_a_source_locatable_issue(self):
        task=self.packet['items'][0]['task'];proposal=make_proposal(task);draft=new_draft(task,proposal,self.packet['reviewer_id'])
        draft['form']['notes']='source mismatch details'
        deferred=defer_draft(task,proposal,draft,code='source_error',reason='source support label is wrong',atom_ids=[task['oracle_draft']['atoms'][0]['atom_id']])
        self.assertEqual(deferred['status'],'requires_source_fix');self.assertEqual(deferred['form']['notes'],draft['form']['notes']);self.assertFalse(deferred['human_gold'])
        backup=self.store_browser(submitted=False);backup['entries'][0]=deferred;write_json(self.submission,rehash_backup(backup))
        result=export_issues(self.assignment,self.submission,self.lib,self.ora,self.root/'issues')
        issue=read_json(result['output'])['issues'][0]
        self.assertEqual(issue['subject_id'],task['subject_id']);self.assertEqual(issue['source_query_sha256'],task['source_query_sha256']);self.assertEqual(issue['notes'],'source mismatch details')

    def test_unsafe_editor_numbers_and_injected_fields_are_not_imported(self):
        backup=self.store_browser(submitted=False)
        for field,value in [('added_atoms_json','[{"number":9007199254740993}]'),('extra_field','injected')]:
            changed=copy.deepcopy(backup);changed['entries'][0]['form'][field]=value;rehash_backup(changed)
            with self.assertRaises(ValidationError):validate_browser_submission(self.packet,changed,self.lib,self.ora)

    def test_repeated_migration_preserves_the_original_confirmation_archive(self):
        self.store_browser();original=self.submission.read_bytes();self.migrate()
        result=self.migrate(old_assignment_path=self.root/'migrated/packet/assignment.json',old_submission_path=self.root/'migrated/progress.json',output_dir=self.root/'migrated-again')
        self.assertEqual(result['carried_confirmations'],2)
        report=read_json(self.root/'migrated-again/migration_report.json')
        digest=sha256_bytes(original)
        self.assertIn(digest,report['lineage_files'])
        self.assertEqual((self.root/'migrated-again/originals/lineage'/report['lineage_files'][digest]).read_bytes(),original)
