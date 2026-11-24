import io
import os
import subprocess
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from job_search import poster_ocr


def poster(size=(1000, 1200)):
    data = io.BytesIO()
    Image.new("RGB", size, "white").save(data, "PNG")
    return data.getvalue()


def tsv(lines):
    result = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
    for block, (x, y, text, conf) in enumerate(lines, 1):
        result.append(f"5\t1\t{block}\t1\t1\t1\t{x}\t{y}\t250\t22\t{conf}\t{text}")
    return "\n".join(result).encode()


# Coordinates in the TSV are pixels in the grayscale image passed to the CLI.
CLEAR = [(35, 50, "서버 엔지니어 모집", 97), (35, 120, "주요 업무", 95),
         (35, 160, "결제 서비스의 백엔드 시스템 설계와 개발", 93), (35, 225, "자격 요건", 97),
         (35, 285, "분산 환경에서 안정적으로 서비스를 운영한 경험", 95)]


def run_engine(img, *outputs, languages=b"eng\nkor\n"):
    responses = [subprocess.CompletedProcess([], 0, result, b"") for result in
                 (languages, *(tsv(rows) for rows in outputs))]
    with patch("subprocess.run", side_effect=responses):
        return poster_ocr.read_poster(img)


class PosterOcrTests(unittest.TestCase):
    def test_post_url_with_no_alt_is_a_candidate_but_logos_and_trackers_are_not(self):
        self.assertTrue(poster_ocr.is_candidate({"url": "https://i0.wp.com/jobs/post.jpg?resize=958,4921", "alt": ""}))
        for path, alt in [("brand-logo-v2.webp", ""), ("12345.png", "기업 로고"),
                          ("icons/share.svg", ""), ("tracking/pixel", ""), ("mark.png", "공유하기 버튼")]:
            self.assertFalse(poster_ocr.is_candidate({"url": "https://example.com/" + path, "alt": alt}))
        self.assertFalse(poster_ocr.is_candidate({"url": "data:image/png;base64,..."}))
        self.assertFalse(poster_ocr.is_candidate({"url": "https://example.com/123.png", "width": 64, "height": 64}))
        self.assertTrue(poster_ocr.is_candidate({"url": "https://example.com/123.png", "width": 90, "height": 850}))

    def test_invalid_or_decorative_raster_is_rejected_before_running_ocr(self):
        with patch("subprocess.run", side_effect=AssertionError("unexpected OCR")):
            self.assertEqual(poster_ocr.read_poster(b"not a raster")["reason"], "invalid_image")
            self.assertEqual(poster_ocr.read_poster(poster((90, 90)))["reason"], "decorative_image")

    def test_missing_cli_and_partial_language_install_return_actionable_reasons(self):
        img = poster()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(poster_ocr.read_poster(img), {"status": "failed", "reason": "tesseract_unavailable", "lines": []})
        self.assertEqual(run_engine(img, languages=b"eng\n")["reason"], "language_unavailable")

    def test_reliable_single_column_retains_original_order_and_coordinates(self):
        result = run_engine(poster((1600, 1200)), CLEAR)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["lines"][0], {"text": "서버 엔지니어 모집", "bbox": [35, 50, 250, 22]})
        self.assertEqual(result["lines"][-1]["text"], CLEAR[-1][2])

    def test_table_cells_keep_their_coordinates_and_never_become_confirmed_requirements(self):
        # Two columns of unrelated duties and qualifications aligned on three rows.
        table = list(CLEAR) + [(695, 50, "지원자격", 94), (695, 120, "학사 이상", 96), (695, 160, "서울", 95)]
        result = run_engine(poster((1600, 1200)), table)
        self.assertEqual((result["status"], result["reason"]), ("uncertain", "ambiguous_layout"))
        self.assertIn({"text": "학사 이상", "bbox": [695, 120, 250, 22]}, result["lines"])

    def test_text_between_two_fragments_blocks_a_false_table_gutter(self):
        rows = []
        for y in (120, 180):
            rows.extend([(35, y, "지원자격", 96), (316, y, "흐린 글자", 35),
                         (650, y, "업무 경험", 95)])
        rows.append((35, 300, "필수 경력 5년", 70))
        result = run_engine(poster((1600, 1200)), rows)
        self.assertEqual((result["status"], result["reason"]), ("uncertain", "uncertain_number"))

    def test_unrelated_horizontal_gaps_on_different_rows_do_not_establish_columns(self):
        rows = [(35, 100, "담당업무", 96), (650, 100, "서비스 개발", 95),
                (680, 175, "채용절차", 97), (1250, 175, "서류전형", 93),
                (35, 300, "지원자격 전산 경력 3년", 65)]
        result = run_engine(poster((1600, 1200)), rows)
        self.assertEqual((result["status"], result["reason"]), ("uncertain", "uncertain_number"))

    def test_disagreeing_original_crop_never_confirms_a_requirement(self):
        result = run_engine(poster(), CLEAR + [(35, 340, "관련 경력 3년 이상 필수", 94)],
                            [(0, 0, "관련 경력 8년 이상 필수", 97)])
        self.assertEqual((result["status"], result["reason"]), ("uncertain", "uncertain_number"))

    def test_long_poster_keeps_original_coordinates_and_deduplicates_overlap(self):
        # The first two tiles see the same line, and the last contains new text.
        first = list(CLEAR) + [(50, 1940, "지원 안내", 96)]
        second = [(50, 140, "지원 안내", 96), (50, 500, "분산 데이터베이스 구축 경험 우대", 95)]
        result = run_engine(poster((1600, 3100)), first, second)
        self.assertEqual(result["status"], "ready")
        self.assertEqual([r for r in result["lines"] if r["text"] == "지원 안내"],
                         [{"text": "지원 안내", "bbox": [50, 1940, 250, 22]}])
        self.assertIn({"text": "분산 데이터베이스 구축 경험 우대", "bbox": [50, 2300, 250, 22]}, result["lines"])

    def test_written_mandatory_condition_is_evidence_without_a_section_heading(self):
        rows = [(30, 40, "Cleaning crew wanted", 97),
                (30, 100, "Clean customer sites overnight.", 95),
                (30, 160, "Applicants must provide their own transportation.", 96)]
        self.assertEqual(run_engine(poster(), rows)["status"], "ready")

    def test_low_quality_non_jd_text_cannot_become_ready(self):
        result = run_engine(poster(), [(35, 50, "모집요강", 70), (35, 100, "000", 15)])
        self.assertEqual(result["status"], "uncertain")

    def test_merged_column_headers_also_hold_the_table(self):
        rows = CLEAR + [(35, 350, "모집분야 담당업무 자격요건 근무지", 97)]
        result = run_engine(poster(), rows)
        self.assertEqual(result["reason"], "ambiguous_layout")

    def test_a_misread_requirement_line_cannot_be_discarded_silently(self):
        rows = [(35, 30 + i * 45, text, 97) for i, text in enumerate(
                ["Backend engineer", "Responsibilities", "Build products and tools", "Develop services",
                 "Maintain databases", "Design APIs", "Support users", "Qualifications", "Collaborate with peers"])]
        result = run_engine(poster(), rows + [(35, 480, "Required English proficiency", 64)])
        self.assertEqual(result["reason"], "low_confidence")

    def test_an_unreadable_second_pass_holds_english_experience(self):
        result = run_engine(poster(), CLEAR + [(35, 350, "3+ years of experience", 97)], [])
        self.assertEqual(result["reason"], "uncertain_number")

    def test_unitless_scores_and_low_confidence_digits_require_confirmation(self):
        for text, confidence in [("필수 어학 자격 TOEIC 850", 99), ("TOEIC850", 98),
                                 ("85O", 43), ("850", 95)]:
            with self.subTest(text=text):
                result = run_engine(poster(), CLEAR + [(35, 350, text, confidence)], [])
                self.assertEqual((result["status"], result["reason"]), ("uncertain", "uncertain_number"))

    def test_known_technical_terms_with_digits_can_still_supply_job_evidence(self):
        result = run_engine(poster(), CLEAR + [(35, 350, "B2B 서비스의 HTML5 개발 경험", 97)])
        self.assertEqual(result["status"], "ready")

    def test_metadata_and_list_numbers_are_separate_from_verified_numeric_requirements(self):
        rows = [(x, y, "1. " + text if i == 1 else text, q) for i, (x, y, text, q) in enumerate(CLEAR)]
        rows += [(35, 330, "접수 마감 12/1", 93), (35, 390, "전화 02-0000-0000", 93),
                 (35, 440, "Page 1/2", 93), (35, 490, "2026년 신입 채용", 95),
                 (35, 540, "필수 어학 자격 TOEIC 850", 96)]
        result = run_engine(poster(), rows, [(10, 10, "필수 어학 자격 TOEIC 850", 94)])
        self.assertEqual(result["status"], "ready")
        texts = [r["text"] for r in result["lines"]]
        self.assertIn("주요 업무", texts)
        self.assertIn("신입 채용", texts)
        self.assertFalse(any(s in t for s in ("12/1", "02-0000", "1/2") for t in texts))
        checked = next(r for r in result["lines"] if "850" in r["text"])
        self.assertEqual(checked["number_check"], "matching_crop")
        self.assertEqual(len(result["metadata_lines"]), 3)

    def test_company_history_numbers_are_annotated_without_becoming_job_requirements(self):
        facts = [(35, 350, "2004년 설립", 92), (35, 420, "전 세계 30개국에 서비스 제공", 79)]
        result = run_engine(poster(), CLEAR + facts)
        self.assertEqual(result["status"], "ready")
        self.assertEqual([r["text"] for r in result["metadata_lines"]], [r[2] for r in facts])

    def test_a_low_confidence_numeric_token_cannot_hide_in_a_high_scoring_line(self):
        # Add five confident words and one weak number, all in the same TSV line.
        token_text = ["Academic", "requirement", "minimum", "score", "TOEIC", "850"]
        data = tsv(CLEAR).decode()
        for number, word in enumerate(token_text, 1):
            score = 78 if word == "850" else 98
            data += f"\n5\t1\t9\t1\t1\t{number}\t{number * 60}\t380\t55\t22\t{score}\t{word}"
        commands = [subprocess.CompletedProcess([], 0, b"eng\nkor\n", b""),
                    subprocess.CompletedProcess([], 0, data.encode(), b"")]
        with patch("subprocess.run", side_effect=commands):
            result = poster_ocr.read_poster(poster())
        self.assertEqual((result["status"], result["reason"]), ("uncertain", "uncertain_number"))

    def test_a_deadline_combined_with_an_unclear_requirement_is_not_discarded(self):
        result = run_engine(poster(), CLEAR + [(35, 340, "접수 마감 12/1 경력 3년 이상", 81)])
        self.assertEqual((result["status"], result["reason"]), ("uncertain", "uncertain_number"))

    def test_matching_ocr_cannot_make_an_impossible_calendar_date_trustworthy(self):
        bad = [(35, 350, "접수 마감 2026.13.40", 98)]
        result = run_engine(poster(), CLEAR + bad, bad)
        self.assertEqual((result["status"], result["reason"]), ("uncertain", "uncertain_number"))

    @unittest.skipUnless(os.environ.get("POSTER_TESSERACT"), "configured Tesseract kor+eng required")
    def test_real_engine_separates_dates_and_checks_experience_on_rendered_korean_poster(self):
        try:
            font = ImageFont.truetype("NotoSansCJK-Regular.ttc", 36)
        except OSError:
            self.skipTest("Noto Sans CJK required for Korean poster rendering")
        img = Image.new("RGB", (1600, 980), "white")
        draw = ImageDraw.Draw(img)
        wording = ["백엔드 엔지니어 모집", "1. 주요 업무", "데이터 분석 시스템 설계", "2. 지원 자격",
                   "서비스 운영 경력 3년 이상", "접수 마감 2026.12.31", "전화 02-0000-0000"]
        for i, text in enumerate(wording):
            draw.text((60, 45 + i * 75), text, font=font, fill="black")
        data = io.BytesIO()
        img.save(data, format="PNG")
        result = poster_ocr.read_poster(data.getvalue())
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(["".join(row["text"].split()) for row in result["lines"]],
                         ["백엔드엔지니어모집", "주요업무", "데이터분석시스템설계", "지원자격", "서비스운영경력3년이상"])
        self.assertEqual(result["lines"][-1]["number_check"], "matching_crop")
        self.assertEqual(len(result["metadata_lines"]), 2)
