"""The paced gh shim: costs, redaction, shapes, the log, its summary and passthrough.

No test runs a real `gh`. The in-process tests fake the pacer at
`gh._pacer`; the end-to-end shim tests put a stub `gh` executable on a
hermetic PATH and run the shim module in a child interpreter.
"""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import corral
from corral import cli
from corral.github import gh, gh_shim, ghshim

#: Directory holding the `corral` package, for the child interpreter.
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(corral.__file__)))


class FakePacer:
    """Stands in for the machine-wide GraphQL pacer at the gh seam."""

    def __init__(self, error=None):
        self.error = error
        self.acquired = []
        self.cooldowns = []

    def acquire(self, cost=gh.GRAPHQL_CALL_COST):
        if self.error is not None:
            raise self.error
        self.acquired.append(cost)

    def cooldown(self, seconds):
        self.cooldowns.append(seconds)


class CostTest(unittest.TestCase):
    def test_reads_cost_one(self):
        self.assertEqual(ghshim.cost_for(["issue", "list"]), 1.0)
        self.assertEqual(ghshim.cost_for(["pr", "view"]), 1.0)
        self.assertEqual(ghshim.cost_for(["search", "repos"]), 1.0)

    def test_writes_cost_five(self):
        self.assertEqual(ghshim.cost_for(["pr", "create"]), 5.0)
        self.assertEqual(ghshim.cost_for(["issue", "close"]), 5.0)
        self.assertEqual(ghshim.cost_for(["api", "-X", "POST", "repos/x"]), 5.0)

    def test_graphql_costs_match_the_library_split(self):
        self.assertEqual(ghshim.cost_for(["api", "graphql"]), gh.GRAPHQL_CALL_COST)
        self.assertEqual(
            ghshim.cost_for(["api", "graphql", "-f", "query=mutation { x }"]),
            gh.GRAPHQL_MUTATION_COST,
        )

    def test_local_commands_are_not_paced(self):
        self.assertIsNone(ghshim.cost_for([]))
        self.assertIsNone(ghshim.cost_for(["--version"]))
        self.assertIsNone(ghshim.cost_for(["auth", "status"]))
        self.assertIsNone(ghshim.cost_for(["config", "get"]))
        # API-backed subcommands of the same entry points are still paced.
        self.assertEqual(ghshim.cost_for(["auth", "login"]), 1.0)
        self.assertEqual(ghshim.cost_for(["extension", "install"]), 5.0)

    def test_rest_method_flag_decides(self):
        self.assertEqual(ghshim.cost_for(["api", "repos/x", "--method", "GET"]), 1.0)


class RedactionTest(unittest.TestCase):
    def test_only_two_words_survive(self):
        self.assertEqual(
            ghshim.short_argv(["pr", "create", "--body", "secret-body", "--token", "hunter2"]),
            ["pr", "create"],
        )

    def test_appended_line_carries_no_tokens_or_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gh-calls.jsonl")
            ghshim.append_call(
                path,
                caller="a@h",
                argv=["issue", "create", "--body", "s3cret", "--title", "t"],
                exit_code=0,
                duration_s=0.1,
                throttled=False,
            )
            with open(path, encoding="utf-8") as handle:
                rec = json.loads(handle.read())
            self.assertEqual(rec["argv"], ["issue", "create"])
            self.assertNotIn("s3cret", json.dumps(rec))
            self.assertEqual(rec["caller"], "a@h")
            self.assertEqual(rec["exit"], 0)
            self.assertFalse(rec["throttled"])


class RotationTest(unittest.TestCase):
    def test_log_moves_to_dot_one_past_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ghshim, "ROTATE_BYTES", 10):
            path = os.path.join(tmp, "gh-calls.jsonl")
            ghshim.append_call(
                path, caller="a", argv=["pr", "list"], exit_code=0, duration_s=0.1, throttled=False
            )
            ghshim.append_call(
                path, caller="b", argv=["pr", "view"], exit_code=0, duration_s=0.1, throttled=False
            )
            with open(path + ".1", encoding="utf-8") as handle:
                self.assertEqual(json.loads(handle.readline())["caller"], "a")
            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
            self.assertEqual([json.loads(line)["caller"] for line in lines], ["b"])


class ShapeTest(unittest.TestCase):
    def _gql(self, query, *extra):
        return ["api", "graphql", "-f", "query=" + query, *extra]

    def test_board_page_is_user(self):
        self.assertEqual(
            ghshim.shape_for(
                self._gql(
                    "query($o:String!){ user(login:$o){ projectV2(number:1){"
                    " items(first:100){ nodes { id } } } } }"
                )
            ),
            "user",
        )

    def test_item_query_names_both_roots_sorted(self):
        self.assertEqual(
            ghshim.shape_for(
                self._gql(
                    "query($o:String!){ user(login:$o){ id }"
                    " repository(owner:$o,name:$r){ issue(number:1){ id } } }"
                )
            ),
            "repository+user",
        )

    def test_mutation_carries_its_field(self):
        self.assertEqual(
            ghshim.shape_for(
                self._gql(
                    "mutation($p:ID!){ addProjectV2ItemById(input:{projectId:$p}){ item { id } } }"
                )
            ),
            "mutation:addProjectV2ItemById",
        )

    def test_non_graphql_and_missing_query_are_none(self):
        self.assertIsNone(ghshim.shape_for(["pr", "list"]))
        self.assertIsNone(ghshim.shape_for(["api", "graphql"]))
        self.assertIsNone(ghshim.shape_for(["api", "graphql", "-f", "owner=x"]))

    def test_strings_and_comments_do_not_pollute_roots(self):
        self.assertEqual(
            ghshim.shape_for(
                self._gql('query { user(login:"weird { brace") { id } } # trailing { comment')
            ),
            "user",
        )

    def test_raw_field_and_attached_forms(self):
        argv = ["api", "graphql", "--raw-field", "query={ node(id:$i){ id } }"]
        self.assertEqual(ghshim.shape_for(argv), "node")
        argv = ["api", "graphql", "-fquery={ node(id:$i){ id } }"]
        self.assertEqual(ghshim.shape_for(argv), "node")

    def test_appended_line_carries_shape_but_no_query_text(self):
        query = "query($o:String!){ user(login:$o){ projectV2(number:1) { id } } }"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gh-calls.jsonl")
            ghshim.append_call(
                path,
                caller="a@h",
                argv=self._gql(query, "-F", "owner=x"),
                exit_code=0,
                duration_s=0.1,
                throttled=False,
            )
            with open(path, encoding="utf-8") as handle:
                rec = json.loads(handle.read())
            self.assertEqual(rec["shape"], "user")
            self.assertNotIn("projectV2(number:1)", json.dumps(rec))

    def test_summarize_splits_shapes_and_marks_legacy_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gh-calls.jsonl")
            base = 1_700_000_000.0
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "ts": base + 1,
                            "caller": "a@h",
                            "argv": ["api", "graphql"],
                            "shape": "user",
                            "exit": 0,
                            "duration_s": 0.2,
                            "throttled": False,
                        }
                    )
                    + "\n"
                )
                handle.write(
                    json.dumps(
                        {
                            "ts": base + 2,
                            "caller": "a@h",
                            "argv": ["api", "graphql"],
                            "exit": 0,
                            "duration_s": 0.2,
                            "throttled": False,
                        }
                    )
                    + "\n"
                )
                handle.write(
                    json.dumps(
                        {
                            "ts": base + 3,
                            "caller": "a@h",
                            "argv": ["pr", "list"],
                            "exit": 0,
                            "duration_s": 0.2,
                            "throttled": False,
                        }
                    )
                    + "\n"
                )
            summary = ghshim.summarize(path)
            self.assertEqual(summary["by_shape"], {"user": 1, "unknown": 1})
            self.assertIn("shape user 1", ghshim.render(summary))

    def test_alias_never_leaks_into_shape(self):
        self.assertEqual(
            ghshim.shape_for(self._gql("query($o:String!){ me: user(login:$o){ id } }")),
            "user",
        )

    def test_named_fragment_spread_is_not_a_shape(self):
        # The spread name is caller-chosen; resolving the fragment body is
        # out of scope, so the token stays absent (unknown in the summary)
        # rather than wrong.
        self.assertIsNone(
            ghshim.shape_for(
                self._gql("query { ...Board } fragment Board on Query { user(login:$o){ id } }")
            )
        )

    def test_leading_comment_keeps_mutation_prefix(self):
        self.assertEqual(
            ghshim.shape_for(
                self._gql(
                    "# board write\nmutation($p:ID!){"
                    " addProjectV2ItemById(input:{projectId:$p})"
                    "{ item { id } } }"
                )
            ),
            "mutation:addProjectV2ItemById",
        )

    def test_summarize_defaults_to_the_live_log(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CORRAL_STATE": tmp}),
        ):
            ghshim.append_call(
                caller="a@h",
                argv=self._gql("query{ user{ id } }"),
                exit_code=0,
                duration_s=0.1,
                throttled=False,
            )
            summary = ghshim.summarize()
            self.assertEqual(summary["calls"], 1)
            self.assertEqual(summary["by_shape"], {"user": 1})


class WatchesStdoutTest(unittest.TestCase):
    def test_only_graphql_watches_stdout(self):
        self.assertTrue(ghshim.watches_stdout(["api", "graphql"]))
        self.assertFalse(ghshim.watches_stdout(["pr", "list"]))
        self.assertFalse(ghshim.watches_stdout(["api", "repos/x"]))
        self.assertFalse(ghshim.watches_stdout([]))


class CallerTest(unittest.TestCase):
    def test_agent_id_names_the_caller(self):
        self.assertEqual(ghshim.caller_name({"CORRAL_AGENT_ID": " agent-1 "}), "agent-1")

    def test_missing_agent_id_is_unknown(self):
        self.assertEqual(ghshim.caller_name({}), "unknown")


class ParseSinceTest(unittest.TestCase):
    def test_units(self):
        now = 1_000_000.0
        self.assertEqual(ghshim.parse_since("1h", now=now), now - 3600.0)
        self.assertEqual(ghshim.parse_since("30m", now=now), now - 1800.0)
        self.assertEqual(ghshim.parse_since("7d", now=now), now - 604800.0)
        self.assertEqual(ghshim.parse_since("45", now=now), now - 45.0)
        self.assertIsNone(ghshim.parse_since(None))
        self.assertIsNone(ghshim.parse_since(""))

    def test_rejects_garbage(self):
        for bad in ("yesterday", "1y", "--since", "h"):
            with self.assertRaises(ValueError, msg=bad):
                ghshim.parse_since(bad)


class SummarizeTest(unittest.TestCase):
    def _write(self, path, recs):
        with open(path, "a", encoding="utf-8") as handle:
            for rec in recs:
                handle.write(json.dumps(rec) + "\n")
            handle.write("not json\n")

    def test_per_caller_per_minute_and_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gh-calls.jsonl")
            base = 1_700_000_000.0
            self._write(
                path,
                [
                    {
                        "ts": base + 1,
                        "caller": "a@h",
                        "argv": ["pr", "list"],
                        "exit": 0,
                        "duration_s": 0.2,
                        "throttled": False,
                    },
                    {
                        "ts": base + 2,
                        "caller": "a@h",
                        "argv": ["pr", "list"],
                        "exit": 0,
                        "duration_s": 0.2,
                        "throttled": True,
                    },
                    {
                        "ts": base + 61,
                        "caller": "b@h",
                        "argv": ["api", "graphql"],
                        "exit": 1,
                        "duration_s": 1.0,
                        "throttled": False,
                    },
                ],
            )
            summary = ghshim.summarize(path)
            # The corrupt tail line is skipped, not fatal.
            self.assertEqual(summary["calls"], 3)
            self.assertEqual(summary["throttled"], 1)
            self.assertEqual(summary["by_caller"], {"a@h": 2, "b@h": 1})
            self.assertEqual(sum(r["calls"] for r in summary["by_minute"]), 3)
            text = ghshim.render(summary)
            self.assertIn("3 calls, 1 throttled", text)
            self.assertIn("a@h 2", text)

    def test_since_filters_old_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gh-calls.jsonl")
            now = time.time()
            self._write(
                path,
                [
                    {
                        "ts": now - 7200,
                        "caller": "a@h",
                        "argv": ["x"],
                        "exit": 0,
                        "duration_s": 0.1,
                        "throttled": False,
                    },
                    {
                        "ts": now - 60,
                        "caller": "a@h",
                        "argv": ["x"],
                        "exit": 0,
                        "duration_s": 0.1,
                        "throttled": False,
                    },
                ],
            )
            summary = ghshim.summarize(path, since=ghshim.parse_since("1h", now=now))
            self.assertEqual(summary["calls"], 1)

    def test_missing_log_summarizes_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = ghshim.summarize(os.path.join(tmp, "missing.jsonl"))
            self.assertEqual(
                summary,
                {
                    "calls": 0,
                    "throttled": 0,
                    "since": None,
                    "by_minute": [],
                    "by_caller": {},
                    "by_shape": {},
                },
            )


class LogPathTest(unittest.TestCase):
    def test_log_lives_in_the_state_dir(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CORRAL_STATE": tmp}),
        ):
            self.assertEqual(ghshim.log_path(), os.path.join(tmp, "gh-calls.jsonl"))
        self.assertEqual(
            ghshim.log_path(os.path.join("x", "y")), os.path.join("x", "y", "gh-calls.jsonl")
        )


class PacerWiringTest(unittest.TestCase):
    def test_before_charges_the_cost_without_a_cap(self):
        pacer = FakePacer()
        with mock.patch.object(gh, "_pacer", return_value=pacer):
            _, capped = ghshim.pace_before(3.0)
        self.assertFalse(capped)
        self.assertEqual(pacer.acquired, [3.0])

    def test_before_caps_instead_of_raising(self):
        pacer = FakePacer(error=gh.GhError("unavailable", "x"))
        with mock.patch.object(gh, "_pacer", return_value=pacer):
            waited, capped = ghshim.pace_before(1.0)
        self.assertTrue(capped)
        self.assertGreaterEqual(waited, 0.0)

    def test_after_cools_down_on_a_secondary_limit(self):
        pacer = FakePacer()
        with mock.patch.object(gh, "_pacer", return_value=pacer):
            self.assertTrue(
                ghshim.pace_after(
                    "HTTP 403: You have exceeded a secondary rate limit. Please wait a few minutes."
                )
            )
        self.assertEqual(pacer.cooldowns, [gh.GRAPHQL_COOLDOWN_S])

    def test_after_ignores_clean_stderr(self):
        pacer = FakePacer()
        with mock.patch.object(gh, "_pacer", return_value=pacer):
            self.assertFalse(ghshim.pace_after(""))
            self.assertFalse(ghshim.pace_after("Created the pull request"))
        self.assertEqual(pacer.cooldowns, [])


class FindOnPathTest(unittest.TestCase):
    """The shim finds the real gh without finding itself, using fake executables."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.shimdir = os.path.join(self.root, "shim")
        self.bindir = os.path.join(self.root, "bin")
        os.makedirs(self.shimdir)
        os.makedirs(self.bindir)
        self._fake(self.shimdir)
        self._fake(self.bindir)

    def _fake(self, directory):
        path = os.path.join(directory, "gh")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_skips_the_shim_directory(self):
        found = gh_shim.find_on_path(
            "gh", self.shimdir, path=f"{self.shimdir}{os.pathsep}{self.bindir}"
        )
        self.assertEqual(found, os.path.join(self.bindir, "gh"))

    def test_skips_a_symlink_to_the_shim_directory(self):
        alias = os.path.join(self.root, "alias")
        os.symlink(self.shimdir, alias)
        found = gh_shim.find_on_path("gh", self.shimdir, path=f"{alias}{os.pathsep}{self.bindir}")
        self.assertEqual(found, os.path.join(self.bindir, "gh"))

    def test_skips_a_non_executable_candidate(self):
        plain = os.path.join(self.root, "plain")
        os.makedirs(plain)
        with open(os.path.join(plain, "gh"), "w", encoding="utf-8") as handle:
            handle.write("not run\n")
        found = gh_shim.find_on_path(
            "gh", self.shimdir, path=f"{self.shimdir}{os.pathsep}{plain}{os.pathsep}{self.bindir}"
        )
        self.assertEqual(found, os.path.join(self.bindir, "gh"))

    def test_none_when_only_the_shim_is_on_path(self):
        self.assertIsNone(gh_shim.find_on_path("gh", self.shimdir, path=self.shimdir))


class ShimScriptTest(unittest.TestCase):
    """The installed wrapper runs the shim, which passes everything through, paced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.shimdir = os.path.join(self.tmp.name, "shim")
        self.bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bindir)
        self.wrapper = ghshim.install_shim(self.shimdir)
        self.env = dict(
            os.environ,
            PATH=os.pathsep.join([self.shimdir, self.bindir]),
            PYTHONPATH=SRC_DIR,
            CORRAL_STATE=self.tmp.name,
            CORRAL_AGENT_ID="shim-test@h",
        )
        self.env.pop(gh.PACED_MARKER, None)

    def _stub(self, body):
        path = os.path.join(self.bindir, "gh")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def _shim(self, *argv, env=None):
        return subprocess.run(
            [self.wrapper] + list(argv),
            capture_output=True,
            env=self.env if env is None else env,
            timeout=120,
        )

    def _log(self):
        with open(os.path.join(self.tmp.name, "gh-calls.jsonl"), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _cooldown(self):
        with mock.patch.dict(os.environ, {"CORRAL_STATE": self.tmp.name}):
            return gh.pacer_cooldown_remaining()

    def test_stdout_stderr_and_exit_pass_through(self):
        self._stub('#!/bin/sh\necho "out-$1-$2"\necho "err-$1" >&2\nexit 3\n')
        proc = self._shim("pr", "create", "--body", "top-secret", "--token", "hunter2")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout, b"out-pr-create\n")
        self.assertEqual(proc.stderr, b"err-pr\n")
        (rec,) = self._log()
        self.assertEqual(rec["argv"], ["pr", "create"])
        self.assertEqual(rec["caller"], "shim-test@h")
        self.assertEqual(rec["exit"], 3)
        self.assertFalse(rec["throttled"])
        self.assertNotIn("top-secret", json.dumps(rec))
        self.assertNotIn("hunter2", json.dumps(rec))

    def test_secondary_limit_cools_down_the_shared_pacer(self):
        self._stub(
            "#!/bin/sh\necho 'HTTP 403: You have exceeded a "
            "secondary rate limit. Please wait a few minutes.' >&2\n"
            "exit 1\n"
        )
        proc = self._shim("pr", "list")
        self.assertEqual(proc.returncode, 1)
        (rec,) = self._log()
        self.assertTrue(rec["throttled"])
        self.assertGreater(self._cooldown(), 0.0)

    def test_local_commands_skip_the_bucket_but_still_log(self):
        self._stub("#!/bin/sh\necho ok\nexit 0\n")
        proc = self._shim("auth", "status")
        self.assertEqual(proc.returncode, 0)
        (rec,) = self._log()
        self.assertEqual(rec["argv"], ["auth", "status"])
        # No bucket file: the pacer was never opened for a local command.
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "graphql-pacing.json")))
        self.assertEqual(self._cooldown(), 0.0)

    def test_graphql_stdout_errors_cool_down_the_pacer(self):
        self._stub(
            '#!/bin/sh\necho \'{"errors": [{"type": '
            '"RATE_LIMITED", "message": "rate limit exceeded"}]}\'\n'
            "exit 0\n"
        )
        proc = self._shim("api", "graphql", "-f", "query={ x }")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"RATE_LIMITED", proc.stdout)
        (rec,) = self._log()
        self.assertTrue(rec["throttled"])
        self.assertGreater(self._cooldown(), 0.0)

    def test_signal_death_is_not_remapped_to_an_exit_code(self):
        self._stub("#!/bin/sh\nkill -TERM $$\n")
        proc = self._shim("pr", "list")
        self.assertEqual(proc.returncode, -15)

    def test_broken_pacing_still_runs_gh(self):
        # Real failure, not a mock: the state dir is a regular file, so
        # every bucket operation fails inside the child. The call must
        # still run, with output and exit code intact and no traceback.
        self._stub("#!/bin/sh\necho ok\necho oops >&2\nexit 7\n")
        block = os.path.join(self.tmp.name, "blocked")
        with open(block, "w", encoding="utf-8") as handle:
            handle.write("not a dir\n")
        env = dict(self.env, CORRAL_STATE=block)
        proc = self._shim("pr", "list", env=env)
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(proc.stdout, b"ok\n")
        self.assertEqual(proc.stderr, b"oops\n")

    def test_library_paced_calls_skip_charge_and_cooldown(self):
        # `gh.run(paced=True)` children carry the marker: the shim logs
        # them flagged but never charges the bucket or cools it down, even
        # on a rate-limit answer (the library already did both).
        self._stub(
            "#!/bin/sh\necho 'HTTP 403: You have exceeded a "
            "secondary rate limit. Please wait a few minutes.' >&2\n"
            "exit 1\n"
        )
        env = dict(self.env, **{gh.PACED_MARKER: "1"})
        proc = self._shim("pr", "list", env=env)
        self.assertEqual(proc.returncode, 1)
        (rec,) = self._log()
        self.assertTrue(rec["paced"])
        self.assertFalse(rec["throttled"])
        self.assertEqual(self._cooldown(), 0.0)

    def test_missing_caller_is_logged_as_unknown(self):
        self._stub("#!/bin/sh\nexit 0\n")
        env = {key: value for key, value in self.env.items() if key != "CORRAL_AGENT_ID"}
        proc = self._shim("pr", "list", env=env)
        self.assertEqual(proc.returncode, 0)
        (rec,) = self._log()
        self.assertEqual(rec["caller"], "unknown")

    def test_no_real_gh_reads_as_127(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        env = dict(self.env, PATH=os.pathsep.join([self.shimdir, empty]))
        proc = self._shim("pr", "list", env=env)
        self.assertEqual(proc.returncode, 127)

    def test_missing_shim_dir_is_a_usage_error(self):
        # The wrapper always sets the variable, so run the module directly.
        env = {key: value for key, value in self.env.items() if key != ghshim.SHIM_DIR_ENV}
        proc = subprocess.run(
            [sys.executable, "-P", "-m", "corral.github.gh_shim", "pr", "list"],
            capture_output=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 4)
        self.assertIn(ghshim.SHIM_DIR_ENV.encode(), proc.stderr)

    def test_install_writes_an_executable_wrapper_in_the_shim_dir(self):
        self.assertEqual(self.wrapper, os.path.join(self.shimdir, "gh"))
        self.assertTrue(os.access(self.wrapper, os.X_OK))
        with open(self.wrapper, encoding="utf-8") as handle:
            body = handle.read()
        self.assertTrue(body.startswith("#!/bin/sh\n"))
        self.assertIn("-m corral.github.gh_shim", body)


class GhCallsCommandTest(unittest.TestCase):
    def _state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CORRAL_STATE": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        return tmp.name

    def test_text_summary(self):
        self._state()
        ghshim.append_call(
            caller="a@h", argv=["pr", "list"], exit_code=0, duration_s=0.2, throttled=False
        )
        ghshim.append_call(
            caller="b@h", argv=["pr", "create"], exit_code=1, duration_s=0.4, throttled=True
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["gh-calls"])
        self.assertEqual(code, 0)
        self.assertIn("2 calls, 1 throttled", out.getvalue())
        self.assertIn("a@h 1", out.getvalue())

    def test_json_summary(self):
        self._state()
        ghshim.append_call(
            caller="a@h", argv=["pr", "list"], exit_code=0, duration_s=0.2, throttled=False
        )
        ghshim.append_call(
            caller="b@h", argv=["pr", "create"], exit_code=1, duration_s=0.4, throttled=True
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["gh-calls", "--json"])
        record = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(record["calls"], 2)
        self.assertEqual(record["by_caller"], {"a@h": 1, "b@h": 1})

    def test_by_shape_lists_shapes_without_minute_rows(self):
        self._state()
        board = (
            "query($o:String!){ user(login:$o){ projectV2(number:1)"
            "{ items(first:100){ nodes { id } } } } }"
        )
        item = "query($i:ID!){ node(id:$i){ id } }"
        ghshim.append_call(
            caller="a@h",
            argv=["api", "graphql", "-f", f"query={board}"],
            exit_code=0,
            duration_s=0.2,
            throttled=False,
        )
        ghshim.append_call(
            caller="a@h",
            argv=["api", "graphql", "-f", f"query={board}"],
            exit_code=0,
            duration_s=0.2,
            throttled=False,
        )
        ghshim.append_call(
            caller="b@h",
            argv=["api", "graphql", "-f", f"query={item}"],
            exit_code=0,
            duration_s=0.2,
            throttled=False,
        )
        ghshim.append_call(
            caller="b@h", argv=["pr", "view", "12"], exit_code=0, duration_s=0.2, throttled=False
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["gh-calls", "--by-shape"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("4 calls, 0 throttled", text)
        self.assertIn("shape user 2", text)
        self.assertIn("shape node 1", text)
        # Per-minute rows stay in the default view, not here.
        self.assertNotIn("a@h 2", text)
        self.assertNotIn("b@h 2", text)

    def test_since_filters_the_summary(self):
        self._state()
        ghshim.append_call(
            caller="a@h", argv=["pr", "list"], exit_code=0, duration_s=0.2, throttled=False
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["gh-calls", "--since", "1h"])
        self.assertEqual(code, 0)
        self.assertIn("1 calls, 0 throttled", out.getvalue())

    def test_bad_since_is_a_usage_error(self):
        self._state()
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = cli.main(["gh-calls", "--since", "yesterday"])
        self.assertEqual(code, 4)
        self.assertIn("bad --since", err.getvalue())


if __name__ == "__main__":
    unittest.main()
