"""Local-only prototype. SQLite owns leases and committed results."""
import argparse
import contextlib
import hashlib
import json
import sqlite3
from socketserver import TCPServer
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Queue:
    def __init__(self, path, lease_seconds=5, clock=time.time):
        self.path, self.lease_seconds, self.clock = str(path), lease_seconds, clock
        with self.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, memory_mb INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', attempt TEXT, worker TEXT,
                expires REAL, result TEXT, digest TEXT);
            ''')

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, payload, memory_mb=1):
        if not isinstance(payload, dict) or payload.get('kind') != 'square':
            raise ValueError('Only built-in square jobs are allowed')
        value = payload.get('value')
        delay = payload.get('delay', 0)
        if type(value) is not int or abs(value) > 10**9:
            raise ValueError('value must be a bounded integer')
        if type(delay) not in (int, float) or not 0 <= delay <= 30:
            raise ValueError('delay must be between 0 and 30 seconds')
        if type(memory_mb) is not int or not 1 <= memory_mb <= 10**6:
            raise ValueError('invalid memory reservation')
        job = uuid.uuid4().hex
        with self.connection() as db:
            db.execute('INSERT INTO jobs(id,payload,memory_mb) VALUES(?,?,?)',
                       (job, json.dumps(payload), memory_mb))
        return job

    def claim(self, worker, capacity_mb):
        if not isinstance(worker, str) or not worker or len(worker) > 128:
            raise ValueError('invalid worker')
        if type(capacity_mb) is not int or capacity_mb < 1:
            raise ValueError('invalid capacity')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            db.execute("UPDATE jobs SET state='queued',attempt=NULL,worker=NULL,expires=NULL WHERE state='running' AND expires<=?", (now,))
            reserved = db.execute("SELECT COALESCE(SUM(memory_mb),0) FROM jobs WHERE state='running' AND worker=?", (worker,)).fetchone()[0]
            row = db.execute("SELECT * FROM jobs WHERE state='queued' AND memory_mb<=? ORDER BY rowid LIMIT 1", (capacity_mb-reserved,)).fetchone()
            if row is None:
                return None
            attempt = uuid.uuid4().hex
            db.execute("UPDATE jobs SET state='running',attempt=?,worker=?,expires=? WHERE id=?", (attempt, worker, now+self.lease_seconds, row['id']))
            return {'id': row['id'], 'attempt': attempt, 'payload': json.loads(row['payload']), 'lease_seconds': self.lease_seconds}

    def heartbeat(self, job, attempt):
        with self.connection() as db:
            now = self.clock()
            return db.execute("UPDATE jobs SET expires=? WHERE id=? AND attempt=? AND state='running' AND expires>?", (now+self.lease_seconds, job, attempt, now)).rowcount == 1

    def complete(self, job, attempt, result):
        if type(result) is not int:
            raise ValueError('result must be an integer')
        encoded = json.dumps(result)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
            if row is None or row['attempt'] != attempt:
                return False
            # Same-result retransmission is acknowledged without applying it again.
            if row['state'] == 'done':
                return row['result'] == encoded
            if row['state'] != 'running' or row['expires'] <= self.clock():
                return False
            if result != json.loads(row['payload'])['value'] ** 2:
                return False
            db.execute("UPDATE jobs SET state='done',result=?,digest=?,expires=NULL WHERE id=?", (encoded, digest, job))
            return True

    def status(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT id,state,attempt,worker,expires,result,digest FROM jobs ORDER BY rowid')]


def serve(queue, port=8765):
    class LocalServer(ThreadingHTTPServer):
        def server_bind(self):
            # This numeric loopback service does not need reverse DNS at startup.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 16384:
                    raise ValueError('invalid request length')
                body = json.loads(self.rfile.read(length))
                routes = {'/submit': queue.submit, '/claim': queue.claim,
                          '/heartbeat': queue.heartbeat, '/complete': queue.complete,
                          '/status': queue.status}
                if self.path not in routes:
                    self.send_error(404)
                    return
                result = routes[self.path](**body)
                data = json.dumps(result).encode()
                self.send_response(200)
            except (ValueError, TypeError, KeyError) as exc:
                data = json.dumps({'error': str(exc)}).encode()
                self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return LocalServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='queue.sqlite')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--lease-seconds', type=float, default=5)
    args = parser.parse_args()
    if args.lease_seconds <= 0:
        parser.error('lease must be positive')
    server = serve(Queue(args.db, args.lease_seconds), args.port)
    print(f'Coordinator on http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
