import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bus_benchmark.atoms import make_atom
from bus_benchmark.errors import ValidationError
from bus_benchmark.human_workflow import export_query_review_bundle
from bus_benchmark.jsonio import read_json,read_jsonl,canonical_json_bytes
from bus_benchmark.review_forms import _atom_batch_id
from bus_benchmark.review_session import QueryReviewSession
from bus_benchmark.surface_diff import surface_diff,inheritance_assessment

ROOT=Path(__file__).resolve().parents[2]


class SurfaceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.library=read_jsonl(ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl')[:3]
        self.oracle=read_jsonl(ROOT/'benchmark_artifacts/drafts/dev_oracle_draft.jsonl')[:3]

    def session(self, identical=False):
        library,oracle=copy.deepcopy(self.library),copy.deepcopy(self.oracle)
        if identical:
            for row,record in zip(library,oracle):
                row['query_text']=library[0]['query_text']
                record['atoms']=copy.deepcopy(oracle[0]['atoms'])
                for atom in record['atoms']:atom['provenance']['query_id']=row['query_id']
        bundle=export_query_review_bundle(library,oracle,reviewer_id='synthetic-surface-reviewer')
        return QueryReviewSession(bundle,library_source=library,oracle_source=oracle,split='development',checkpoint_path=self.root/('same.json' if identical else 'review.json'))

    def test_all_lexical_dimensions_are_visible_and_unknown_changes_require_review(self):
        session=self.session();source=copy.deepcopy(session.task(session.ordered_query_ids[0]));target=copy.deepcopy(source)
        source['query_text']='One cyclist is 8 m ahead before the car moves, if the bus turns slowly.'
        target['query_text']='Two pedestrians are not 3 m behind after the car moves when the bus turns suddenly.'
        diff=surface_diff(source,target)
        self.assertTrue(set(('cardinality','negation','roles','relations','order','triggers','modifiers','numbers_units'))<=set(diff['categories']))
        self.assertTrue(diff['full_review_required'])
        self.assertFalse(inheritance_assessment(source,target)['can_inherit'])
        target['query_text']=source['query_text']+' unexplained qualifier'
        self.assertIn('unclassified',surface_diff(source,target)['categories'])

    def test_precise_added_requirements_and_same_ids_do_not_leak_into_other_surfaces(self):
        session=self.session();precise,partial,vague=session.ordered_query_ids
        form=session.mechanical_form(precise)
        added=make_atom('road','lane_configuration',{'feature':'synthetic_precise_only','value':8},layer='surface_required')
        form['added_atoms_json']=json.dumps([added])
        partial_form=session.form(partial);partial_form['notes']='existing exception';session.save_form(partial,partial_form)
        self.assertEqual(session.mark_complete_and_seed_siblings(precise,form),[])
        self.assertEqual(session.form(partial),partial_form)
        self.assertEqual(session.form(vague)['added_atoms_json'],'[]')
        self.assertFalse(session.can_seed_from_precise(partial))
        self.assertEqual(session.progress()['human_gold_records'],0)
        self.assertTrue(set(a['atom_id'] for a in session.task(precise)['oracle_draft']['atoms']) & set(a['atom_id'] for a in session.task(partial)['oracle_draft']['atoms']))

    def test_exact_text_inheritance_records_target_basis_but_never_confirms_target(self):
        session=self.session(identical=True);precise,partial,vague=session.ordered_query_ids
        form=session.mechanical_form(precise)
        form['added_atoms_json']=json.dumps([make_atom('road','lane_configuration',{'feature':'synthetic_source_addition','value':True},layer='surface_required')])
        seeded=session.mark_complete_and_seed_siblings(precise,form)
        self.assertEqual(seeded,[partial,vague])
        self.assertEqual(session.form(partial)['added_atoms_json'],form['added_atoms_json'])
        self.assertEqual(session.form(partial)['cpd_decision']['verdict'],'')
        self.assertFalse(session._entry(partial)['human_confirmed'])
        history=list(Path(str(session.checkpoint_path)+'.batch-history').glob('*/prepared.json'))
        self.assertEqual(len(history),1)
        action=read_json(history[0]);self.assertEqual(action['kind'],'inheritance')
        self.assertTrue(all(a['target_applicability'] for a in action['target_applicability']))
        session.undo_batch(action['action_id'])
        self.assertTrue(session._entry(precise)['human_confirmed'])
        self.assertFalse(session._form_has_content(session.form(partial)))

    def test_batch_undo_preserves_later_edits_and_confirmations(self):
        session=self.session();batch=next(b for b in session.atom_review_batches() if len(b['instances'])>=2)
        session.apply_atom_batch_decision(batch['batch_id'],'accept')
        action_id=session.last_batch_action_id
        edited=batch['instances'][0]['subject_id'];form=session.form(edited);form['notes']='later human edit';session.save_form(edited,form)
        result=session.undo_batch(action_id)
        self.assertIn(edited,result['skipped_edited_or_confirmed'])
        self.assertEqual(session.form(edited),form)
        self.assertTrue(result['restored'])
        self.assertFalse(result['human_gold'])
        session.apply_atom_batch_decision(batch['batch_id'],'accept')
        action_id=session.last_batch_action_id
        confirmed=batch['instances'][1]['subject_id'];session.mark_complete(confirmed,session.mechanical_form(confirmed))
        result=session.undo_batch(action_id)
        self.assertIn(confirmed,result['skipped_edited_or_confirmed'])
        self.assertTrue(session._entry(confirmed)['human_confirmed'])

    def test_batch_cas_failure_leaves_only_preparation_and_no_undo_target(self):
        session=self.session();batch=session.atom_review_batches()[0]
        before=canonical_json_bytes(read_json(session.checkpoint_path))
        with patch.object(session,'_persist',side_effect=ValidationError('synthetic CAS failure')):
            with self.assertRaises(ValidationError):session.apply_atom_batch_decision(batch['batch_id'],'accept')
        self.assertEqual(canonical_json_bytes(read_json(session.checkpoint_path)),before)
        prepared=list(Path(str(session.checkpoint_path)+'.batch-history').glob('*/prepared.json'))
        self.assertEqual(len(prepared),1)
        with self.assertRaises(ValidationError):session.undo_batch(prepared[0].parent.name)

    def test_atom_and_cpd_batches_never_cross_dataset_splits(self):
        test_library=read_jsonl(ROOT/'query_lib/bus_ego_topdown_2d_query_library_v0_2.jsonl')
        test_oracles=read_jsonl(ROOT/'benchmark_artifacts/drafts/test_oracle_draft.jsonl')
        keys={_atom_batch_id(a) for a in self.oracle[0]['atoms']}
        other=next(r for r in test_oracles if any(_atom_batch_id(a) in keys for a in r['atoms']))
        library=[self.library[0],next(r for r in test_library if r['query_id']==other['query_id'])];oracle=[self.oracle[0],other]
        bundle=export_query_review_bundle(library,oracle,reviewer_id='synthetic-mixed-reviewer')
        session=QueryReviewSession(bundle,library_source=library,oracle_source=oracle,split='mixed-manifest',checkpoint_path=self.root/'mixed.json')
        for batch in session.atom_review_batches()+session.cpd_review_batches():
            splits={session.task(i['subject_id'])['query_record']['dataset_split'] for i in batch['instances']}
            self.assertEqual(len(splits),1)
