import io
import tempfile
import threading
import unittest
import zipfile
import sys
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from course_downloads import stream_file, allowed_file_auth, download_week, file_is_valid

buffer = io.BytesIO()
with zipfile.ZipFile(buffer, 'w') as archive:
    archive.writestr('ppt/presentation.xml', '<presentation/>')
PPTX = buffer.getvalue()
COUNTS = Counter()
AUTH = 'Basic dGVzdDp0ZXN0'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        COUNTS[(self.path, self.headers.get('Authorization'))] += 1
        if self.path == '/auth.pptx' and self.headers.get('Authorization') != AUTH:
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="test"')
            self.end_headers()
            return
        if self.path == '/redirect.pptx':
            self.send_response(302)
            self.send_header('Location', '/login')
            self.end_headers()
            return
        if self.path == '/missing.pptx':
            self.send_response(404)
            self.end_headers()
            return
        if self.path == '/retry.pptx' and COUNTS[(self.path, None)] == 1:
            self.send_response(503)
            self.end_headers()
            return
        body = b'<html>Login</html>' if self.path == '/html.pptx' else PPTX
        self.send_response(200)
        self.send_header('Content-Type', 'text/html' if self.path == '/html.pptx' else 'application/octet-stream')
        self.send_header('Content-Length', str(len(body) + (20 if self.path == '/truncated.pptx' else 0)))
        self.end_headers()
        self.wfile.write(body)


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        COUNTS.clear()

    def test_credentials_only_after_challenge(self):
        with tempfile.TemporaryDirectory() as directory, requests.Session() as session:
            target = Path(directory) / 'file.pptx'
            self.assertTrue(stream_file(session, self.url + '/auth.pptx', target, AUTH)[0])
            self.assertTrue(file_is_valid(target))
            self.assertEqual(COUNTS[('/auth.pptx', None)], 1)
            self.assertEqual(COUNTS[('/auth.pptx', AUTH)], 1)
            self.assertNotIn('Authorization', session.headers)

    def test_redirect_html_and_incomplete_not_saved(self):
        with tempfile.TemporaryDirectory() as directory, requests.Session() as session:
            for path in ('redirect', 'html', 'truncated'):
                target = Path(directory) / (path + '.pptx')
                self.assertFalse(stream_file(session, self.url + '/' + path + '.pptx', target, AUTH)[0])
                self.assertFalse(target.exists())
                self.assertFalse(target.with_suffix('.pptx.part').exists())
            self.assertEqual(sum(n for (path, auth), n in COUNTS.items() if path == '/login'), 0)

    def test_login_never_receives_file_credentials(self):
        origin = 'https://curriculum.shsmu.edu.cn/docs/a.pptx'
        self.assertTrue(allowed_file_auth(origin, 'https://webvpn2.shsmu.edu.cn/https/token/docs/a.pptx'))
        self.assertFalse(allowed_file_auth(origin, 'https://webvpn2.shsmu.edu.cn/ac/'))
        self.assertFalse(allowed_file_auth(origin, 'https://elsewhere.invalid/docs/a.pptx'))
        self.assertFalse(allowed_file_auth(origin, 'http://curriculum.shsmu.edu.cn/docs/a.pptx'))

    def test_failure_continues_retry_and_resume_no_duplicates(self):
        records = [dict(url=self.url + '/' + name + '.pptx', week='Week 01', fallback_name=name+'.pptx', date='2026-09-07', course=name, title='ppt') for name in ('missing', 'ok', 'retry')]
        page = SimpleNamespace(url=self.url, evaluate=lambda expression: 'test-browser')
        context = SimpleNamespace(cookies=lambda: [])
        args = SimpleNamespace(file_username='', file_password='')
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(download_week(page, context, records, directory, args), 1)
            self.assertEqual(COUNTS[('/missing.pptx', None)], 1)
            self.assertEqual(COUNTS[('/ok.pptx', None)], 1)
            self.assertEqual(COUNTS[('/retry.pptx', None)], 2)
            self.assertEqual(download_week(page, context, records, directory, args), 1)
            self.assertEqual(COUNTS[('/ok.pptx', None)], 1)
            self.assertEqual(COUNTS[('/retry.pptx', None)], 2)
            self.assertEqual(len(list(Path(directory).rglob('*.pptx'))), 2)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    unittest.main()
