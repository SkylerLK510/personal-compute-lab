import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from coordinator import Queue


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'queue.sqlite'
        self.now = 100.
        self.q = Queue(self.path, 5, lambda: self.now)

    def test_expired_attempt_cannot_commit_after_reassignment(self):
        job = self.q.submit({'kind': 'square', 'value': 7})
        first = self.q.claim('mac', 100)
        self.now += 6
        second = self.q.claim('desktop', 100)
        self.assertNotEqual(first['attempt'], second['attempt'])
        self.assertFalse(self.q.heartbeat(job, first['attempt']))
        self.assertFalse(self.q.complete(job, first['attempt'], 49))
        self.assertTrue(self.q.complete(job, second['attempt'], 49))

    def test_duplicate_and_conflicting_completion(self):
        job = self.q.submit({'kind': 'square', 'value': 3})
        a = self.q.claim('mac', 100)['attempt']
        self.assertFalse(self.q.complete(job, a, 8))
        self.assertTrue(self.q.complete(job, a, 9))
        self.assertTrue(self.q.complete(job, a, 9))
        self.assertFalse(self.q.complete(job, a, 10))
        self.assertIsNone(self.q.claim('desktop', 100))
        self.assertEqual(Queue(self.path).status()[0]['result'], '9')

    def test_expired_heartbeat_cannot_resurrect(self):
        j = self.q.submit({'kind':'square', 'value':2})
        a = self.q.claim('mac', 100)['attempt']
        self.now += 5
        self.assertFalse(self.q.heartbeat(j, a))
        self.assertFalse(self.q.complete(j, a, 4))

    def test_memory_reservations(self):
        for _ in range(2):
            self.q.submit({'kind':'square','value':2}, 60)
        self.assertIsNone(self.q.claim('small', 50))
        self.assertIsNotNone(self.q.claim('mac', 100))
        self.assertIsNone(self.q.claim('mac', 100))
        self.assertIsNotNone(self.q.claim('desktop', 100))

    def test_simultaneous_claims_are_unique(self):
        for i in range(20):
            self.q.submit({'kind':'square','value':i})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda i:self.q.claim(str(i), 100), range(30)))
        ids = [x['id'] for x in claims if x]
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)

    def test_uncommitted_result_is_rolled_back(self):
        j = self.q.submit({'kind':'square','value':2})
        with self.assertRaises(RuntimeError):
            with self.q.connection() as db:
                db.execute("UPDATE jobs SET state='done',result='4' WHERE id=?", (j,))
                raise RuntimeError('simulated failure before commit')
        self.assertEqual(Queue(self.path).status()[0]['state'], 'queued')


if __name__ == '__main__':
    unittest.main()
