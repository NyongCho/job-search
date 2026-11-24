import io
import json
import time
import unittest
from contextlib import closing

from PIL import Image

from job_search import collect_jobs, company_logos
from job_search.public_fetch import PublicHTTPError
from tests.test_company_logos import logo_png
from tests.test_deadline_sources import next_page


TOSS = "https://toss.im/career/job-detail?job_id=7990802003"
COMMERCE = TOSS + "&sub_position_id=6545457003&company=%ED%86%A0%EC%8A%A4"
WEBTOON = "https://recruit.webtoonscorp.com/rcrt/view.do?annoId=30005739&lang=ko"
TOSS_ICON = "https://static.toss.im/tds/favicon/favicon-196x196.png"
WEBTOON_ICON = "/share/tmplat/webtoon/img/og/webtoon_favicon_32_2025.svg"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="15" viewBox="0 0 16 15"><path d="M1 1L15 1L8 14Z" fill="#00dc64"/></svg>'


def toss_html(company="토스", subsidiary="토스", selected="토스", icon=TOSS_ICON):
    # Each query's data is itself a serialized JSON value in the public Next.js response.
    def query(name, data, job_id="7990802003"):
        return {"queryKey": ["@tossteam/iso-resource", name, job_id], "state": {"data": json.dumps(data)}}

    queries = [
        query("job-detail", {"job": {"id": 7990802003, "company": company}}),
        query("job-detail-job-group", {"subPositionList": [{"id": 6545457003, "company": subsidiary}]}),
    ]
    if selected:
        queries.append(query("job-detail-og-sub", {"id": 6545457003, "company": selected}, "6545457003"))
    props = {"prefetchResult": {"dehydratedState": {"queries": queries}}}
    return (f'<head><link rel="icon" type="image/png" sizes="196x196" href="{icon}"></head>' + next_page(props))


def webtoon_html(company="NAVER WEBTOON", job_id="30005739", icon=WEBTOON_ICON):
    job = {"@type": "JobPosting", "hiringOrganization": {"@type": "Organization", "name": company}}
    return (f'<head><link rel="icon" type="image/svg+xml" href="{icon}"></head>'
            f'<script type="application/ld+json">{json.dumps(job)}</script>'
            f'<input type="hidden" name="annoId" value="{job_id}">')


def toss_png(color="blue", size=(196, 196), fmt="PNG"):
    data = io.BytesIO()
    with Image.new("RGBA" if fmt == "PNG" else "RGB", size, color) as image:
        image.save(data, format=fmt)
    return data.getvalue()


class OfficialIconTests(unittest.TestCase):
    def attempt(self, company, page_url, page, responses=None, final=None, **fields):
        doc = {"title": company + "｜AI Engineer", "links": [{"text": "지원하러 가기", "url": page_url}], **fields}
        calls = []
        deadline = time.monotonic() + 5

        def fetch(request, **kwargs):
            self.assertEqual(kwargs["deadline"], deadline)
            calls.append(request)
            if request == page_url:
                if isinstance(page, Exception):
                    raise page
                return final or page_url, page.encode(), "text/html"
            response = (responses or {})[request]
            if isinstance(response, Exception):
                raise response
            return response

        with closing(collect_jobs.connect(":memory:")) as db:
            outcome, candidate, png, _ = company_logos._attempt(db, company_logos._plan(doc), fetch, deadline, Image, doc)
            if outcome != "ready":
                self.assertEqual(db.execute("SELECT COUNT(*) FROM logo_image_claims").fetchone()[0], 0)
        return outcome, candidate, png, calls

    def test_toss_uses_declared_square_png_for_verified_job_or_selected_subposition(self):
        for url in (TOSS, COMMERCE):
            with self.subTest(url=url):
                outcome, candidate, png, calls = self.attempt("토스", url, toss_html(), {
                    TOSS_ICON: (TOSS_ICON, toss_png(), "image/png")})
                self.assertEqual(outcome, "ready")
                self.assertEqual(calls, [url, TOSS_ICON])
                self.assertEqual((candidate.source_page, candidate.image_url), (url, TOSS_ICON))
                with Image.open(io.BytesIO(png)) as image:
                    self.assertEqual(image.getpixel((10, 10)), (0, 0, 255, 255))

    def test_webtoon_rasterizes_only_the_declared_official_favicon(self):
        icon = "https://recruit.webtoonscorp.com" + WEBTOON_ICON
        outcome, candidate, png, calls = self.attempt("네이버웹툰", WEBTOON, webtoon_html(), {
            icon: (icon, SVG, "image/svg+xml")})
        self.assertEqual(outcome, "ready")
        self.assertEqual(calls, [WEBTOON, icon])
        self.assertEqual(candidate.image_url, icon)
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertIsNotNone(image.getbbox())
            self.assertIn((0, 220, 100, 255), image.get_flattened_data())

    def test_unverified_job_company_or_asset_does_not_trigger_an_image_request(self):
        for company, url, page, final in (
            ("토스증권", TOSS, toss_html(), None),
            ("토스", TOSS, toss_html(company="토스뱅크"), None),
            ("토스", COMMERCE, toss_html(subsidiary="토스증권"), None),
            ("토스", COMMERCE, toss_html(selected="토스증권"), None),
            ("토스", COMMERCE, toss_html(selected=None), None),
            ("토스", COMMERCE.replace("6545457003", "111"), toss_html(), None),
            ("토스", TOSS + "&job_id=2", toss_html(), None),
            ("토스", TOSS, toss_html(), TOSS.replace("7990802003", "1")),
            ("토스", COMMERCE, toss_html(), TOSS),
            ("토스", TOSS, toss_html(icon="https://other.example/icon.png"), None),
            ("네이버", WEBTOON, webtoon_html(), None),
            ("네이버웹툰", WEBTOON, webtoon_html(company="NAVER"), None),
            ("네이버웹툰", WEBTOON, webtoon_html(job_id="7"), None),
            ("네이버웹툰", WEBTOON, webtoon_html(icon="https://other.example/icon.svg"), None),
            ("네이버웹툰", WEBTOON, webtoon_html(icon="/share/tmplat/webtoon/img/og/other_company.svg"), None),
        ):
            with self.subTest(company=company, url=url, final=final):
                outcome, _, _, calls = self.attempt(company, url, page, final=final)
                self.assertEqual(outcome, "missing")
                self.assertEqual(calls, [url])

    def test_icon_asset_redirect_empty_and_transient_failure_release_claim(self):
        for response, expected in (
            (("https://other.example/brand", logo_png(), "image/png"), "missing"),
            ((TOSS_ICON, toss_png((0, 0, 0, 0)), "image/png"), "missing"),
            ((TOSS_ICON, toss_png(fmt="JPEG"), "image/png"), "missing"),
            ((TOSS_ICON, toss_png(size=(120, 10)), "image/png"), "missing"),
            (PublicHTTPError(503), "pending"),
        ):
            with self.subTest(expected=expected):
                outcome, _, _, calls = self.attempt("토스", TOSS, toss_html(), {TOSS_ICON: response})
                self.assertEqual(outcome, expected)
                self.assertEqual(calls, [TOSS, TOSS_ICON])

    def test_webtoon_invisible_geometry_is_not_cached(self):
        icon = "https://recruit.webtoonscorp.com" + WEBTOON_ICON
        for svg in (SVG.replace(b"#00dc64", b"#ffffff"), SVG.replace(b"M1 1L15 1L8 14Z", b"M32 32H48V48H32Z")):
            with self.subTest(svg=svg):
                outcome, _, _, _ = self.attempt("네이버웹툰", WEBTOON, webtoon_html(), {
                    icon: (icon, svg, "image/svg+xml")})
                self.assertEqual(outcome, "missing")

    def test_official_icon_is_preferred_but_a_page_failure_preserves_labeled_body_logo(self):
        body = "https://assets.example/toss.png"
        fields = {"url": "https://inthiswork.com/archives/1", "images": [{"alt": "토스 로고", "url": body}]}
        good = {TOSS_ICON: (TOSS_ICON, toss_png(), "image/png"), body: (body, logo_png(), "image/png")}
        for page, expected in ((toss_html(), TOSS_ICON), (PublicHTTPError(404), body), (TimeoutError(), body)):
            with self.subTest(page=type(page)):
                outcome, candidate, _, calls = self.attempt("토스", TOSS, page, good, **fields)
                self.assertEqual(outcome, "ready")
                self.assertEqual(candidate.image_url, expected)
                self.assertEqual(calls, [TOSS, expected])

    def test_recovered_post_shares_verified_png_and_keeps_it_on_same_posting_revision(self):
        doc = {"title": "토스｜AI Engineer", "apply_url": TOSS}
        with closing(collect_jobs.connect(":memory:")) as db:
            for i in (1, 2):
                with db:
                    post = {**doc, "id": i}
                    version = collect_jobs.save_document(db, post)
                    company_logos.enqueue_logo(db, i, post, version)
                    db.execute("UPDATE job_company_logos SET identity='linked:toss.im:토스' WHERE post_id=?", (i,))
                calls = []

                def fetch(url, **_):
                    calls.append(url)
                    if url == TOSS:
                        return url, toss_html().encode(), "text/html"
                    self.assertEqual(url, TOSS_ICON)
                    return url, toss_png(), "image/png"

                outcome = company_logos._refresh_post(db, i, fetch=fetch, deadline=time.monotonic() + 5, image_module=Image)
                self.assertEqual(outcome, "ready")
                self.assertEqual(calls, [TOSS, TOSS_ICON] if i == 1 else [TOSS])
            with db:
                collect_jobs.save_document(db, {**doc, "id": 1, "lines": ["새 공고 본문"]})
            self.assertIsNotNone(db.execute("SELECT company_key FROM job_company_logos WHERE post_id=1").fetchone()[0])
            with db:
                collect_jobs.save_document(db, {**doc, "id": 2, "apply_url": COMMERCE})
            self.assertIsNone(db.execute("SELECT company_key FROM job_company_logos WHERE post_id=2").fetchone()[0])
