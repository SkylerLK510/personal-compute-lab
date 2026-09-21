import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from worker import request

ROOT = Path(__file__).parent


class ProcessTests(unittest.TestCase):
    def test_worker_death_and_coordinator_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            processes = []
            def launch(script, *args):
                p = subprocess.Popen([sys.executable, str(ROOT/script), *args],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                processes.append(p)
                return p
            def wait_for(predicate, timeout=12):
                end = time.monotonic()+timeout
                while time.monotonic() < end:
                    try:
                        if predicate():
                            return
                    except OSError:
                        pass
                    time.sleep(.05)
                self.fail('Timed out waiting for process state')
            def start():
                p = launch('coordinator.py', '--db', str(Path(folder)/'queue.sqlite'),
                           '--port', str(port), '--lease-seconds', '1')
                wait_for(lambda: request(url, '/status') is not None)
                return p
            try:
                coordinator = start()
                slow = request(url, '/submit', payload={'kind':'square','value':7,'delay':2})
                doomed = launch('worker.py') if port == 8765 else launch('worker.py', '--url', url)
                wait_for(lambda:any(j['state']=='running' for j in request(url,'/status')))
                old_attempt = request(url, '/status')[0]['attempt']
                doomed.kill()
                doomed.wait(timeout=3)
                coordinator.kill()
                coordinator.wait(timeout=3)
                coordinator = start()
                for i in range(10):
                    request(url, '/submit', payload={'kind':'square','value':i,'delay':.05})
                launch('worker.py', '--url', url)
                launch('worker.py', '--url', url)
                wait_for(lambda:all(j['state']=='done' for j in request(url,'/status')))
                rows = request(url, '/status')
                self.assertEqual(len(rows), 11)
                self.assertEqual(len({j['id'] for j in rows}), 11)
                result = next(j for j in rows if j['id']==slow)
                self.assertEqual(json.loads(result['result']),49)
                self.assertNotEqual(old_attempt, result['attempt'])
                self.assertFalse(request(url, '/complete', job=slow, attempt=old_attempt, result=49))
                coordinator.kill()
                coordinator.wait(timeout=3)
                start()
                self.assertEqual(rows, request(url, '/status'))
            finally:
                for p in processes:
                    if p.poll() is None:
                        p.terminate()
                    try:
                        p.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
                    p.stderr.close()


if __name__ == '__main__':
    unittest.main()
