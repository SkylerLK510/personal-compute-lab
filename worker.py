"""One job at a time; no shell commands or arbitrary code accepted."""
import argparse
import json
import threading
import time
import urllib.request
import uuid


def request(url, route, **body):
    req = urllib.request.Request(url + route, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)


def run(url, capacity_mb=256, once=False):
    worker = uuid.uuid4().hex
    while True:
        try:
            job = request(url, '/claim', worker=worker, capacity_mb=capacity_mb)
            if job is None:
                if once:
                    return
                time.sleep(.2)
                continue
            stop = threading.Event()
            lost = threading.Event()
            def renew():
                while not stop.wait(job['lease_seconds']/3):
                    try:
                        if not request(url, '/heartbeat', job=job['id'], attempt=job['attempt']):
                            lost.set()
                            return
                    except (OSError, ValueError):
                        lost.set()
                        return
            thread = threading.Thread(target=renew, daemon=True)
            thread.start()
            try:
                payload = job['payload']
                time.sleep(payload.get('delay', 0))
                if not lost.is_set():
                    request(url, '/complete', job=job['id'], attempt=job['attempt'], result=payload['value']**2)
            finally:
                stop.set()
                thread.join(timeout=4)
            if once:
                return
        except (OSError, ValueError):
            if once:
                raise
            time.sleep(.5)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--url', default='http://127.0.0.1:8765')
    p.add_argument('--capacity-mb', type=int, default=256)
    p.add_argument('--once', action='store_true')
    a = p.parse_args()
    run(a.url, a.capacity_mb, a.once)
