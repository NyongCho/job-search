import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import collect_jobs
import dashboard_store as store
import import_profile_applications as importer
from test_collect_jobs import posting


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.state = self.root / 'jobs.sqlite', self.root / 'private/state.sqlite'
        collect_jobs.collect(self.source, lambda _: ([posting()], 1), datetime(2026, 11, 16, tzinfo=timezone.utc))
        store.init_state_db(self.source, self.state)
        self.profile = self.root / 'profile'
        folder = self.profile / 'applications/예시기업'
        folder.mkdir(parents=True)
        self.summary = folder / '00_직무와_자료상태.txt'
        self.summary.write_text('기업·과정: 예시기업\n직무: AI 개발\n\n진행 상태\n과거 서류 합격. 면접 차수 미확인.\n\n자기소개서·이력서 버전 구분\n마지막 저장본.\n')
        self.records = importer.read_records(self.profile, 'https://github.com/example/profile/tree/main', '2026-11-16')

    def imported(self):
        store.import_profile_jobs(self.source, self.state, self.records)
        return store.list_jobs(self.source, self.state, view='applied')['items'][0]['id']

    def dump(self, path):
        with sqlite3.connect(path) as db:
            return '\n'.join(db.iterdump())

    def test_import_search_and_historical_provenance(self):
        original = self.dump(self.source)
        report = store.import_profile_jobs(self.source, self.state, self.records)
        self.assertEqual(report, {'created': 1, 'skipped': 0})
        listing = store.list_jobs(self.source, self.state, view='applied', q='AI 개발')
        self.assertEqual(listing['total'], 1)
        job = store.get_job(self.source, self.state, listing['items'][0]['id'])
        self.assertLess(job['id'], 0)
        self.assertEqual(job['status'], 'applied')
        self.assertEqual(job['source_kind'], 'profile')
        self.assertIsNone(job['reviewed_at'])
        self.assertIsNone(job['published_at'])
        self.assertFalse(job['is_unread'])
        self.assertEqual(job['progress']['document']['result'], 'passed')
        self.assertEqual(job['progress']['interview1']['result'], 'unknown')
        self.assertEqual(job['progress']['document']['event_date'], '')
        self.assertIn('/applications/%EC%98%88%EC%8B%9C%EA%B8%B0%EC%97%85', job['source_url'])
        self.assertEqual(self.dump(self.source), original)

    def test_reimport_and_schema_init_preserve_user_changes(self):
        post_id = self.imported()
        store.set_stage(self.source, self.state, post_id, 'interview2', 'failed', '2026-11-15', '회고')
        store.set_status(self.source, self.state, post_id, 'archived')
        before = self.dump(self.state)
        self.assertEqual(store.import_profile_jobs(self.source, self.state, self.records), {'created': 0, 'skipped': 1})
        store.init_state_db(self.source, self.state)
        self.assertEqual(self.dump(self.state), before)
        self.assertEqual(store.list_jobs(self.source, self.state, view='archived')['total'], 1)

    def test_stage_updates_preserve_other_stages_and_seen(self):
        store.set_status(self.source, self.state, 1, 'applied')
        with sqlite3.connect(self.state) as db:
            db.execute("UPDATE job_state SET seen_reviewed_at='2026-11-15T00:00:00.000000+00:00' WHERE post_id=1")
        original = self.dump(self.source)
        store.set_stage(self.source, self.state, 1, 'document', 'passed', '', '서류 통과')
        store.set_stage(self.source, self.state, 1, 'interview1', 'in_progress', '2026-11-23', '오후 면접')
        store.set_stage(self.source, self.state, 1, 'interview2', 'not_started', '', '')
        job = store.get_job(self.source, self.state, 1)
        self.assertEqual(job['progress']['document']['note'], '서류 통과')
        self.assertEqual(job['progress_label'], '1차 면접 · 진행 중')
        self.assertEqual(self.dump(self.source), original)
        collect_jobs.collect(self.source, lambda _: ([posting(content='<p>JD 변경</p>')], 1), datetime(2026, 11, 17, tzinfo=timezone.utc))
        self.assertEqual(store.get_job(self.source, self.state, 1)['progress'], job['progress'])
        with sqlite3.connect(self.state) as db:
            self.assertEqual(db.execute('SELECT seen_reviewed_at FROM job_state WHERE post_id=1').fetchone()[0], '2026-11-15T00:00:00.000000+00:00')
        # Independent app state does not change JD on writes; recollection above is deliberate.
        self.assertNotEqual(self.dump(self.source), original)

    def test_invalid_stage_payloads_are_atomic(self):
        post_id = self.imported()
        before = self.dump(self.state)
        for stage, result, day, note in [('third','passed','',''), ('document','bad','',''),
                ('document','passed','2026-02-30',''), ('document','passed','20261116',''),
                ('document','passed','','x'*1001), ('document','passed','',[])]:
            with self.subTest(stage=stage,result=result,day=day), self.assertRaises(ValueError):
                store.set_stage(self.source, self.state, post_id, stage, result, day, note)
        self.assertEqual(self.dump(self.state), before)
        with self.assertRaises(LookupError):
            store.set_stage(self.source, self.state, -999, 'document', 'passed', '', '')
        with self.assertRaises(ValueError):
            store.set_stage(self.source, self.state, 1, 'document', 'passed', '', '')

    def test_old_schema_migration_preserves_rows(self):
        legacy = self.root / 'old.sqlite'
        with sqlite3.connect(legacy) as db:
            db.execute('CREATE TABLE job_state(post_id INTEGER PRIMARY KEY,status TEXT,seen_reviewed_at TEXT,updated_at TEXT)')
            db.execute("INSERT INTO job_state VALUES(1,'interested','2026-11-15','2026-11-15')")
        store.init_state_db(self.source, legacy)
        job = store.get_job(self.source, legacy, 1)
        self.assertEqual(job['status'], 'interested')
        self.assertTrue(all(value['result']=='unknown' for value in job['progress'].values()))
        self.assertEqual(legacy.stat().st_mode & 0o777, 0o600)

    def test_import_validation_rolls_back_all_rows(self):
        invalid = {**self.records[0], 'source_key': 'applications/other', 'company': ''}
        before = self.dump(self.state)
        with self.assertRaises(ValueError):
            store.import_profile_jobs(self.source, self.state, [self.records[0], invalid])
        self.assertEqual(self.dump(self.state), before)

    def test_cli_dry_run_does_not_create_state(self):
        state = self.root / 'not-created.sqlite'
        result = subprocess.run([sys.executable, str(Path(importer.__file__)), '--profile-root',str(self.profile),
            '--jobs-db',str(self.source),'--state-db',str(state),'--confirmed-on','2026-11-16'], capture_output=True,text=True,check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report['count'], 1)
        self.assertFalse(state.exists())

    def test_profile_cannot_be_hidden_by_clearing_status(self):
        post_id = self.imported()
        with self.assertRaises(ValueError):
            store.set_status(self.source, self.state, post_id, 'unmarked')
        self.assertEqual(store.list_jobs(self.source, self.state, view='applied')['total'], 1)

    def test_profile_displays_latest_user_record_note(self):
        post_id = self.imported()
        with sqlite3.connect(self.state) as db:
            doc = json.loads(db.execute('SELECT document FROM profile_posts WHERE id=?', (post_id,)).fetchone()[0])
            doc['record_note'] = '지인 소개로 진행한 단기 아르바이트. 일반 채용 전형이 아님.'
            db.execute('UPDATE profile_posts SET document=? WHERE id=?', (json.dumps(doc), post_id))
        self.assertEqual(store.get_job(self.source, self.state, post_id)['reason'], doc['record_note'])
        self.assertEqual(store.list_jobs(self.source, self.state, view='applied', q='단기 아르바이트')['total'], 1)

    def test_all_skipped_stages_mean_no_recruitment_process(self):
        post_id = self.imported()
        for stage in ['document','interview1','interview2','final']:
            store.set_stage(self.source, self.state, post_id, stage, 'skipped', '', '지인 소개 단기 업무')
        self.assertEqual(store.get_job(self.source, self.state, post_id)['progress_label'], '채용 전형 해당 없음')
        store.set_stage(self.source, self.state, post_id, 'document', 'failed', '', '')
        self.assertEqual(store.get_job(self.source, self.state, post_id)['progress_label'], '서류 · 불합격')

    def test_uncertain_or_missing_document_result_is_not_promoted(self):
        for text in ['서류 합격 여부 미확인', '서류 합격 시 면접 예정', '서류 합격 확인 필요',
                     '서류 합격 가능성 있음', '아마 서류 합격', '서류 합격이라고 가정']:
            with self.subTest(text=text):
                self.summary.write_text('기업·과정: 예시기업\n직무: 개발\n진행 상태\n'+text+'\n자기소개서·이력서 버전 구분\n')
                result = importer.read_records(self.profile, '', '2026-11-16')[0]
                self.assertEqual(result['document_result'], 'unknown')
        self.summary.write_text('기업·과정: 예시기업\n직무: 개발\n자료 작성 예시: 서류 합격\n')
        self.assertEqual(importer.read_records(self.profile, '', '2026-11-16')[0]['document_result'], 'unknown')
        for text, expected in [('서류 불합격 통보.', 'failed'),
            ('당시 대화에 서류 합격 및 2026-10-02 14:00 면접 확정 기록.', 'passed')]:
            self.summary.write_text('기업·과정: 예시기업\n직무: 개발\n진행 상태\n'+text+'\n')
            self.assertEqual(importer.read_records(self.profile, '', '2026-11-16')[0]['document_result'], expected)


if __name__ == '__main__':
    unittest.main()
