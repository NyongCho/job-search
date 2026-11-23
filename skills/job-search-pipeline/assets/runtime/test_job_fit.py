import json
import sqlite3
import unittest
from contextlib import closing

from job_fit import evaluate_fit, read_fit, recommendation_group


def criterion(key, category, status, *, line=1, evidence="담당 프로젝트에서 확인", **details):
    return {"key": key, "label": key, "category": category, "status": status,
            "evidence_lines": [line], "evidence": evidence, **details}


def assessment(*criteria, **details):
    return {"version": 1, "complete": True, "eligibility": "eligible",
            "preference": "preferred", "criteria": list(criteria), **details}


def evaluate(*criteria, **details):
    return evaluate_fit(assessment(*criteria, **details), line_count=8)


def posting_a():
    return assessment(criterion("제품 개발", "work", "met"),
                      criterion("협업", "required", "met", line=2),
                      criterion("배포", "required", "met", line=3),
                      criterion("관련 경력", "required", "tenure", line=4, months=24, required_months=36),
                      criterion("영어", "preferred", "unmet", line=5, evidence="사용자가 미경험 확인"))


def posting_b():
    return assessment(criterion("개발", "work", "met"), criterion("운영", "work", "unmet", line=2),
                      criterion("배포", "required", "met", line=3), criterion("영어", "preferred", "met", line=4))


def posting_c():
    return assessment(criterion("운영", "work", "met"),
                      criterion("장애 대응", "required", "unknown", line=2, evidence="경력 자료에 기재 없음"),
                      question="운영 장애에 직접 대응한 경험이 있나요?")


class JobFitTests(unittest.TestCase):
    def test_posting_a_partial_tenure_only_reduces_one_required_item(self):
        fit = evaluate_fit(posting_a(), line_count=5)
        self.assertAlmostEqual(fit["score"], 86.6666666667)
        self.assertAlmostEqual(fit["criteria"][3]["earned"], 6.6666666667)
        self.assertEqual(fit["criteria"][3]["possible"], 10)
        self.assertEqual(fit["breakdown"]["work"], {"earned": 60, "possible": 60})

    def test_posting_b_half_the_core_duties_score_70_and_preference_only_changes_group(self):
        for preference, group in (("preferred", "preferred"), ("outside", None), ("unknown", None)):
            with self.subTest(preference=preference):
                fit = evaluate_fit({**posting_b(), "preference": preference}, line_count=4)
                self.assertEqual(fit["score"], 70)
                self.assertEqual(recommendation_group("recommend", fit), group)

    def test_posting_c_unknown_incident_response_defers_score_with_question(self):
        question = "운영 장애에 직접 대응한 경험이 있나요?"
        fit = evaluate_fit(posting_c(), line_count=2)
        self.assertIsNone(fit["score"])
        self.assertIsNone(fit["breakdown"]["required"]["earned"])
        self.assertEqual(fit["possible"], 90)
        self.assertEqual(fit["question"], question)
        self.assertEqual(recommendation_group("needs_review", fit), "question")

    def test_absent_category_reweights_only_complete_source(self):
        criteria = [criterion(f"업무 {i}", "work", "met" if i < 4 else "unmet") for i in range(5)]
        criteria.append(criterion("배포", "required", "met"))
        fit = evaluate(*criteria)
        self.assertEqual(fit["earned"], 78)
        self.assertEqual(fit["possible"], 90)
        self.assertAlmostEqual(fit["score"], 86.6666666667)
        partial = evaluate(*criteria, complete=False)
        self.assertIsNone(partial["score"])
        self.assertEqual(partial["possible"], 100)
        self.assertTrue(partial["hold_reason"])
        self.assertIsNone(recommendation_group("needs_review", partial))

    def test_repeated_tenure_credits_the_required_condition_once(self):
        minimum = criterion("연차", "required", "tenure", months=24, required_months=36)
        repeat = {**minimum, "category": "preferred", "evidence_lines": [3]}
        for criteria in ([minimum, repeat], [repeat, minimum]):
            fit = evaluate(*criteria)
            self.assertEqual(fit["possible"], 30)
            self.assertEqual(fit["earned"], 20)
            self.assertEqual(fit["criteria"][0]["evidence_lines"], [1, 3])

    def test_duplicate_requirements_merge_lines_and_keep_required_weight(self):
        same_project = "개인 앱에서 직접 설계 및 배포"
        fit = evaluate(criterion("배포", "preferred", "met", line=4, evidence=same_project),
                       criterion("설계", "work", "met", evidence=same_project),
                       criterion("배포", "required", "met", line=3, evidence=same_project),
                       criterion("운영", "required", "unmet"))
        self.assertEqual(fit["possible"], 90)
        self.assertAlmostEqual(fit["score"], 83.3333333333)
        self.assertEqual(len(fit["criteria"]), 3)
        deploy = next(c for c in fit["criteria"] if c["key"] == "배포")
        self.assertEqual(deploy["category"], "required")
        self.assertEqual(deploy["evidence_lines"], [3, 4])
        self.assertEqual(fit["breakdown"]["preferred"], {"earned": 0, "possible": 0})

    def test_conflicting_status_or_months_for_same_fact_are_rejected(self):
        first = criterion("경력", "required", "tenure", months=12, required_months=24)
        for second in [criterion("경력", "preferred", "unmet"),
                       {**first, "months": 8}, {**first, "required_months": 48}]:
            with self.subTest(second=second), self.assertRaises(ValueError):
                evaluate(first, second)

    def test_zero_is_scored_and_partial_and_capped_tenure_have_credit(self):
        zero = evaluate(criterion("운영", "work", "unmet"))
        self.assertEqual(zero["score"], 0)
        self.assertEqual(recommendation_group("recommend", zero), "preferred")
        fit = evaluate(criterion("운영", "work", "partial", evidence="교대 때 일부 대응"),
                       criterion("경력", "required", "tenure", months=48, required_months=36))
        self.assertAlmostEqual(fit["score"], 66.6666666667)
        self.assertIsNone(evaluate()["score"])

    def test_unconfirmed_optional_experience_does_not_become_zero_or_shrink_denominator(self):
        fit = evaluate(criterion("운영", "work", "met"),
                       criterion("영어", "preferred", "unknown", evidence=""))
        self.assertIsNone(fit["score"])
        self.assertEqual(fit["possible"], 70)
        self.assertEqual(fit["eligibility"], "eligible")
        self.assertTrue(fit["hold_reason"])

    def test_cutoff_uses_raw_score_even_when_both_values_display_as_80(self):
        for months, group in [(7999, None), (8000, "outside")]:
            fit = evaluate(criterion("경력", "required", "tenure", months=months, required_months=10000),
                           preference="outside")
            self.assertEqual(f'{fit["score"]:.1f}', "80.0")
            self.assertEqual(recommendation_group("recommend", fit), group)

    def test_hard_ineligibility_and_decision_override_question_and_score(self):
        question = "출근 가능하신가요?"
        blocked = evaluate(criterion("운영", "required", "unknown", evidence="기재 없음"),
                           eligibility="ineligible", question=question)
        for decision in ("recommend", "needs_review", "exclude"):
            self.assertIsNone(recommendation_group(decision, blocked))
        good = evaluate(criterion("운영", "work", "met"))
        self.assertIsNone(recommendation_group("exclude", good))
        self.assertIsNone(recommendation_group("needs_review", good))
        self.assertIsNone(recommendation_group("needs_review", {**good, "question": "원문 링크가 있나요?"}))
        pending = evaluate(criterion("운영", "work", "met"), eligibility="unknown", question=question)
        self.assertIsNone(recommendation_group("exclude", pending))
        self.assertEqual(recommendation_group("needs_review", pending), "question")

    def test_only_open_postings_and_complete_user_questions_are_deliverable(self):
        perfect = evaluate(criterion("개발", "work", "met"))
        for availability in ("closed", "unknown"):
            self.assertIsNone(recommendation_group("recommend", {**perfect, "availability": availability}))
            self.assertIsNone(recommendation_group("needs_review", {**perfect, "availability": availability,
                                                                    "question": "경험이 있나요?"}))
        incomplete = evaluate(criterion("개발", "work", "met"), complete=False, question="원문이 있나요?")
        self.assertIsNone(recommendation_group("needs_review", incomplete))

    def test_unknown_required_experience_requires_one_concrete_question(self):
        fact = criterion("장애 대응", "required", "unknown", evidence="자료에 내용 없음")
        for question in (None, "", "   ", "장애 대응 경험이 있나요?\n기간은요?"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                evaluate(fact, question=question)
        with self.assertRaises(ValueError):
            evaluate(fact, eligibility="unknown")
        self.assertIsNone(evaluate(fact, eligibility="ineligible")["score"])
        self.assertIsNone(evaluate(fact, complete=False)["score"])

    def test_rejects_invalid_schema_and_unsupported_evidence(self):
        valid = criterion("설계", "required", "met")
        for override in ({"version": True}, {"version": 2}, {"complete": 1},
                         {"preference": "yes"}, {"eligibility": "maybe"}, {"criteria": {}}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                evaluate_fit(assessment(valid, **override), line_count=8)
        for change in ({"evidence_lines": []}, {"evidence_lines": [True]}, {"evidence_lines": [0]},
                       {"evidence_lines": [9]}, {"evidence": ""}, {"status": "unmet", "evidence": " "},
                       {"key": ""}, {"label": ""}, {"status": "yes"}, {"category": "bonus"},
                       {"status": "tenure", "category": "work", "months": 1, "required_months": 2},
                       {"status": "tenure", "category": "preferred", "months": 1, "required_months": 2},
                       {"months": 1}, {"status": "tenure", "months": -1, "required_months": 1},
                       {"status": "tenure", "months": 1, "required_months": 0},
                       {"status": "tenure", "months": True, "required_months": 3}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluate({**valid, **change})

    def test_unrelated_raw_profile_data_is_not_saved(self):
        fit = evaluate({**criterion("설계", "work", "met"), "resume": "private raw text"}, memory="private raw text")
        self.assertNotIn("private raw text", json.dumps(fit))


class ReadFitTests(unittest.TestCase):
    def test_reads_current_fit_for_mapping_or_sqlite_row(self):
        fit = {**evaluate(criterion("설계", "work", "met")), "fingerprint": "jd1", "reviewed_at": "time1"}
        row = {"fit_review": json.dumps(fit), "fingerprint": "jd1", "reviewed_at": "time1"}
        self.assertEqual(read_fit(row), fit)
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            sql_row = db.execute("SELECT :fit_review AS fit_review,:fingerprint AS fingerprint,"
                                 ":reviewed_at AS reviewed_at", row).fetchone()
            self.assertEqual(read_fit(sql_row), fit)

    def test_older_row_stale_stamp_and_malformed_json_have_no_persisted_fit(self):
        fit = {**evaluate(criterion("설계", "work", "met")), "fingerprint": "jd1", "reviewed_at": "time1"}
        row = {"fit_review": json.dumps(fit), "fingerprint": "jd1", "reviewed_at": "time1"}
        for override in ({"fingerprint": "jd2"}, {"reviewed_at": "time2"}, {"fit_review": None},
                         {"fit_review": "{"}, {"fit_review": "[]"}, {"fit_review": "{}"}):
            with self.subTest(override=override):
                self.assertIsNone(read_fit({**row, **override}))
        for override in ({"score": "100"}, {"score": float("nan")}, {"version": 2},
                         {"criteria": ["malformed"]}, {"breakdown": {"work": []}}):
            with self.subTest(override=override):
                self.assertIsNone(read_fit({**row, "fit_review": json.dumps({**fit, **override})}))
        self.assertIsNone(read_fit({"fingerprint": "jd1", "reviewed_at": None}))


if __name__ == "__main__":
    unittest.main()
