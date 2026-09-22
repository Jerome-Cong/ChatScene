import copy
import unittest
from pathlib import Path

from bus_benchmark.atoms import make_atom
from bus_benchmark.errors import ValidationError
from bus_benchmark.human_workflow import export_query_review_bundle
from bus_benchmark.jsonio import read_json,read_jsonl
from bus_benchmark.review_model import content_hash
from bus_benchmark.review_proposals import build_revision_proposals,validate_revision_proposals,proposal_coverage

ROOT=Path(__file__).resolve().parents[2]


def task_for(text, atoms):
    library=read_jsonl(ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl')[:1]
    oracle=read_jsonl(ROOT/'benchmark_artifacts/drafts/dev_oracle_draft.jsonl')[:1]
    library[0]['query_text']=text
    atoms=copy.deepcopy(atoms)
    for atom in atoms:atom['provenance']={'query_id':library[0]['query_id'],'source':'query_library_metadata','field':'synthetic_development_fixture'}
    oracle[0]['atoms']=atoms
    return export_query_review_bundle(library,oracle,reviewer_id='synthetic-proposal-reviewer')['reviewer_packet']['tasks'][0]


class ConcreteProposalTests(unittest.TestCase):
    def actor(self,count='1',role='cyclist'):
        return make_atom('actor','actor_role_count',{'type':'bicycle','role':role,'count':count})

    def test_cardinality_optional_and_absence_produce_concrete_revisions(self):
        for text,count,layer,polarity in [('Two cyclists cross.','2','core_required','present'),('There may be a cyclist nearby.','optional','permitted','present'),('No cyclist is present.','0','forbidden','absent')]:
            task=task_for(text,[self.actor()]);before=copy.deepcopy(task);report=build_revision_proposals(task);item=report['items'][0]
            self.assertEqual(item['status'],'proposed');self.assertEqual(item['operation'],'modify')
            replacement=item['replacement_atoms'][0]
            self.assertEqual(replacement['provenance']['source'],'query_text_regex')
            self.assertEqual(replacement['arguments']['count'],count);self.assertEqual(replacement['layer'],layer);self.assertEqual(replacement['polarity'],polarity)
            self.assertEqual(task,before);self.assertFalse(report['human_gold'])

    def test_spatial_proposal_uses_text_not_role_name(self):
        spatial=make_atom('spatial','actor_relative_region',{'role':'cyclist','relation':'behind_ego'})
        report=build_revision_proposals(task_for('One cyclist is in front of the bus.',[self.actor(),spatial]))
        self.assertEqual(report['items'][1]['replacement_atoms'][0]['arguments']['relation'],'ahead_of_ego')
        report=build_revision_proposals(task_for('One cyclist watches a car in front of the bus.',[self.actor(),spatial]))
        self.assertEqual(report['items'][1]['status'],'unresolved')
        report=build_revision_proposals(task_for('One cyclist is nearby.',[self.actor(),spatial]))
        self.assertEqual(report['items'][1]['status'],'unresolved')

    def test_explicit_parallel_order_and_trigger_are_structured(self):
        edge=make_atom('temporal','before',{'first':'pedestrian_starts_crossing','second':'following_vehicle_deceleration'})
        task=task_for('The pedestrian starts crossing while the following vehicle slows down.',[edge])
        item=build_revision_proposals(task)['items'][0]
        self.assertEqual(item['replacement_atoms'][0]['predicate'],'parallel_group')
        task=task_for('The pedestrian starts crossing after the following vehicle slows down.',[edge])
        item=build_revision_proposals(task)['items'][0]
        self.assertEqual(item['replacement_atoms'][0]['arguments']['first'],'following_vehicle_deceleration')
        event=make_atom('event','event_spec',{'event':'following_vehicle_deceleration'})
        task=task_for('When the pedestrian starts crossing, the following vehicle slows down.',[event])
        item=build_revision_proposals(task)['items'][0]
        self.assertEqual(item['semantic_kind'],'triggered_event')
        self.assertEqual(item['replacement_atoms'][0]['arguments']['trigger'],'the pedestrian starts crossing')

    def test_missing_evidence_negation_ambiguity_and_metadata_remain_unresolved(self):
        for text in ('Not a cyclist but a pedestrian crosses.','If a cyclist appears, the bus may stop.','When a cyclist appears, the bus stops.','A cyclist follows another cyclist.','At least two cyclists cross.','At most two cyclists cross.','There may be no cyclist nearby.','No cyclist is required.'):
            self.assertEqual(build_revision_proposals(task_for(text,[self.actor()]))['items'][0]['status'],'unresolved')
        risk=make_atom('normative','risk_level',{'value':'high'})
        for text in ('A cyclist crosses.','This is not a high-risk case.'):
            item=build_revision_proposals(task_for(text,[risk]))['items'][0]
            self.assertEqual(item['status'],'unresolved');self.assertEqual(item['replacement_atoms'],[])
        state=make_atom('event','event_spec',{'event':'vehicle_queue_present'})
        self.assertEqual(build_revision_proposals(task_for('Vehicles are waiting.',[state]))['items'][0]['semantic_kind'],'state')

    def test_unknown_conditions_cannot_be_erased_by_a_proposed_revision(self):
        edge=make_atom('temporal','before',{'first':'pedestrian_starts_crossing','second':'following_vehicle_deceleration','extra_condition':'only during rain'})
        task=task_for('The pedestrian starts crossing after the following vehicle slows down.',[edge])
        item=build_revision_proposals(task)['items'][0]
        self.assertEqual(item['status'],'unresolved')
        self.assertEqual(item['reason_code'],'unknown_source_semantics')
        self.assertEqual(item['replacement_atoms'],[])

    def test_literal_event_actor_binding_is_concrete_without_inventing_action(self):
        event=make_atom('event','event_spec',{'event':'cyclist_straight_crossing'})
        item=build_revision_proposals(task_for('One cyclist continues straight through the crossing.',[self.actor(),event]))['items'][1]
        self.assertEqual(item['status'],'proposed')
        self.assertEqual(item['replacement_atoms'][0]['arguments'],{'event':'cyclist_straight_crossing','actor':'cyclist'})
        item=build_revision_proposals(task_for('One cyclist watches the bus that continues straight through the crossing.',[self.actor(),event]))['items'][1]
        self.assertEqual(item['status'],'unresolved')
        item=build_revision_proposals(task_for('One through cyclist at the cycle crossing is waiting.',[self.actor(),event]))['items'][1]
        self.assertEqual(item['status'],'unresolved')
        motorcycle=make_atom('actor','actor_role_count',{'type':'motorcycle','role':'oncoming_motorcycle','count':'1'})
        event=make_atom('event','event_spec',{'event':'oncoming_motorcycle_through'})
        item=build_revision_proposals(task_for('One motorcycle coming the other way is now waiting.',[motorcycle,event]))['items'][1]
        self.assertEqual(item['status'],'unresolved')

    def test_report_validator_rejects_omitted_sources_and_forged_quotes(self):
        task=task_for('Two cyclists cross.',[self.actor()]);report=build_revision_proposals(task)
        for modify in (lambda r:r['items'].clear(), lambda r:r['items'][0]['evidence'][0].__setitem__('quote','invented'),lambda r:r['items'][0].__setitem__('evidence',[])):
            changed=copy.deepcopy(report);modify(changed);changed['report_id']=content_hash({k:v for k,v in changed.items() if k!='report_id'})
            with self.assertRaises(ValidationError):validate_revision_proposals(task,changed)

    def test_existing_agent_advisory_is_bound_and_never_becomes_text_evidence(self):
        library=read_jsonl(ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl')[:1]
        oracle=read_jsonl(ROOT/'benchmark_artifacts/drafts/dev_oracle_draft.jsonl')[:1]
        task=export_query_review_bundle(library,oracle,reviewer_id='synthetic')['reviewer_packet']['tasks'][0]
        agent=next(r for r in read_json(ROOT/'benchmark_artifacts/drafts/query_agent_semantic_review_v0_2.json')['reviews'] if r['query_id']==task['subject_id'])
        report=build_revision_proposals(task,agent)
        self.assertTrue(all('legacy_advisory' in item for item in report['items']))
        self.assertEqual(proposal_coverage([report])['atoms'],len(task['oracle_draft']['atoms']))
        agent['source_query_sha256']='0'*64
        with self.assertRaises(ValidationError):build_revision_proposals(task,agent)
