import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_search import dashboard, company_research
from tests.test_collect_jobs import posting
from job_search import collect_jobs


class CompanyResearchTests(unittest.TestCase):
    def test_optional_private_research_renders_without_publishing_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, state = root / 'jobs.sqlite', root / 'state.sqlite'
            item = posting(1)
            item['title']['rendered'] = 'Example Company｜Engineer'
            collect_jobs.collect(source, lambda _: ([item], 1))
            research = root / 'research.json'
            record = {'key': 'example', 'name': 'Example Company', 'aliases': ['Example Company'],
                      'status': 'reviewed', 'rating': 3.5, 'benefits_rating': 3.0,
                      'work_life_rating': 4.0, 'review_count': 2, 'pros': 'Example benefit',
                      'cons': 'Example limitation', 'caveat': 'Small sample',
                      'jobplanet_url': 'https://www.jobplanet.co.kr/companies/123/reviews/example',
                      'latest_review_month': '2026-01'}
            research.write_text(json.dumps({'schema_version': 1, 'checked_on': '2026-01-15',
                                             'companies': [record]}))
            config = {'TESTING': True, 'JOBS_DB': source, 'STATE_DB': state, 'DEV_MODE': True,
                      'BASE_URL': 'http://localhost', 'SECRET_KEY': 'x' * 40,
                      'COMPANY_RESEARCH': research}
            app = dashboard.create_app(config)
            body = app.test_client().get('/jobs/1').get_data(as_text=True)
            self.assertIn('Example benefit', body)
            self.assertIn('표본 적음', body)
            self.assertIn('/companies/123/reviews/example', body)
            index = company_research.load_research(research)
            self.assertIsNone(company_research.lookup_research(index, 'Example Company Cloud'))
            research.unlink()
            app = dashboard.create_app(config)
            response = app.test_client().get('/jobs/1')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('Example benefit', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
