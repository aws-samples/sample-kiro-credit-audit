"""Unit tests for kiro_credit_audit.py.

All fixtures are synthetic and created in a temp dir; nothing under ~/.kiro is read.
Run: python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import kiro_credit_audit as kca  # noqa: E402

TS = "2026-09-01T10:00:00.123456789Z"


def turn(cr=1.5, mids=("p1", "a1"), model="claude-opus-5", cyc=2, req=3, ts=TS, ctx=40.5, **extra):
    t = {
        "message_ids": list(mids) if mids is not None else None,
        "total_request_count": req,
        "number_of_cycles": cyc,
        "turn_duration": {"secs": 30, "nanos": 0},
        "end_timestamp": ts,
        "context_usage_percentage": ctx,
        "metering_usage": [{"value": cr, "unit": "credit", "unitPlural": "credits"}] if cr is not None else [],
    }
    if model is not None:
        t["model"] = model
    t.update(extra)
    return t


def session(turns, cwd="/home/u/proj", sess_model="claude-opus-5", rate=2.2):
    return {
        "session_id": "x",
        "cwd": cwd,
        "session_created_reason": "subagent",
        "session_state": {
            "conversation_metadata": {"user_turn_metadatas": turns},
            "rts_model_state": {"model_info": {"model_id": sess_model, "rate_multiplier": rate}},
        },
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._old = os.environ.get("KIRO_SESSIONS_DIR")
        os.environ["KIRO_SESSIONS_DIR"] = self.dir

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KIRO_SESSIONS_DIR", None)
        else:
            os.environ["KIRO_SESSIONS_DIR"] = self._old
        self.tmp.cleanup()

    def write(self, name, obj):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)

    def run_main(self, *argv):
        out = io.StringIO()
        old = sys.argv
        sys.argv = ["kiro_credit_audit.py"] + list(argv)
        try:
            with contextlib.redirect_stdout(out):
                kca.main()
        finally:
            sys.argv = old
        return out.getvalue()


class TestParseTs(unittest.TestCase):
    def test_formats(self):
        utc = timezone.utc
        cases = {
            "2026-09-01T00:00:00Z": datetime(2026, 9, 1, tzinfo=utc),
            "2026-09-01T00:00:00.123456789Z": datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=utc),
            "2026-09-01": datetime(2026, 9, 1, tzinfo=utc),
            "2026-09-01T08:00:00+08:00": datetime(2026, 9, 1, tzinfo=utc),
            "2026-09-01T08:00:00+0800": datetime(2026, 9, 1, tzinfo=utc),
            "2026-08-31T19:00:00.5-05:00": datetime(2026, 9, 1, 0, 0, 0, 500000, tzinfo=utc),
            "2026-09-01 00:00": datetime(2026, 9, 1, tzinfo=utc),
        }
        for s, want in cases.items():
            got = kca.parse_ts(s)
            self.assertEqual(got, want, s)
            self.assertEqual(got.utcoffset().total_seconds(), 0, s)  # always normalised to UTC

    def test_rejects_garbage(self):
        for s in ("", None, 123, "bad", "2026/09/01", "2026-09-01T25:00:00Z",
                  "2026-09-01T00:00.5Z", "2026-09-01.5"):
            self.assertIsNone(kca.parse_ts(s), repr(s))


class TestNum(unittest.TestCase):
    def test_num(self):
        self.assertEqual(kca.num(3), 3)
        self.assertEqual(kca.num(2.5), 2.5)
        self.assertEqual(kca.num(True), 0)
        self.assertEqual(kca.num(None), 0)
        self.assertEqual(kca.num(float("nan")), 0)
        self.assertEqual(kca.num(float("inf"), None), None)
        self.assertEqual(kca.num("7"), 0)


class TestLoad(Base):
    def test_totals_and_sessions(self):
        self.write("a.json", session([turn(1.5, ("p1", "a1")), turn(2.0, ("p2", "a2"))]))
        self.write("b.json", session([]))
        rows, st = kca.load()
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(sum(r["cr"] for r in rows), 3.5)
        self.assertEqual((st["sessions"], st["files"], st["dupes"]), (1, 2, 0))

    def test_dedupe_ignores_null_message_ids(self):
        turns = [turn(1.5, ("p1", "a1")), turn(1.5, (None, None)), turn(9.0, (None, None)),
                 turn(1.5, "abc"), turn(1.5, (None,))]
        self.write("a.json", session(turns))
        rows, st = kca.load()
        self.assertEqual(len(rows), 5)
        self.assertAlmostEqual(sum(r["cr"] for r in rows), 15.0)
        self.assertEqual(st["dupes"], 0)

    def test_dedupe_same_file_copied_into_two_roots(self):
        # The same session file present under two roots (e.g. WSL home and /mnt/c) counts once.
        r1, r2 = os.path.join(self.dir, "r1"), os.path.join(self.dir, "r2")
        os.makedirs(r1)
        os.makedirs(r2)
        for r in (r1, r2):
            with open(os.path.join(r, "same.json"), "w") as fh:
                json.dump(session([turn(1.5, ("p1", "a1")), turn(2.5, ("p2", "a2"))]), fh)
            with open(os.path.join(r, "same.jsonl"), "w") as fh:
                fh.write(json.dumps({"kind": "AssistantMessage", "data": {"message_id": "a1", "content": [
                    {"kind": "toolUse", "data": {"name": "shell"}}]}}) + "\n")
        old = kca.session_roots
        kca.session_roots = lambda: [r1, r2]
        try:
            rows, st = kca.load()
            self.assertEqual(len(rows), 2)
            self.assertAlmostEqual(sum(r["cr"] for r in rows), 4.0)
            self.assertEqual((st["dupes"], st["sessions"], st["files"]), (2, 1, 2))
            out = self.run_main("--tools")
        finally:
            kca.session_roots = old
        self.assertIn("已扫描 1 个 .jsonl", out)
        self.assertIn("toolUse 1/1 次已归到具体轮次", out)
        self.assertNotIn("另有", out)

    def test_adjacent_turns_with_identical_message_ids_are_both_kept(self):
        self.write("a.json", session([turn(5.0, ("p1", "a1")), turn(7.0, ("p1", "a1"))]))
        self.write("b.json", session([turn(1.0, ("p1", "a1"))]))  # different session, same ids
        rows, st = kca.load()
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(sum(r["cr"] for r in rows), 13.0)
        self.assertEqual(st["dupes"], 0)

    def test_malformed_turns_are_skipped_not_fatal(self):
        turns = [turn(1.5), None, "str", turn(2.0, ("p9", "a9"), cyc="3", req=2.0, ctx=float("nan"),
                                              turn_duration=None, end_timestamp=12345, model={"x": 1})]
        turns[3]["metering_usage"] = [None, 5, {"value": True, "unit": "credit"},
                                      {"value": 2.0, "unit": "credit"}, {"value": 1, "unit": "token"},
                                      {"value": 10 ** 400, "unit": "credit"}]
        self.write("a.json", session(turns))
        self.write("bad.json", "{not json")
        self.write("list.json", [1, 2, 3])
        rows, st = kca.load()
        self.assertEqual(len(rows), 2)
        self.assertEqual(st["bad_turns"], 2)
        self.assertEqual(st["skipped"], 2)
        odd = [r for r in rows if r["cr"] == 2.0][0]
        self.assertEqual((odd["cyc"], odd["req"], odd["ctx"], odd["dur"], odd["ts"]), (0, 2, None, 0, None))
        self.assertTrue(odd["inferred"])
        self.assertEqual(odd["model"], "claude-opus-5")

    def test_model_fallback_and_rate(self):
        self.write("a.json", session([turn(1.0, model=None), turn(1.0, ("p2", "a2"), model="new-model")],
                                     sess_model="new-model", rate=3.3))
        rows, _ = kca.load()
        by = {r["mult"]: r for r in rows}
        self.assertIn(3.3, by)  # unknown model falls back to the session's own multiplier
        self.assertTrue(all(r["model"] == "new-model" for r in rows))

    def test_offset_timestamp_is_bucketed_in_utc(self):
        self.write("a.json", session([turn(1.0, ("a", "b"), ts="2026-09-01T02:00:00+08:00")]))
        rows, _ = kca.load(tz_off=8)
        self.assertEqual((rows[0]["utc"], rows[0]["local"], rows[0]["hour"]),
                         ("2026-08-31", "2026-09-01", "09-01 02h"))

    def test_since_filters_and_counts_no_ts(self):
        self.write("a.json", session([turn(1.0, ("a", "b"), ts="2026-08-31T00:00:00Z"),
                                      turn(2.0, ("c", "d"), ts="2026-09-02T00:00:00Z"),
                                      turn(4.0, ("e", "f"), ts=None)]))
        rows, st = kca.load(since=kca.parse_ts("2026-09-01"))
        self.assertEqual([r["cr"] for r in rows], [2.0])
        self.assertEqual(st["no_ts"], 1)
        rows, st = kca.load()  # without --since the row is kept but still reported
        self.assertEqual(len(rows), 3)
        self.assertEqual(st["no_ts"], 1)
        out = self.run_main()
        self.assertIn("no_ts=1", out)
        self.assertIn("已计入总额", out)
        self.assertIn("credits=7.00", out)
        self.assertIn("其中 2 天有用量", out)          # the "?" bucket is not an active day
        self.assertIn("活跃日均：2 credits/活跃日", out)  # (1.0 + 2.0) / 2, timestamp-less 4.0 excluded
        self.assertRegex(out, r"\n  \?\s+1\s+4\.00")     # but it does show as a "?" row


class TestMain(Base):
    def test_empty_dir(self):
        out = self.run_main()
        self.assertIn("no sidecar turns found", out)
        os.environ["KIRO_SESSIONS_DIR"] = os.path.join(self.dir, "typo")
        out = self.run_main()
        self.assertIn("不是目录", out)
        os.environ["KIRO_SESSIONS_DIR"] = self.dir
        self.write("bad.json", "{truncated")
        out = self.run_main()
        self.assertIn("有 1 个文件无法解析为 JSON 对象", out)

    def test_zero_credits_does_not_crash(self):
        self.write("a.json", session([turn(0.0), turn(None, ("p2", "a2"))]))
        with open(os.path.join(self.dir, "a.jsonl"), "w") as fh:
            fh.write(json.dumps({"kind": "AssistantMessage", "data": {"message_id": "a1", "content": [
                {"kind": "toolUse", "data": {"name": "shell"}}]}}) + "\n")
        out = self.run_main("--tools")
        self.assertIn("credits=0.00", out)
        self.assertIn("计量总额为 0", out)

    def test_tz_label_and_boundary(self):
        self.write("a.json", session([turn(), turn(2.0, ("x", "y"), turn_duration={"secs": 10**9, "nanos": 0})]))
        out = self.run_main("--tz-offset", "-0")
        self.assertIn("tz=UTC+0", out)
        self.assertRegex(out, r">15min\s+turns=\s+1")
        out = self.run_main("--tz-offset", "-3.5")
        self.assertIn("tz=UTC-3.5", out)
        self.assertIn("UTC-3.5 为", out)
        out = self.run_main("--tz-offset", "8")
        self.assertIn("tz=UTC+8", out)
        self.assertNotIn("UTC+8.0", out)

    def test_since_after_month_start_is_labelled(self):
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.write("a.json", session([turn(1.0, ("a", "b"), ts=now_iso)]))
        with open(os.path.join(self.dir, "a.jsonl"), "w") as fh:
            fh.write(json.dumps({"kind": "AssistantMessage", "data": {"message_id": "a1", "content": [
                {"kind": "toolUse", "data": {"name": "shell"}}]}}) + "\n")
        out = self.run_main()
        self.assertIn("当前计费周期：1.00 credits", out)
        self.assertIn("估算整月", out)
        # --since one hour before now, i.e. after the month start: truncated section is labelled
        since = (datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = self.run_main("--since", since, "--tools")
        self.assertIn("自 --since", out)
        self.assertIn("不能与 /usage 直接对账", out)
        self.assertNotIn("估算整月", out)
        # --since in the future: no rows -> handled by the empty branch
        out = self.run_main("--since", "2999-01-01", "--tools")
        self.assertIn("no sidecar turns found", out)

    def test_glob_metacharacters_in_session_dir(self):
        odd = os.path.join(self.dir, "kiro [bak]")
        os.makedirs(odd)
        with open(os.path.join(odd, "s1.json"), "w") as fh:
            json.dump(session([turn(2.0)]), fh)
        os.environ["KIRO_SESSIONS_DIR"] = odd
        out = self.run_main()
        self.assertIn("credits=2.00", out)

    def test_timestamp_less_turn_does_not_steal_shared_message_id(self):
        self.write("a.json", session([turn(10.0, ("a", "b"), ts="2026-09-01T10:00:00Z"),
                                      turn(1.0, ("b", "c"), ts=None)]))
        with open(os.path.join(self.dir, "a.jsonl"), "w") as fh:
            fh.write(json.dumps({"kind": "AssistantMessage", "data": {"message_id": "b", "content": [
                {"kind": "toolUse", "data": {"name": "read"}}]}}) + "\n")
        out = self.run_main("--tools")
        self.assertRegex(out, r"read\s+1\s+10\.00")

    def test_tools_with_no_tool_turns_in_range(self):
        self.write("a.json", session([turn(1.0, ("a", "b"), ts="2026-08-01T00:00:00Z"),
                                      turn(1.0, ("c", "d"), ts="2026-09-02T00:00:00Z")]))
        with open(os.path.join(self.dir, "a.jsonl"), "w") as fh:
            fh.write(json.dumps({"kind": "AssistantMessage", "data": {"message_id": "b", "content": [
                {"kind": "toolUse", "data": {"name": "shell"}}]}}) + "\n")
        out = self.run_main("--tools", "--since", "2026-09-01")
        self.assertIn("含 1 次 toolUse", out)
        self.assertIn("都没有关联到工具调用（--since 过滤后常见）", out)
        self.assertNotIn("可能缺失、为空", out)
        # same situation without --since must not blame --since, and images still print
        with open(os.path.join(self.dir, "a.jsonl"), "a") as fh:
            fh.write(json.dumps({"kind": "Prompt", "data": {"message_id": "zz", "content": [
                {"kind": "image", "data": {"source": {"data": [1, 2, 3]}}}]}}) + "\n")
        self.write("a.json", session([turn(1.0, ("c", "d"), ts="2026-09-02T00:00:00Z")]))
        out = self.run_main("--tools")
        self.assertNotIn("--since", out.split("--- 工具调用归因")[1])
        self.assertIn("出现 1 个 image 项", out)

    def test_bad_args_exit(self):
        self.write("a.json", session([turn()]))
        for argv in (["--since", "nope"], ["--tz-offset", "99"], ["--bogus"]):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.run_main(*argv)

    def test_ctx_buckets_clamped(self):
        self.write("a.json", session([turn(1.0, ("a", "b"), ctx=100.0), turn(1.0, ("c", "d"), ctx=-5),
                                      turn(1.0, ("e", "f"), ctx=105.5), turn(1.0, ("g", "h"), ctx=True)]))
        out = self.run_main()
        self.assertIn("90-100%", out)
        self.assertNotIn("-10-", out)

    def test_unknown_model_shows_question_mark(self):
        self.write("a.json", session([turn(1.0, model="mystery")], sess_model="other", rate=None))
        out = self.run_main()
        self.assertIn("mystery x?", out)

    def test_tools_attribution_claims_each_message_once(self):
        # message "shared" appears in both turns; its tool calls must be credited once, to turn 1.
        self.write("a.json", session([
            turn(4.0, ("p1", "shared"), ts="2026-09-01T10:00:00Z"),
            turn(2.0, ("shared", "a2"), ts="2026-09-01T11:00:00Z")]))
        lines = [
            {"kind": "AssistantMessage", "data": {"message_id": "shared", "content": [
                {"kind": "toolUse", "data": {"name": "shell"}}, {"kind": "toolUse", "data": {"name": "read"}}]}},
            {"kind": "AssistantMessage", "data": {"message_id": "a2", "content": [
                {"kind": "toolUse", "data": {"name": "write"}}]}},
            {"kind": "Prompt", "data": {"message_id": "p1", "content": [
                {"kind": "image", "data": {"source": {"data": [1, 2, 3]}}}]}},
            {"kind": "ToolResults", "data": {"message_id": "t1", "content": [
                {"kind": "toolResult", "data": {"content": [
                    {"kind": "image", "data": {"source": {"data": [1, 2, 3]}}},
                    {"kind": "image", "data": {"source": {"data": "AAEC"}}},
                    {"kind": "image", "data": {"source": {"data": "https://example.com/x.png"}}}]}},
                {"kind": "toolResult", "data": {"content": 42}}]}},
        ]
        with open(os.path.join(self.dir, "a.jsonl"), "w") as fh:
            fh.write("".join(json.dumps(l) + "\n" for l in lines))
        # orphan: transcript with assistant messages but no turn metadata in its .json
        self.write("orphan.json", session([]))
        with open(os.path.join(self.dir, "orphan.jsonl"), "w") as fh:
            fh.write(json.dumps({"kind": "AssistantMessage", "data": {"message_id": "o1", "content": []}}) + "\n")
        out = self.run_main("--tools")
        self.assertIn("toolUse 3/3 次已归到具体轮次", out)
        # orphan count must not depend on --since filtering
        out2 = self.run_main("--tools", "--since", "2026-09-01T10:30:00Z")
        self.assertIn("另有 1 个会话", out2)
        self.assertIn("另有 1 个会话", out)
        # shell and read each get 4.0/2 = 2.0 from turn 1; write gets 2.0 from turn 2
        self.assertRegex(out, r"shell\s+1\s+2\.00")
        self.assertRegex(out, r"write\s+1\s+2\.00")
        self.assertIn("出现 3 个 image 项", out)
        self.assertIn("涉及 2 张不同图片", out)


if __name__ == "__main__":
    unittest.main()
