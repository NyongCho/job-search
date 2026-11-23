import json
import time
import unittest
from contextlib import closing
from unittest.mock import MagicMock, Mock, patch

from PIL import Image

from job_search import collect_jobs, public_fetch
from job_search.company_logos import _attempt, _official, _plan
from tests.test_company_logos import logo_png


API = "https://api-recruiter.recruiter.co.kr/design/v2?languageType=KOR"
CDN = "https://infra1-static.recruiter.co.kr/builder/"
CONFIG = CDN + "recruiter/2026/11/05/a3e06f09-52c9-4f70-8219-4a938ded6c97.json"
UID = "bcc0498a-a842-4d5f-b37b-2eb7321560af.png"
IMAGE = CDN + "2024/05/25/" + UID
JOB = "https://cosmecca-k.recruiter.co.kr/career/jobs/135315"
COMPANY = "코스메카코리아"


class JobdaLogoTests(unittest.TestCase):
    def discover(self, *, employer=COMPANY, url=JOB, author=COMPANY, canonical=JOB, design=None,
                 logo=None, config_final=CONFIG, image_final=None):
        # Reduced metadata from the public COSMECCA job page and its two design responses.
        page = f'<link rel="canonical" href="{canonical}"/><meta name="author" content="{author}"/>'
        doc = {"title": employer + "｜품질보증 및 완제품검사 담당자 모집", "apply_url": url}
        if design is None:
            design = {"title": COMPANY, "headFileUrl": CONFIG}
        if logo is None:
            logo = {"logoFileUid": UID, "logoFileName": "210x70(px).png", "logoFileUrl": IMAGE}
        data = {
            url: (url, page.encode(), "text/html"),
            API: (API, json.dumps(design).encode(), "application/json"),
            CONFIG: (config_final, json.dumps({"logo": logo}).encode(), "application/json"),
        }
        self.calls = []

        def fetch(request, **kwargs):
            self.calls.append((request, kwargs))
            return data[request]

        if image_final:
            data[IMAGE] = (image_final, logo_png(), "image/png")
            with closing(collect_jobs.connect(":memory:")) as db:
                result = _attempt(db, _plan(doc), fetch, time.monotonic() + 5, Image, doc)
                return result[0], db.execute("SELECT COUNT(*) FROM logo_image_claims").fetchone()[0]
        return _official(_plan(doc), fetch, 25.0, doc)

    def test_valid_image_is_cached_only_for_the_expected_cdn_asset(self):
        self.assertEqual(self.discover(image_final=IMAGE), ("ready", 1))
        self.assertEqual(self.discover(image_final="https://other.example/logo.png"), ("missing", 0))

    def test_collects_the_company_mark_from_the_linked_tenants_design(self):
        result = self.discover(employer="(주)코스메카코리아")
        self.assertIsNotNone(result)
        self.assertEqual((result.key, result.source_page, result.image_url),
                         ("jobda:cosmecca-k.recruiter.co.kr", JOB, IMAGE))
        self.assertEqual(self.calls[1][1]["jobda_tenant"], "cosmecca-k.recruiter.co.kr")
        self.assertEqual([opts["deadline"] for _, opts in self.calls], [25.0] * 3)

    def test_wrong_tenant_job_and_publisher_never_use_the_design_endpoint(self):
        for kwargs in ({"employer": "다른회사"}, {"canonical": JOB[:-1] + "6"},
                       {"url": "https://jobs.example/career/jobs/135315"},
                       {"url": "https://cosmecca-k.recruiter.co.kr.evil.example/career/jobs/135315"}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(self.discover(**kwargs))
                self.assertEqual(len(self.calls), 1)

    def test_conflicting_company_configuration_and_shared_assets_do_not_become_logos(self):
        valid = {"logoFileUid": UID, "logoFileName": "logo.png", "logoFileUrl": IMAGE}
        for kwargs in ({"design": {"title": "코스메카그룹", "headFileUrl": CONFIG}},
                       {"design": {"title": COMPANY, "headFileUrl": "https://other.example/config"}},
                       {"config_final": "https://other.example/config"},
                       {"logo": {**valid, "logoFileUrl": "https://other.example/" + UID}},
                       {"logo": {**valid, "logoFileUrl": CDN + "default-logo.png"}},
                       {"logo": {**valid, "logoFileUrl": IMAGE.replace("static", "ſtatic")}},
                       {"logo": {**valid, "logoFileUid": "different.png"}},
                       {"logo": {**valid, "logoFileName": "JOBDA 기본로고.png"}},
                       {"logo": {**valid, "logoFileName": "HK콜마홀딩스_가로형 1.png"}},
                       {"logo": {}}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(self.discover(**kwargs))


class PublicJobdaFetchTests(unittest.TestCase):
    def test_jobda_design_file_accepts_only_bounded_uncompressed_utf8(self):
        def fetch(url, encoding, body, limit=128):
            client = MagicMock()
            response = client.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.length = None
            response.getheaders.return_value = [("Content-Encoding", encoding)]
            response.read1.side_effect = [body, b""]
            with patch.object(public_fetch, "_addresses", return_value=[]), \
                    patch("http.client.HTTPSConnection", return_value=client):
                return public_fetch.fetch_public(url, deadline=time.monotonic() + 1, limit=limit)
        self.assertEqual(fetch(CONFIG, "UTF-8", b'{"logo": {}}')[1], b'{"logo": {}}')
        self.assertEqual(fetch(IMAGE, "UTF-8", b"\x89PNG")[1], b"\x89PNG")
        for url, encoding in ((CDN + "other.png", "UTF-8"), ("https://other.example/config.json", "UTF-8"),
                              (CONFIG, "gzip")):
            with self.subTest(url=url, encoding=encoding), self.assertRaisesRegex(OSError, "encoding"):
                fetch(url, encoding, b"data")
        with self.assertRaisesRegex(OSError, "size limit"):
            fetch(CONFIG, "UTF-8", b"x" * 129)

    def test_tenant_prefix_is_only_sent_on_the_design_request(self):
        responses = []
        for status, headers in ((302, [("Location", "https://other.example/")]), (200, [])):
            response = Mock(status=status, length=0)
            response.getheaders.return_value = headers
            response.read1.return_value = b""
            client = Mock()
            client.getresponse.return_value.__enter__ = Mock(return_value=response)
            client.getresponse.return_value.__exit__ = Mock(return_value=False)
            responses.append(client)
        with patch.object(public_fetch, "_addresses", return_value=[]), \
                patch("http.client.HTTPSConnection", side_effect=responses):
            public_fetch.fetch_public(API, deadline=time.monotonic() + 1, limit=1024,
                                      jobda_tenant="cosmecca-k.recruiter.co.kr")
        self.assertEqual(responses[0].request.call_args.kwargs["headers"]["prefix"], "cosmecca-k.recruiter.co.kr")
        self.assertNotIn("prefix", responses[1].request.call_args.kwargs["headers"])

    def test_tenant_prefix_requires_the_public_design_endpoint_and_a_tenant_hostname(self):
        for url, host in (("https://other.example/", "company.recruiter.co.kr"),
                          (API, "company.recruiter.co.kr\r\nOther: x"), (API, "other.example")):
            with self.subTest(url=url, host=host), patch.object(public_fetch, "_addresses") as addresses:
                with self.assertRaises(ValueError):
                    public_fetch.fetch_public(url, deadline=time.monotonic() + 1, limit=1024, jobda_tenant=host)
                addresses.assert_not_called()
