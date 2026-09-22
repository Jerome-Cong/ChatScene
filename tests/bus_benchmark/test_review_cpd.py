import copy
import unittest

from bus_benchmark.cpd import QUERY_BLIND_TARGET_SELECTORS
from bus_benchmark.review_cpd import policy_catalog
from bus_benchmark.review_forms import response_from_form
import test_review_packet


class CPDCatalogTests(unittest.TestCase):
    def test_catalog_uses_frozen_selectors_without_mutating_them(self):
        before = copy.deepcopy(QUERY_BLIND_TARGET_SELECTORS)
        catalog = policy_catalog()
        for name, selector in before.items():
            self.assertEqual(catalog['dimensions'][name]['target_selector'], selector)
        catalog['dimensions']['merge_gap_relation']['target_selector']['ranking'].clear()
        self.assertEqual(QUERY_BLIND_TARGET_SELECTORS, before)
        self.assertEqual(policy_catalog()['dimensions']['actor_longitudinal_distance_bin']['bin_definition_m'], {'near_max':10.0,'medium_max':30.0})

    def test_catalog_does_not_replace_per_query_policy_or_compiler(self):
        fixture = test_review_packet.OfflinePacketTests();fixture.setUp()
        first, second = fixture.packet['items']
        self.assertNotEqual(first['task']['oracle_draft']['cpd_policy'], second['task']['oracle_draft']['cpd_policy'])
        self.assertIn('cpd_catalog', fixture.packet['dictionary'])
        for item in fixture.packet['items']:
            response = response_from_form(item['task'], item['initial_draft']['form'])
            self.assertEqual(response['cpd_decision']['verdict'], response['required_check_decisions']['cpd_common_eligibility']['verdict'])
            self.assertEqual(response['proposed_oracle']['cpd_policy'], item['task']['oracle_draft']['cpd_policy'])
