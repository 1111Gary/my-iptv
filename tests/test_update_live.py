import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from update_live import parse_txt, valid_stream_url, build


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            'sources': [{'url': 'https://example.com/upstream.txt'}],
            'hls_only': True, 'max_lines_per_channel': 2,
            'keep_previous_if_missing': True,
            'groups': [{'name': '精选', 'channels': [
                {'name': 'CCTV1', 'aliases': ['CCTV-1']},
                {'name': 'CCTV13'},
            ]}]
        }

    def test_exact_match_does_not_confuse_cctv1_and_cctv10(self):
        s = parse_txt('央视,#genre#\nCCTV10,https://example.com/10.m3u8\nCCTV1,https://example.com/1.m3u8\n')
        output, stats = build(self.cfg, s, {}, set(), {})
        self.assertIn('CCTV1,https://example.com/1.m3u8', output)
        self.assertNotIn('/10.m3u8', output)
        self.assertEqual(stats[1]['state'], 'missing')

    def test_dedupe_and_limit_and_filter_mp4(self):
        s = parse_txt('台,#genre#\nCCTV1,https://example.com/a.m3u8\nCCTV1,https://example.com/a.m3u8\nCCTV1,https://example.com/b.mp4\nCCTV1,https://example.com/c.m3u8?x=1\nCCTV1,https://example.com/d.m3u8\n')
        output, stats = build(self.cfg, s, {}, set(), {})
        self.assertEqual(output.count('CCTV1,'), 2)
        self.assertNotIn('.mp4', output)
        self.assertNotIn('/d.m3u8', output)

    def test_group_and_url_with_comma(self):
        s = parse_txt('\ufeff央视,#genre#\nCCTV13,https://example.com/13.m3u8?a=1,b=2\n')
        self.assertEqual(s['CCTV13'][0], 'https://example.com/13.m3u8?a=1,b=2')
        self.assertNotIn('央视', s)

    def test_private_and_non_hls_urls_rejected(self):
        for url in ('http://192.168.1.5/a.m3u8', 'file:///abc',
                    'http://localhost/a.m3u8', 'http://example.com/a.mp4',
                    'http://example.com:123/udp/239.0.0.1:9000'):
            self.assertFalse(valid_stream_url(url), url)
        self.assertTrue(valid_stream_url('http://example.com/live.m3u8?k=abc'))

    def test_prev_stable_priority_and_fallback(self):
        s = {'CCTV1': ['https://example.com/a.m3u8', 'https://example.com/b.m3u8']}
        prev = {'CCTV1': ['https://example.com/b.m3u8'],
                'CCTV13': ['https://example.com/prev.m3u8']}
        out, stats = build(self.cfg, s, prev, set(), {})
        self.assertLess(out.index('CCTV1,https://example.com/b'), out.index('CCTV1,https://example.com/a'))
        self.assertIn('CCTV13,https://example.com/prev.m3u8', out)
        self.assertEqual(stats[1]['state'], 'previous_unverified')

    def test_blocklist_and_pinned_priority(self):
        s = {'CCTV1': ['https://example.com/a.m3u8', 'https://example.com/b.m3u8']}
        out, _ = build(self.cfg, s, {}, {'https://example.com/a.m3u8'},
                       {'CCTV1': ['https://example.com/own.m3u8']})
        self.assertIn('CCTV1,https://example.com/own.m3u8', out)
        self.assertIn('CCTV1,https://example.com/b.m3u8', out)
        self.assertNotIn('/a.m3u8', out)

    def test_empty_refuses_overwrite(self):
        with self.assertRaises(ValueError):
            build(self.cfg, {'CCTV10':['https://example.com/10.m3u8']}, {}, set(), {})

    def test_cli_local_fixture_and_replay(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            cfg = path/'cfg.json'; cfg.write_text(json.dumps(self.cfg), encoding='utf8')
            upstream = path/'upstream.txt'; upstream.write_text('央视,#genre#\nCCTV-1,https://example.com/live.m3u8\n', encoding='utf8')
            result = path/'live.txt'
            cmd = [sys.executable, str(ROOT/'update_live.py'), '--config', str(cfg),
                   '--source-file', str(upstream), '--output', str(result)]
            first = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn('CCTV1,https://example.com/live.m3u8', result.read_text(encoding='utf8'))
            second = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0)
            self.assertIn('NO_CHANGE', second.stdout)
            before = result.read_bytes()
            upstream.write_text('???', encoding='utf8')
            failed = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(before, result.read_bytes())

if __name__ == '__main__':
    unittest.main()
