"""Fixture-only tests. Any accidental network call fails immediately."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import watch

ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden in tests'))
        self.network_mock = self.network.start()
        self.addCleanup(self.network.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def execute(self, fixture='baseline', config=None):
        with redirect_stdout(io.StringIO()) as output:
            code = watch.run(config or ROOT / 'watch_targets.json', self.output,
                             ROOT / 'fixtures' / fixture, NOW)
        report = json.loads((self.output / 'last_run.json').read_text())
        return code, report, output.getvalue()

    def test_numeric_change_addition_removal(self):
        relative = Path('xai-org/x-algorithm/home-mixer/params/param.rs')
        old = (ROOT / 'fixtures/baseline' / relative).read_text()
        new = (ROOT / 'fixtures/changed' / relative).read_text()
        self.assertEqual(watch.numeric_diff(old, new), [
            {'name': 'ADDED', 'old': None, 'new': '0.025'},
            {'name': 'REMOVED', 'old': '2', 'new': None},
            {'name': 'REPLY_WEIGHT', 'old': '5.0', 'new': '6.0'},
        ])

    def test_numeric_suffixes_comments_and_equivalent_literals(self):
        text = '''// const FAKE: f32 = 99;
        /* const HIDDEN: f32 = 1; */
        pub const A: f64 = -1_000.5e-2_f64;
        const B: u32 = 0xff_u32;
        const C: u16 = 0b1_010u16;
        const D: i32 = -0o10;
        const EXPRESSION: f32 = 2.0 * 3.0;
        const TEXT: &str = "42";
        '''
        parsed = watch.numeric_constants(text)
        self.assertEqual({key: str(value) for key, value in parsed.items()},
                         {'A': '-10.005', 'B': '255', 'C': '10', 'D': '-8'})
        self.assertEqual(watch.numeric_diff('const A: f64 = 1.0;', 'const A: f64 = 1e0;'), [])

    def test_doc_keyword_classification(self):
        for text in ('Pricing and billing credits', 'Automation rules for bots', 'Developer policy and compliance'):
            with self.subTest(text=text):
                self.assertEqual(watch.classify(text), ['rules_pricing'])
        self.assertEqual(watch.classify('Visibility filtering labels'), ['visibility'])
        self.assertEqual(watch.classify('Ranking score weight'), ['weights'])
        self.assertEqual(watch.classify('A typo correction'), ['other'])
        self.assertEqual(watch.classify('policyholder bookshelf'), ['other'])

    def test_stale_exact_boundary_and_timezones(self):
        commits = json.loads((ROOT / 'fixtures/baseline/commits.json').read_text())
        for elapsed, expected in ((timedelta(days=14) - timedelta(microseconds=1), 'fresh'),
                                  (timedelta(days=14), 'STALE'),
                                  (timedelta(days=14, seconds=1), 'STALE')):
            with self.subTest(elapsed=elapsed):
                date = NOW - elapsed
                commits[0]['commit']['committer']['date'] = date.isoformat()
                self.assertEqual(watch.freshness(commits, NOW)['status'], expected)
        commits[0]['commit']['committer']['date'] = '2026-10-28T09:00:00+09:00'
        self.assertEqual(watch.freshness(commits, NOW)['status'], 'STALE')

    def test_first_run_baseline_and_second_unchanged(self):
        code, report, stdout = self.execute()
        self.assertEqual(code, 0)
        self.assertIn('baseline established', stdout)
        self.assertEqual([item['status'] for item in report['targets']], ['baseline'] * 5)
        self.assertTrue((self.output / 'snapshots/xai-org/x-algorithm/home-mixer/params/param.rs').exists())
        for path in ('developer-guidelines.mdx', 'developer-terms/policy.mdx',
                     'x-api/getting-started/pricing.mdx'):
            with self.subTest(path=path):
                self.assertTrue((self.output / 'snapshots/xdevplatform/docs' / path).exists())
        self.assertFalse((self.output / 'history').exists())
        code, report, _ = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual([item['status'] for item in report['targets']], ['unchanged'] * 5)
        self.network_mock.assert_not_called()

    def test_changed_fixture_history_and_success_exit(self):
        self.execute()
        code, report, stdout = self.execute('changed')
        self.assertEqual(code, 0)
        self.assertIn('REPLY_WEIGHT: 5.0 -> 6.0', stdout)
        self.assertEqual(report['targets'][0]['categories'], ['weights'])
        self.assertEqual(report['targets'][1]['categories'], ['weights', 'visibility', 'rules_pricing'])
        self.assertEqual([item['status'] for item in report['targets']],
                         ['changed', 'changed', 'unchanged', 'unchanged', 'changed'])
        self.assertEqual(report['targets'][4]['categories'], ['rules_pricing'])
        history = (self.output / 'history/2026-11-11.md').read_text()
        self.assertIn('```diff', history)
        self.assertIn('-pub const REPLY_WEIGHT', history)
        self.assertIn('+pub const REPLY_WEIGHT', history)
        self.assertIn('### pricing\n', history)
        self.assertIn('-Synthetic fixture: pricing is 1 credit per request.', history)
        self.assertIn('+Synthetic fixture: pricing is 2 credits per request.', history)
        self.execute('baseline')
        self.assertEqual((self.output / 'history/2026-11-11.md').read_text().count('\n## '), 2)

    def test_offline_ignores_notification_environment(self):
        with patch.dict('os.environ', {'DISCORD_WEBHOOK_URL': 'https://example.invalid/private',
                                      'HEALTHCHECKS_PING_URL': 'https://example.invalid/private'}):
            self.execute()
            self.execute('changed')
        self.network_mock.assert_not_called()

    def test_missing_target_is_hard_failure_and_does_not_block_others(self):
        config = json.loads((ROOT / 'watch_targets.json').read_text())
        config['targets'][0]['path'] = 'missing.rs'
        path = self.output / 'config.json'
        path.write_text(json.dumps(config))
        code, report, stdout = self.execute(config=path)
        self.assertEqual(code, 1)
        self.assertEqual(report['targets'][0]['status'], 'error')
        self.assertEqual(report['targets'][1]['status'], 'baseline')
        self.assertNotIn(str(self.output), stdout)

    def test_missing_commits_is_hard_failure(self):
        with patch('watch.freshness', side_effect=ValueError('private-value')):
            code, report, stdout = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(report['freshness']['status'], 'error')
        self.assertNotIn('private-value', stdout)
        self.assertNotIn('private-value', json.dumps(report))

    def test_invalid_config_writes_failure_report(self):
        path = self.output / 'bad.json'
        path.write_text('{')
        code, report, _ = self.execute(config=path)
        self.assertEqual(code, 1)
        self.assertTrue(report['hard_failure'])

    def test_no_environment_notifications_no_network(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(watch.notify('summary', True, True), [])
        self.network_mock.assert_not_called()

    def test_healthchecks_failure_suffix_and_safe_errors(self):
        with patch.dict('os.environ', {'HEALTHCHECKS_PING_URL': 'https://hc-ping.com/synthetic?check=1',
                                      'DISCORD_WEBHOOK_URL': ''}, clear=True):
            warnings = watch.notify('failed', False, False)
        request = self.network_mock.call_args.args[0]
        self.assertEqual(request.full_url, 'https://hc-ping.com/synthetic/fail?check=1')
        self.assertEqual(warnings, ['Healthchecks notification failed'])

    def test_unknown_healthchecks_host_no_failure_ping(self):
        with patch.dict('os.environ', {'HEALTHCHECKS_PING_URL': 'https://example.invalid/synthetic'}, clear=True):
            self.assertEqual(watch.notify('failed', False, False), [])
        self.network_mock.assert_not_called()

    def test_hex_digits_are_not_float_suffixes(self):
        self.assertEqual(str(watch.numeric_constants('const A: u32 = 0xff32;')['A']), '65330')

    def test_malformed_health_url_is_safe(self):
        with patch.dict('os.environ', {'HEALTHCHECKS_PING_URL': 'https://[broken'}, clear=True):
            self.assertEqual(watch.notify('ok', True, False), ['Healthchecks notification failed'])
        self.network_mock.assert_not_called()

    def test_report_write_failure_never_pings_success(self):
        with patch('watch.atomic_write', side_effect=OSError('private-value')), patch('watch.notify', return_value=[]) as notify:
            with redirect_stdout(io.StringIO()):
                code = watch.run(ROOT / 'watch_targets.json', self.output, ROOT / 'fixtures/baseline', NOW)
        self.assertEqual(code, 1)
        self.assertFalse(notify.call_args.args[1])

    def test_malformed_target_is_reported_safely(self):
        path = self.output / 'config.json'
        path.write_text(json.dumps({'targets': [{'repo': 'missing'}]}))
        code, report, _ = self.execute(config=path)
        self.assertEqual(code, 1)
        self.assertEqual(report['targets'][0]['status'], 'error')

    def test_snapshot_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            watch.local_path(self.output, '../../escape')


if __name__ == '__main__':
    unittest.main()
