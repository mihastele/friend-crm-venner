"""Focused regression: the PWA shell serves and boots without JS errors.

Run:  python3 -m unittest tests.test_frontend_boots
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="venner-test-")
os.environ["VENNER_SECRET_FILE"] = os.path.join(TMP, "secret")
sys.path.insert(0, ROOT)

import server as venner_server  # noqa: E402  (needs env above first)
from store import Store  # noqa: E402


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class FrontendBoots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), venner_server.Handler)
        cls.httpd.store = Store(os.path.join(TMP, "test.db"))
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as res:
            return res.status, res.read()

    def test_static_shell_serves(self):
        for path in ["/", "/app.js", "/styles.css", "/manifest.json",
                     "/service-worker.js", "/icons/icon-192.png",
                     "/icons/icon.svg", "/samples/contacts.vcf", "/samples/phones.csv"]:
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(len(body) > 0, path)

    def test_frontend_boots_without_js_errors(self):
        chrome = (shutil.which("google-chrome") or shutil.which("chromium")
                  or shutil.which("chromium-browser"))
        if not chrome:
            raise unittest.SkipTest("no chrome/chromium available")
        proc = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", "--enable-logging=stderr",
             "--dump-dom", self.base + "/"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        bad = [ln for ln in proc.stderr.splitlines()
               if re.search(r"uncaught|syntaxerror", ln, re.IGNORECASE)]
        self.assertEqual(bad, [], "\n".join(bad))
        self.assertIn("Sign in", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
