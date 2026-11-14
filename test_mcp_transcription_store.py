import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from mcp_transcription_store import TranscriptionStore, TranscriptionError, reserved_seconds


class TranscriptionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'usage.db'
        self.now = 100000
        self.stores = []
        self.addCleanup(lambda: [s.close() for s in self.stores])
    def store(self, **kwargs):
        s = TranscriptionStore(self.path, clock=lambda: self.now, **kwargs)
        self.stores.append(s); return s
    def claim(self, s, n, duration=1): return s.claim(f'{n:064x}', duration, drive_id='drive-id')
    def test_four_rolling_limits(self):
        self.assertEqual(reserved_seconds(.2), 11)
        self.assertEqual(reserved_seconds(10.1), 12)
        for dimension in ('rpm', 'rpd', 'ash', 'asd'):
            with self.subTest(dimension=dimension):
                p = Path(self.temp.name) / (dimension + '.db')
                limit = 11 if dimension in ('ash', 'asd') else 1
                s = TranscriptionStore(p, clock=lambda: self.now, limits={dimension: limit})
                self.stores.append(s)
                self.claim(s, 1)
                s.failed(f'{1:064x}')
                with self.assertRaises(TranscriptionError) as error: self.claim(s, 2)
                self.assertEqual(error.exception.code, 'quota_exceeded')
                self.assertGreater(error.exception.retry_after, 0)
    def test_default_twenty_per_minute(self):
        s = self.store()
        for n in range(20):
            self.claim(s, n); s.failed(f'{n:064x}')
        with self.assertRaises(TranscriptionError): self.claim(s, 20)
        self.now += 61; self.assertTrue(self.claim(s, 20)['new'])
    def test_atomic_duplicate_claim_and_boundary(self):
        s1, s2 = self.store(limits={'rpm': 1}), self.store(limits={'rpm': 1})
        def attempt(pair):
            s, n = pair
            try: return self.claim(s, n)
            except TranscriptionError: return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, [(s1, 1), (s2, 1)]))
        self.assertEqual(sum(bool(r and r['new']) for r in results), 1)
        self.assertEqual(s1.health()['used']['rpm'], 1)
        with self.assertRaises(TranscriptionError): self.claim(s2, 2)
    def test_restart_and_clock_rollback_do_not_reset(self):
        s = self.store(limits={'rpm': 1})
        self.claim(s, 1); s.submitted(f'{1:064x}')
        again = self.store(limits={'rpm': 1})
        self.now -= 1000
        self.assertEqual(again.get(f'{1:064x}')['status'], 'submitted')
        with self.assertRaises(TranscriptionError): self.claim(again, 2)
        self.assertFalse(self.claim(again, 1)['new'])
    def test_provider_block_survives_restart(self):
        s = self.store(); s.block(120)
        again = self.store()
        with self.assertRaises(TranscriptionError) as error: self.claim(again, 1)
        self.assertEqual(error.exception.code, 'provider_rate_limited')
        self.now += 121; self.assertTrue(self.claim(again, 1)['new'])
    def test_single_result_and_physical_budget(self):
        s = self.store()
        key = f'{1:064x}'; self.claim(s, 1); s.submitted(key)
        s.save_result(key, {'text': 'recognized'})
        self.assertEqual(s.pending_result()['result']['text'], 'recognized')
        with self.assertRaises(TranscriptionError): self.claim(s, 2)
        s.completed(key)
        self.assertIsNone(s.pending_result())
        self.assertNotIn('recognized', str(s.health()))
        self.assertTrue(self.claim(s, 2)['new'])
        self.assertLess(sum(p.stat().st_size for p in self.path.parent.glob('usage.db*')), 8388608)
    def test_result_limit_and_uncertain_day(self):
        s = self.store(); key = f'{1:064x}'; self.claim(s, 1); s.submitted(key)
        with self.assertRaises(TranscriptionError): s.save_result(key, {'text': 'x' * 262144})
        s.uncertain(key)
        self.assertFalse(self.claim(s, 1)['new'])
        self.now += 86401; self.assertTrue(self.claim(s, 1)['new'])
    def test_limits_cannot_increase_free_tier(self):
        with self.assertRaises(ValueError): self.store(limits={'rpm': 21})


if __name__ == '__main__': unittest.main()
