import copy
import json
import tempfile
import unittest
from pathlib import Path

from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import read_json, read_jsonl, write_json
from bus_benchmark.review_packet import build_packet, browser_snapshot, export_packet, import_packet, validate_browser_submission, finalize_packet
from bus_benchmark.review_wire import wire_hash

ROOT = Path(__file__).resolve().parents[2]


def browser_backup(packet, submitted=False):
    entries = [copy.deepcopy(i['initial_draft']) for i in packet['items']]
    if submitted:
        for entry, item in zip(entries, packet['items']):
            entry['receipts'] = [{'action':'explicit_confirm', 'content_sha256':wire_hash(browser_snapshot(packet['packet_id'], item['task'], item['proposal'], entry, item.get('revision_proposals'), item.get('surface_diff'))), 'covered_units':['atom:'+a['atom_id'] for a in item['task']['oracle_draft']['atoms']]+['support','cpd','additions','notes'], 'reviewer_id':packet['reviewer_id'], 'revision':entry['revision']}]
            entry['status'] = 'submitted'
    value = dict(artifact_type='browser_review_backup', packet_version=packet['packet_version'],packet_id=packet['packet_id'],reviewer_id=packet['reviewer_id'],generation=1,entries=entries,human_gold=False)
    return {**value,'backup_sha256':wire_hash(value)}


def rehash_backup(value):
    value['backup_sha256'] = wire_hash({k:v for k,v in value.items() if k!='backup_sha256'})
    return value


class OfflinePacketTests(unittest.TestCase):
    def setUp(self):
        self.library = read_jsonl(ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl')[:2]
        self.oracle = read_jsonl(ROOT/'benchmark_artifacts/drafts/dev_oracle_draft.jsonl')[:2]
        self.packet = build_packet(self.library, self.oracle, 'synthetic-browser-reviewer')

    def test_drafts_and_browser_submissions_are_not_gold(self):
        for submitted in (False, True):
            result = validate_browser_submission(self.packet, browser_backup(self.packet, submitted), self.library, self.oracle)
            self.assertFalse(result['human_gold'])
            self.assertEqual(result['subjects_submitted'], 2 if submitted else 0)

    def test_rehashed_source_and_reviewer_forgeries_fail(self):
        for mutate in (
            lambda x:x['items'][0]['task'].__setitem__('query_text','forged source'),
            lambda x:x['dictionary']['tokens'].__setitem__('bicycle','forged meaning'),
        ):
            packet = copy.deepcopy(self.packet);mutate(packet)
            packet['packet_id'] = wire_hash({k:v for k,v in packet.items() if k!='packet_id'})
            with self.assertRaisesRegex(ValidationError,'trusted current sources'):
                validate_browser_submission(packet, browser_backup(packet,True),self.library,self.oracle)
        backup = browser_backup(self.packet,True)
        backup['entries'][0]['reviewer_id'] = 'other'
        with self.assertRaisesRegex(ValidationError,'reviewer'):
            validate_browser_submission(self.packet,rehash_backup(backup),self.library,self.oracle)

    def test_stale_receipts_missing_subjects_and_unknown_fields_fail(self):
        for mutate in (
            lambda x:x['entries'][0]['form'].__setitem__('notes','edited after confirmation'),
            lambda x:x['entries'].pop(),
            lambda x:x.__setitem__('extra_field','injected'),
            lambda x:x['entries'][0].__setitem__('receipts',[]),
        ):
            value=browser_backup(self.packet,True);mutate(value);rehash_backup(value)
            with self.assertRaises(ValidationError):validate_browser_submission(self.packet,value,self.library,self.oracle)

    def test_export_escapes_scripts_and_has_no_remote_assets(self):
        lib=copy.deepcopy(self.library)
        lib[0]['query_text'] += ' </script><img src=x onerror="window.attacked=true"> & 中文'
        with tempfile.TemporaryDirectory() as root:
            result=export_packet(lib,self.oracle,'synthetic-browser-reviewer',Path(root)/'packet')
            html=Path(result['html']).read_text()
            self.assertNotIn('</script><img',html)
            self.assertIn('\\u003c/script>',html)
            self.assertIn("connect-src 'none'",html)
            self.assertNotIn('<script src=',html)
            self.assertNotIn('cdn.jsdelivr',html)
            with self.assertRaisesRegex(ValidationError,'already exists'):export_packet(lib,self.oracle,'synthetic-browser-reviewer',Path(root)/'packet')

    def test_import_is_immutable_and_finalizer_requires_all_subjects(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);assignment=root/'assignment.json';submission=root/'backup.json'
            write_json(assignment,self.packet);backup=browser_backup(self.packet,False);write_json(submission,backup)
            result=import_packet(assignment,submission,self.library,self.oracle,root/'imported')
            self.assertFalse(result['human_gold'])
            with self.assertRaisesRegex(ValidationError,'already exists'):import_packet(assignment,submission,self.library,self.oracle,root/'imported')
            with self.assertRaisesRegex(ValidationError,'all subjects'):finalize_packet(assignment,submission,self.library,self.oracle,root/'gold')
            self.assertFalse((root/'gold').exists())
            write_json(submission,browser_backup(self.packet,True))
            result=finalize_packet(assignment,submission,self.library,self.oracle,root/'synthetic-test-finalizer')
            self.assertEqual(result['record_count'],2)

    def test_wire_numbers_and_typed_objects_do_not_collide(self):
        self.assertEqual(wire_hash(8),wire_hash(8.0))
        self.assertEqual(wire_hash(-0.0),wire_hash(0))
        self.assertNotEqual(wire_hash(1),wire_hash(True))
        self.assertNotEqual(wire_hash(1),wire_hash(['num','3ff0000000000000']))
        with self.assertRaises(ValidationError):wire_hash(9007199254740992)
        with self.assertRaises(ValidationError):wire_hash(float('nan'))
