import unittest

from job_search.fit_benchmark import audit, compare, example


class BenchmarkTests(unittest.TestCase):
    def test_same_score_still_detects_wrong_required_evidence(self):
        sample = example()
        first, other = sample["jobs"][:2]
        entries = [{"decision": "recommend", "id": first["id"], "version": first["version"], "assessment": {
            "version":1, "complete":True, "preference":"preferred", "eligibility":"eligible",
            "criteria":[{"key":"work", "label":"업무", "category":"work", "status":"met",
                         "evidence_lines":[1], "quotes":[other["lines"][1]], "profile_ids":["P1"],
                         "evidence":"제공된 경력"}]}}]
        row, = audit([first], entries, sample["facts"])
        self.assertEqual(row["missing_required_lines"], list(first["required"]))
        self.assertIn("JD 인용", row["errors"])
        differing = dict(row, score=80.0)
        self.assertTrue(compare({first["id"]: dict(row, score=79.99)}, {first["id"]: differing})[0]["crosses_80"])
