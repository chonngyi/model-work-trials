"""Regression checks for frozen prompts, one-attempt dispatch and grading."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import chat_api
import trials


class TrialsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.run = self.folder / "run.sqlite"
        self.case = self.folder / "case"
        shutil.copytree(trials.ROOT / "examples/lead", self.case)
        self.key = patch.dict("os.environ", TRIAL_API_KEY="mock-key")
        self.key.start()
        self.addCleanup(self.key.stop)

    def start(self, channel="subscription"):
        trials.start(self.case, self.run, "model", channel, "test", "unspecified")

    def record(self, stage, text="answer", status="complete", note="", artifact=None):
        answer = self.folder / "answer.txt"
        answer.write_text(text, encoding="utf-8")
        args = argparse.Namespace(run=self.run, stage=stage, status=status, file=answer, note=note,
                                  receipt=None, artifact=[artifact] if artifact else [])
        trials.manual(args)

    def api(self, stage):
        args = argparse.Namespace(run=self.run, stage=stage, endpoint="https://example.invalid/v1/chat/completions",
                                  timeout=30)
        with contextlib.redirect_stdout(io.StringIO()):
            trials.api_stage(args)

    def test_prompt_freeze_and_stage_order(self):
        self.start()
        (self.case / "task.md").write_text("changed", encoding="utf-8")
        with trials.connect(self.run) as conn:
            first = trials.active(conn)["prompt"]
            self.assertNotIn("changed", first)
            self.assertNotIn("整期都无人", first)
            self.assertNotIn("长期方向咨询", first)
        with self.assertRaises(ValueError):
            self.record(2)
        self.record(1)
        with trials.connect(self.run) as conn:
            self.assertIn("整期都无人", trials.active(conn)["prompt"])
        with self.assertRaises(ValueError):
            self.record(1)
        self.record(2)
        with self.assertRaises(FileExistsError):
            self.start()

    def test_partial_capture_artifact_and_grade(self):
        self.start()
        evidence = self.folder / "preview.html"
        evidence.write_bytes(b"<p>hello</p>")
        self.record(1, "partial output", "partial", "cut off", evidence)
        with trials.connect(self.run) as conn:
            self.assertEqual(conn.execute("SELECT body FROM artifacts").fetchone()[0], evidence.read_bytes())
            with self.assertRaises(ValueError):
                trials.active(conn)
            values = trials.grade_template(trials.metadata(conn))
            values.update(reason="No tested recovery", decision="insufficient_evidence")
            for item in values["criteria"].values():
                item.update(rating="unobserved", evidence="No verifiable outcome")
            trials.set_grade(conn, values)
            with self.assertRaises(ValueError):
                trials.set_grade(conn, values)
            row = trials.steps(conn)[0]
            self.assertEqual((row["answer"],row["state"]),("partial output","partial"))

    def test_empty_success_and_early_grade_are_rejected(self):
        self.start()
        with self.assertRaises(ValueError):
            self.record(1, "")
        with trials.connect(self.run) as conn, self.assertRaises(ValueError):
            trials.set_grade(conn, {})
        self.record(1, "", "failed", "No answer")
        with self.assertRaises(ValueError):
            self.record(2)

    def test_api_sends_only_permitted_history_and_records_usage(self):
        self.start("api")
        response = {"model":"served-model","choices":[{"finish_reason":"stop","message":{"content":"response"}}],
                    "usage":{"total_tokens":17}}
        bodies = []
        def open_request(request, timeout):
            bodies.append(json.loads(request.data))
            self.assertEqual(request.get_header("Authorization"), "Bearer mock-key")
            return io.BytesIO(json.dumps(response).encode())
        with patch("chat_api.build_opener") as op:
            op.return_value.open.side_effect = open_request
            self.api(1)
            with self.assertRaises(ValueError):
                self.api(1)
            self.api(2)
        self.assertEqual(len(bodies), 2)
        self.assertEqual([m["role"] for m in bodies[1]["messages"]], ["user","assistant","user"])
        self.assertEqual(bodies[1]["messages"][1]["content"], "response")
        self.assertNotIn("整期都无人", bodies[0]["messages"][0]["content"])
        self.assertIn("整期都无人", bodies[1]["messages"][-1]["content"])
        with trials.connect(self.run) as conn:
            stored = json.loads(trials.steps(conn)[1]["receipt"])
            self.assertEqual(stored["actual_model"], "served-model")
            self.assertEqual(stored["usage"]["total_tokens"], 17)

    def test_empty_and_truncated_api_responses(self):
        for content,reason,want in [("","stop","failed"),("draft","length","partial")]:
            response = {"choices":[{"message":{"content":content},"finish_reason":reason}]}
            with patch("chat_api.build_opener") as op:
                op.return_value.open.return_value=io.BytesIO(json.dumps(response).encode())
                status,answer,note,_=chat_api.complete("https://example.invalid", "test", 1, "m", "unspecified", [])
            self.assertEqual((status,answer),(want,content))
            self.assertTrue(note)

    def test_request_failure_and_repeated_dispatch(self):
        self.start("api")
        with patch("chat_api.build_opener") as op:
            op.return_value.open.side_effect = TimeoutError()
            with self.assertRaises(ValueError):
                self.api(1)
            with self.assertRaises(ValueError):
                self.api(1)
            self.assertEqual(op.return_value.open.call_count, 1)
        with trials.connect(self.run) as conn:
            self.assertEqual(trials.steps(conn)[0]["state"], "failed")
            self.assertIsNotNone(trials.steps(conn)[0]["started"])

    def test_missing_key_and_unsafe_url_consume_no_slot(self):
        self.start("api")
        with self.assertRaises(ValueError):
            chat_api.settings("http://example.com", 10)
        with self.assertRaises(ValueError):
            chat_api.settings("https://example.com/?token=example", 10)
        with patch.dict("os.environ",TRIAL_API_KEY=""), self.assertRaises(ValueError):
            self.api(1)
        with trials.connect(self.run) as conn:
            self.assertEqual(trials.active(conn)["state"], "ready")

    def test_case_boundary_and_comparison(self):
        self.start()
        data=json.loads((self.case / "case.json").read_text(encoding="utf-8"))
        data["stages"]=[["../answer-key.md"]]
        (self.case / "case.json").write_text(json.dumps(data),encoding="utf-8")
        with self.assertRaises(ValueError):
            trials.freeze(self.case)
        second=self.folder / "review.sqlite"
        trials.start(trials.ROOT/"examples/reviewer", second, "m", "subscription","cli","high")
        with self.assertRaises(ValueError):
            trials.compare([self.run,second])

    def test_example_reports_and_html_artifact(self):
        with contextlib.redirect_stdout(io.StringIO()):
            trials.demo(self.run)
        with trials.connect(self.run) as conn:
            _, grade = trials.view(conn)
            self.assertEqual(grade["criteria"]["false_alarm"]["rating"],"met")
        self.run = self.folder / "specialist.sqlite"
        trials.start(trials.ROOT/"examples/specialist",self.run,"m","subscription","web","high")
        artifact=self.folder/"page.html"
        artifact.write_text("<h1>Fixture</h1>",encoding="utf-8")
        self.record(1,"",artifact=artifact)
        with sqlite3.connect(self.run) as conn:
            self.assertEqual(conn.execute("SELECT body FROM artifacts").fetchone()[0],artifact.read_bytes())


    def test_blind_pack_uses_frozen_text_and_blank_scorecard(self):
        with contextlib.redirect_stdout(io.StringIO()):
            trials.demo(self.run)
        first = self.folder / "A.zip"
        second = self.folder / "B.zip"
        with trials.connect(self.run) as conn:
            trials.blind_pack(conn, first)
            trials.blind_pack(conn, second)
            with self.assertRaises(FileExistsError):
                trials.blind_pack(conn, first)
            step = trials.steps(conn)[0]
            expected_form = trials.grade_template(trials.metadata(conn))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as review:
            self.assertEqual(set(review.namelist()), {"prompt.txt", "answer.txt", "review.md", "scorecard.json"})
            self.assertEqual(review.read("prompt.txt"), step["prompt"].encode())
            self.assertEqual(review.read("answer.txt"), step["answer"].encode())
            self.assertEqual(json.loads(review.read("scorecard.json")), expected_form)
            for name in review.namelist():
                self.assertNotIn(b"illustrative answer", review.read(name))
                self.assertEqual(review.getinfo(name).date_time, (1980, 1, 1, 0, 0, 0))

    def test_blind_pack_stopped_run_excludes_unseen_stage(self):
        self.start()
        packet = self.folder / "A.zip"
        with trials.connect(self.run) as conn, self.assertRaises(ValueError):
            trials.blind_pack(conn, packet)
        self.record(1, "partial text", status="partial", note="private delivery note")
        with trials.connect(self.run) as conn:
            trials.blind_pack(conn, packet)
        with zipfile.ZipFile(packet) as review:
            self.assertEqual(review.read("stage-01/answer.txt"), b"partial text")
            self.assertFalse(any("stage-02" in name for name in review.namelist()))
            self.assertFalse(any(b"private delivery note" in review.read(name) for name in review.namelist()))
        with trials.connect(self.run) as conn:
            conn.execute("INSERT INTO artifacts VALUES (1, 'evidence.txt', X'61')")
            with self.assertRaisesRegex(ValueError, "attachments"):
                trials.blind_pack(conn, self.folder / "with-attachment.zip")


if __name__ == "__main__":
    unittest.main()
