import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main


class ResultsSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        report = self.root / 'job-id' / 'report'
        report.mkdir(parents=True)
        self.matches = [
            {
                'match_id': f'M-{index}',
                'application_a': application_a,
                'application_b': application_b,
                'source_a_asset_id': f'A-{index}',
                'source_b_asset_id': f'B-{index}',
                'source_a_file': 'government.pdf',
                'source_b_file': 'records.xlsx',
                'decision': decision,
            }
            for index, application_a, application_b, decision in (
                (1, 'T/2026/000001', '', 'EXACT_VISUAL_IDENTITY'),
                (2, '', 'T/2026/000002', 'HIGH_VISUAL_SIMILARITY'),
                (3, 'T/2026/000003', 'T/2026/000003', 'EXACT_VISUAL_IDENTITY'),
            )
        ]
        (report / 'matches.json').write_text(json.dumps(self.matches), encoding='utf8')
        self.data_patch = patch.object(main, 'DATA', self.root)
        self.jobs_patch = patch.dict(main.JOBS, {'job-id': {'status': 'completed'}})
        self.data_patch.start()
        self.jobs_patch.start()

    def tearDown(self):
        self.jobs_patch.stop()
        self.data_patch.stop()
        self.temp_dir.cleanup()

    def test_search_matches_application_number_on_either_source(self):
        result = main.results('job-id', q='t/2026/000002')

        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['application_b'], 'T/2026/000002')

    def test_search_and_decision_filter_are_paginated_after_filtering(self):
        result = main.results('job-id', offset=1, limit=1, q='t/2026', decision='EXACT_VISUAL_IDENTITY')

        self.assertEqual(result['total'], 2)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['match_id'], 'M-3')


if __name__ == '__main__':
    unittest.main()
