"""Self-contained HTTP regression tests; personal runs.json is never touched."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
import helpers


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory(prefix="runledger-api-")
        cls.data = Path(cls.sandbox.name) / "runs.json"
        cls.notes = Path(cls.sandbox.name) / "notes.json"
        cls.original_notes = helpers.NOTES_FILE
        helpers.NOTES_FILE = str(cls.notes)
        cls.original_data = helpers.DATA_FILE
        helpers.DATA_FILE = str(cls.data)
        cls.static = Path(cls.sandbox.name) / "static"
        cls.static.mkdir()
        (cls.static / "index.html").write_bytes((ROOT / "static/index.html").read_bytes())
        sibling = Path(cls.sandbox.name) / "static-private"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("private", encoding="utf-8")
        cls.original_static = app.STATIC_DIR
        app.STATIC_DIR = str(cls.static)
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        helpers.NOTES_FILE = cls.original_notes
        helpers.DATA_FILE = cls.original_data
        app.STATIC_DIR = cls.original_static
        cls.sandbox.cleanup()

    def setUp(self):
        helpers.save_runs([])
        helpers.save_notes([])

    def request(self, path, method="GET", body=None, raw=None):
        payload = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        req = urllib.request.Request(self.base + path, data=payload, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            response = self.opener.open(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            if "application/json" in response.headers.get("Content-Type", ""):
                data = json.loads(data)
            return response.status, data

    def create(self, **fields):
        body = {"type": "运行任务", "task": "Demo", "est_label": "30m"}
        body.update(fields)
        status, data = self.request("/api/runs", "POST", body)
        self.assertEqual(status, 201, data)
        return data["run"]

    def test_empty_start_and_index(self):
        self.assertEqual(self.request("/api/runs")[1], {"runs": [], "projects": []})
        self.assertEqual(self.request("/api/summary")[1]["done_week"], 0)
        status, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"RunLedger", html)
        self.assertIn(b'id="tbody"', html)

    def test_lifecycle_and_time_transition(self):
        run = self.create()
        path = "/api/runs/" + run["id"]
        self.assertEqual(self.request(path + "/submit", "POST")[1]["run"]["status"], "运行中")
        runs = helpers.load_runs()
        runs[0]["submit_time"] = (helpers.now_dt() - helpers.timedelta(minutes=60)).isoformat()
        helpers.save_runs(runs)
        self.assertEqual(self.request("/api/runs")[1]["runs"][0]["status"], "待看结果")
        done = self.request(path + "/check", "POST", {"result_note": "verified"})[1]["run"]
        self.assertEqual(done["status"], "已完成")
        self.assertEqual(done["result_note"], "verified")
        self.assertIsNotNone(done["done_at"])
        self.assertEqual(self.request(path + "/rerun", "POST")[1]["run"]["status"], "运行中")
        reopened = self.request(path + "/reopen", "POST")[1]["run"]
        self.assertEqual(reopened["status"], "待提交")
        self.assertIsNone(reopened["done_at"])
        self.assertIsNone(reopened["submit_time"])

    def test_tree_cycle_and_cascade_delete(self):
        parent = self.create(type="待办项")
        child = self.create(type="待办项", parent_id=parent["id"])
        self.create(parent_id=child["id"])
        status, _ = self.request("/api/runs/" + parent["id"], "PUT", {"type": "运行任务", "est_label": "1h"})
        self.assertEqual(status, 400)
        _, data = self.request("/api/runs/" + parent["id"], "PUT", {"parent_id": child["id"]})
        self.assertEqual(data["run"]["parent_id"], "")
        self.assertEqual(self.request("/api/runs/" + child["id"] + "/rerun", "POST")[0], 400)
        self.assertEqual(self.request("/api/runs/" + parent["id"], "DELETE")[1]["deleted"], 3)
        self.assertEqual(helpers.load_runs(), [])

    def test_concurrent_creates_preserve_every_record(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            runs = list(pool.map(lambda n: self.create(task="Task " + str(n)), range(32)))
        stored = helpers.load_runs()
        self.assertEqual(len(stored), 32)
        self.assertEqual(len({r["id"] for r in runs}), 32)
        self.assertEqual({r["task"] for r in stored}, {"Task " + str(n) for n in range(32)})

    def test_invalid_input_does_not_write(self):
        for raw in (b"{", b"[]", b"null", b'{"task":12}', b'{"task":""}'):
            self.assertEqual(self.request("/api/runs", "POST", raw=raw)[0], 400)
        for label in ("-2h", "garbage 2h", "1h nonsense", "0", ""):
            self.assertEqual(self.request("/api/runs", "POST", {"task": "bad", "est_label": label})[0], 400)
        self.assertEqual(helpers.load_runs(), [])
        run = self.create()
        self.assertEqual(self.request("/api/runs/" + run["id"], "PUT", {"task": " "})[0], 400)
        self.assertEqual(helpers.load_runs()[0]["task"], "Demo")

    def test_corrupt_data_is_not_overwritten(self):
        for broken in ('{broken', '{}', '[12]'):
            self.data.write_text(broken, encoding="utf-8")
            self.assertEqual(self.request("/api/runs", "POST", {"task": "Demo", "est_label": "1h"})[0], 500)
            self.assertEqual(self.request("/api/runs")[0], 500)
            self.assertEqual(self.data.read_text(encoding="utf-8"), broken)

    def test_static_directory_boundary(self):
        self.assertEqual(self.request("/../static-private/secret.txt")[0], 403)
        self.assertEqual(self.request("/missing.html")[0], 404)

    def test_reminder_reports_overdue_minutes(self):
        run = self.create()
        run["status"] = "运行中"
        run["submit_time"] = (helpers.now_dt() - helpers.timedelta(minutes=90)).isoformat()
        helpers.save_runs([run])
        env = dict(os.environ, PYTHONUTF8="1")
        result = subprocess.run([sys.executable, "-c",
            "import helpers,runpy; helpers.DATA_FILE=" + repr(str(self.data)) + "; runpy.run_path('reminder.py')"],
            cwd=str(ROOT), env=env, capture_output=True, encoding="utf-8", check=True)
        self.assertIn("60 分钟", result.stdout)


    def test_urgent_lifecycle_and_legacy_default(self):
        run = self.create(type="待办项", urgent=True)
        path = "/api/runs/" + run["id"]
        self.assertTrue(run["urgent"])
        self.assertEqual(self.request("/api/summary")[1]["urgent_count"], 1)
        self.assertFalse(self.request(path + "/urgent", "POST")[1]["run"]["urgent"])
        self.assertTrue(self.request(path, "PUT", {"urgent": True})[1]["run"]["urgent"])
        self.request(path + "/check", "POST", {"result_note": "done"})
        self.assertEqual(self.request("/api/summary")[1]["urgent_count"], 0)
        self.assertTrue(self.request("/api/runs")[1]["runs"][0]["urgent"])
        self.assertEqual(self.request(path + "/urgent", "POST", {"urgent": "false"})[0], 400)
        self.assertFalse(helpers.enrich({"id": "old"})["urgent"])

    def test_notes_crud_does_not_affect_tasks(self):
        run = self.create()
        before = self.data.read_bytes()
        self.assertEqual(self.request("/api/notes")[1], {"notes": []})
        status, data = self.request("/api/notes", "POST", {"text": "结论\n下一步"})
        self.assertEqual(status, 201)
        note = data["note"]
        path = "/api/notes/" + note["id"]
        updated = self.request(path, "PUT", {"text": "updated"})[1]["note"]
        self.assertEqual(updated["text"], "updated")
        self.assertEqual(updated["created_at"], note["created_at"])
        self.assertEqual(self.request("/api/notes", "POST", {"text": " "})[0], 400)
        self.assertEqual(self.request(path, "DELETE")[0], 200)
        self.assertEqual(self.request(path, "PUT", {"text": "gone"})[0], 404)
        self.assertEqual(self.data.read_bytes(), before)
        self.assertEqual(helpers.load_runs()[0]["id"], run["id"])

    def test_concurrent_notes_and_corrupt_storage(self):
        def create_note(n):
            status, data = self.request("/api/notes", "POST", {"text": str(n)})
            self.assertEqual(status, 201)
            return data["note"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            notes = list(pool.map(create_note, range(24)))
        self.assertEqual(len(helpers.load_notes()), 24)
        self.assertEqual(len({n["id"] for n in notes}), 24)
        for broken in ('{broken', '{}', '[12]'):
            self.notes.write_text(broken, encoding="utf-8")
            self.assertEqual(self.request("/api/notes", "POST", {"text": "new"})[0], 500)
            self.assertEqual(self.request("/api/notes")[0], 500)
            self.assertEqual(self.notes.read_text(encoding="utf-8"), broken)

    def test_notes_order_and_reminder_urgent(self):
        notes = [{"id": "n0001", "text": "old", "created_at": "2026-11-20T00:00:00+08:00"},
                 {"id": "n0002", "text": "new", "created_at": "2026-11-21T00:00:00+08:00"}]
        helpers.save_notes(notes)
        self.assertEqual(self.request("/api/notes")[1]["notes"][0]["id"], "n0002")
        self.create(type="待办项", urgent=True, task="Urgent demo")
        result = subprocess.run([sys.executable, "-c",
            "import helpers,runpy; helpers.DATA_FILE=" + repr(str(self.data)) + "; runpy.run_path('reminder.py')"],
            cwd=str(ROOT), env=dict(os.environ, PYTHONUTF8="1"), capture_output=True, encoding="utf-8", check=True)
        self.assertIn("Urgent demo", result.stdout)
        self.assertIn("紧急任务", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
