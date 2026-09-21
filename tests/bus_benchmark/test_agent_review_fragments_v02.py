import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_agent_review_fragments_v0_2.py"
FRAGMENT_DIR = ROOT / "benchmark_artifacts" / "drafts" / "agent_review_fragments"
FILES = (
    "dev_intents.json",
    "test_A_C_intents.json",
    "test_D_G_intents.json",
)


class AgentReviewFragmentsV02Test(unittest.TestCase):
    def test_checked_in_fragments_are_reproducible(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {"fragments": 3, "intents": 100, "status": "verified"},
        )

    def test_fragments_cover_v02_only_and_never_claim_human_gold(self):
        fragments = [
            json.loads((FRAGMENT_DIR / name).read_text(encoding="utf-8"))
            for name in FILES
        ]
        reviews = [
            review
            for fragment in fragments
            for review in fragment["intent_reviews"]
        ]
        self.assertEqual([len(value["intent_reviews"]) for value in fragments], [16, 40, 44])
        self.assertEqual(len(reviews), 100)
        self.assertEqual(len({review["intent_group_id"] for review in reviews}), 100)
        self.assertTrue(all(fragment["agent_generated"] for fragment in fragments))
        self.assertTrue(all(fragment["human_gold"] is False for fragment in fragments))
        serialized = json.dumps(fragments, ensure_ascii=False)
        self.assertNotIn("_v0_1_", serialized)
        self.assertNotIn("query_library_v0_1", serialized)

    def test_event_findings_keep_ego_reaction_out_of_the_oracle(self):
        for name in FILES:
            fragment = json.loads((FRAGMENT_DIR / name).read_text(encoding="utf-8"))
            for review in fragment["intent_reviews"]:
                event_findings = [
                    finding
                    for finding in review["semantic_findings"]
                    if finding["target_type"] == "required_check"
                    and finding["target"] == "event_graph_and_actor_binding"
                ]
                self.assertEqual(len(event_findings), 1)
                finding = event_findings[0]
                self.assertEqual(finding["recommendation"], "human_judgment")
                self.assertIn("ego policy", finding["suggested_change"])


if __name__ == "__main__":
    unittest.main()
