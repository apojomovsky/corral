"""`korapy github setup`: labels, the board ensure, the mapping write.

Fake entry, tracker and provisioner seams only. No test runs a real
`gh`, touches the network, or reaches a real board; importing
``tests.test_board_fields`` installs the suite-wide tripwire.
"""

import argparse
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from korapy import cli, githubcli
from korapy import tracker as iface
from korapy import trackers
from tests.test_init import FakeGit
from tests.test_tracker import FakeTracker

TOML = """\
[[repo]]
name = "app"
tracker.repo = "acme/app"
"""

TWO = """\
[[repo]]
name = "app"
tracker.repo = "acme/app"

[[repo]]
name = "lib"
tracker.repo = "acme/lib"
"""

BOARD_BOOTSTRAP = """\
[[repo]]
name = "app"
tracker.repo = "acme/app"
tracker.variant = "board"
"""

TABLED = """\
[[repo]]
name = "app"
tracker.repo = "acme/app"
tracker.variant = "board"

[repo.board]
project_owner = "acme"
project_number = 3
"""

MATCHING_TABLED = """\
[[repo]]
name = "app"
tracker.repo = "acme/app"
tracker.variant = "board"

[repo.board]
project_owner = "acme"
project_number = 3
agent_field = "Agent"
status_field = "Agent state"
status_column = "Status"
priority_column = "Priority"
blocked_by_column = "Blocked by"

[repo.board.priority_map]
"opt-1" = "urgent"
"opt-2" = "high"
"""

NAMES = [label.name for label in iface.BOOTSTRAP_LABELS]


def entry(name="app", repo="acme/app", variant="labels", board=None):
    """One setup entry as plain data, the way ``setup_entry`` resolves it."""
    return trackers.SetupEntry(
        name=name,
        tracker_repo=repo,
        operators=["acme"],
        variant=variant,
        board=board,
        max_pages=20,
    )


def entry_factory(entries):
    """An entry factory over a ``{name: SetupEntry}`` mapping."""

    def get(name):
        try:
            return entries[name]
        except KeyError:
            raise ValueError(f"unknown repo {name!r}") from None

    return get


def tracker_factory(mapping):
    """A tracker factory over a ``{name: tracker}`` mapping."""

    def get(resolved):
        try:
            return mapping[resolved.name]
        except KeyError:
            raise ValueError(f"no tracker for {resolved.tracker_repo}") from None

    return get


def standard_fields():
    """The five always-provisioned fields as live spec data."""
    return (
        iface.FieldSpec("agent", "Agent", "text", ()),
        iface.FieldSpec("status", "Agent state", "text", ()),
        iface.FieldSpec("board_status", "Status", "single_select", ("Todo", "In Progress", "Done")),
        iface.FieldSpec(
            "priority",
            "Priority",
            "single_select",
            ("Urgent", "High", "Normal", "Low", "Backlog"),
        ),
        iface.FieldSpec("blocked_by", "Blocked by", "text", ()),
    )


def provision(created=(), kept=(), refused=(), number=3, project_id="PVT_1", fields=None):
    """One board provision report carrying the standard live fields."""
    return iface.BoardProvision(
        owner="acme",
        number=number,
        project_id=project_id,
        fields=standard_fields() if fields is None else tuple(fields),
        created=tuple(created),
        kept=tuple(kept),
        refused=tuple(refused),
        priority_map={"opt-1": "urgent", "opt-2": "high"},
    )


def matching_board(**over):
    """A ``BoardConfig`` matching what ``provision()`` renders, unless overridden."""
    from tests.test_board_planner import table

    base = {
        "project_owner": "acme",
        "project_number": 3,
        "agent_field": "Agent",
        "status_field": "Agent state",
        "status_column": "Status",
        "priority_column": "Priority",
        "blocked_by_column": "Blocked by",
        "priority_map": {"opt-1": "urgent", "opt-2": "high"},
    }
    base.update(over)
    return table(**base)


def populated(added=(), kept=(), refused=()):
    """One populate report."""
    return iface.BoardPopulate(tuple(added), tuple(kept), tuple(refused))


class FakeProvisioner:
    """A provisioner double: programmed reports, no board contact."""

    def __init__(
        self,
        provision=None,
        *,
        ensure_error=None,
        dry_report=None,
        populate_report=None,
        populate_error=None,
    ):
        self.calls = []
        self.provision = provision
        self.ensure_error = ensure_error
        self.dry_report = dry_report
        self.populate_report = populate_report
        self.populate_error = populate_error

    def ensure_board(self, spec):
        self.calls.append(("ensure_board", spec))
        if self.ensure_error is not None:
            raise self.ensure_error
        return self.provision

    def dry_run(self, spec):
        self.calls.append(("dry_run", spec))
        if self.ensure_error is not None:
            raise self.ensure_error
        return self.dry_report if self.dry_report is not None else self.provision

    def populate(self, project_id, repo):
        self.calls.append(("populate", project_id, repo))
        if self.populate_error is not None:
            raise self.populate_error
        return self.populate_report

    def preview_populate(self, project_id, repo):
        self.calls.append(("preview_populate", project_id, repo))
        if self.populate_error is not None:
            raise self.populate_error
        return self.populate_report


def refuse_provisioner():
    """A provisioner factory that fails the test when called."""

    def get():
        raise AssertionError("provisioner must not be built for a labels-only run")

    return get


class SetupRootCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-githubcli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TOML)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_setup(self, entries, mapping, factory, names=(), **flags):
        with contextlib.redirect_stdout(io.StringIO()):
            return githubcli.setup_root(
                self.root,
                list(names),
                entry_factory=entry_factory(entries),
                tracker_factory=tracker_factory(mapping),
                provisioner_factory=factory,
                out=self.out,
                err=self.err,
                **flags,
            )

    def toml_text(self):
        with open(os.path.join(self.root, "korapy.toml"), encoding="utf-8") as handle:
            return handle.read()


class ExitCodeTest(SetupRootCase):
    def test_a_fresh_repo_creates_everything_and_exits_0(self):
        fake = FakeTracker()
        code = self.run_setup({"app": entry()}, {"app": fake}, refuse_provisioner())
        self.assertEqual(code, 0)
        self.assertIn("created label ready on acme/app\n", self.out.getvalue())
        self.assertIn("created label priority:backlog on acme/app\n", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_a_rerun_keeps_everything_and_exits_1(self):
        fake = FakeTracker()
        self.assertEqual(self.run_setup({"app": entry()}, {"app": fake}, refuse_provisioner()), 0)
        self.out = io.StringIO()
        code = self.run_setup({"app": entry()}, {"app": fake}, refuse_provisioner())
        self.assertEqual(code, 1)
        self.assertIn(f"kept {len(NAMES)} labels on acme/app\n", self.out.getvalue())
        self.assertNotIn("created label", self.out.getvalue())

    def test_refused_labels_are_exit_1_with_a_fix_line(self):
        fake = FakeTracker(refused=set(NAMES))
        code = self.run_setup({"app": entry()}, {"app": fake}, refuse_provisioner())
        self.assertEqual(code, 1)
        out = self.out.getvalue()
        self.assertNotIn("created label", out)
        self.assertIn(
            "refused label hold on acme/app: ask an owner to create it,"
            " or move the board to the ops side\n",
            out,
        )

    def test_created_plus_refused_is_still_1(self):
        fake = FakeTracker(refused={"hold"})
        code = self.run_setup({"app": entry()}, {"app": fake}, refuse_provisioner())
        self.assertEqual(code, 1)
        out = self.out.getvalue()
        self.assertIn("created label ready on acme/app\n", out)
        self.assertIn("refused label hold on acme/app", out)

    def test_an_unreachable_tracker_is_exit_3(self):
        fake = FakeTracker(ensure_error=iface.TrackerError("connection refused"))
        code = self.run_setup({"app": entry()}, {"app": fake}, refuse_provisioner())
        self.assertEqual(code, 3)
        self.assertIn("acme/app", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_a_rejected_label_create_is_exit_1_with_the_reason(self):
        fake = FakeTracker(
            ensure_error=iface.TrackerRejected("tracker rejected: Validation Failed (HTTP 422)")
        )
        code = self.run_setup({"app": entry()}, {"app": fake}, refuse_provisioner())
        self.assertEqual(code, 1)
        self.assertIn("tracker rejected", self.err.getvalue())
        self.assertIn("acme/app", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_partial_failure_with_a_create_is_3(self):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO)
        entries = {"app": entry(), "lib": entry(name="lib", repo="acme/lib")}
        good = FakeTracker()
        bad = FakeTracker(ensure_error=iface.TrackerError("down"))
        code = self.run_setup(entries, {"app": good, "lib": bad}, refuse_provisioner())
        self.assertEqual(code, 3)
        self.assertIn("created label ready on acme/app\n", self.out.getvalue())
        self.assertIn("acme/lib", self.err.getvalue())

    def test_only_a_subset_runs_when_named(self):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO)
        entries = {"app": entry(), "lib": entry(name="lib", repo="acme/lib")}
        app = FakeTracker()
        lib = FakeTracker()
        code = self.run_setup(
            entries, {"app": app, "lib": lib}, refuse_provisioner(), names=["lib"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(app.calls, [])
        self.assertIn(("ensure_labels", "acme/lib"), lib.calls)

    def test_an_unknown_repo_is_exit_4(self):
        fake = FakeTracker()
        code = self.run_setup({"app": entry()}, {"app": fake}, refuse_provisioner(), names=["nope"])
        self.assertEqual(code, 4)
        self.assertIn("nope", self.err.getvalue())
        self.assertEqual(fake.calls, [])

    def test_no_entries_is_exit_4(self):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[project]\nname = "demo"\n')
        fake = FakeTracker()
        code = self.run_setup({"app": entry()}, {"app": fake}, refuse_provisioner())
        self.assertEqual(code, 4)
        self.assertEqual(fake.calls, [])

    def test_a_failed_resolution_counts_4_and_the_rest_still_run(self):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO)
        entries = {"app": entry(), "lib": entry(name="lib", repo="acme/lib")}
        lib = FakeTracker()

        def failing(name):
            if name == "app":
                raise ValueError("no owner/name in the origin remote")
            return entries[name]

        with contextlib.redirect_stdout(io.StringIO()):
            code = githubcli.setup_root(
                self.root,
                [],
                entry_factory=failing,
                tracker_factory=tracker_factory({"lib": lib}),
                provisioner_factory=refuse_provisioner(),
                out=self.out,
                err=self.err,
            )
        self.assertEqual(code, 4)
        self.assertIn("app", self.err.getvalue())
        self.assertIn(("ensure_labels", "acme/lib"), lib.calls)

    def test_a_failed_construction_counts_4_and_the_rest_still_run(self):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO)
        entries = {"app": entry(), "lib": entry(name="lib", repo="acme/lib")}
        lib = FakeTracker()

        def building(resolved):
            if resolved.name == "app":
                raise ValueError("unknown tracker variant 'bord'")
            return {"lib": lib}[resolved.name]

        with contextlib.redirect_stdout(io.StringIO()):
            code = githubcli.setup_root(
                self.root,
                [],
                entry_factory=entry_factory(entries),
                tracker_factory=building,
                provisioner_factory=refuse_provisioner(),
                out=self.out,
                err=self.err,
            )
        self.assertEqual(code, 4)
        self.assertIn("app", self.err.getvalue())
        self.assertIn(("ensure_labels", "acme/lib"), lib.calls)


class LabelsOnlyTest(SetupRootCase):
    def test_labels_entries_run_no_board_half_without_the_flag(self):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(TWO)
        entries = {"app": entry(), "lib": entry(name="lib", repo="acme/lib")}
        app = FakeTracker()
        lib = FakeTracker()
        code = self.run_setup(entries, {"app": app, "lib": lib}, refuse_provisioner())
        self.assertEqual(code, 0)
        out = self.out.getvalue()
        self.assertNotIn("[repo.board]", out)
        self.assertNotIn("next:", out)
        self.assertNotIn("[repo.board]", self.toml_text())

    def test_the_clone_origin_names_the_tracker_repo(self):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "app"\nclone.path = "clone"\n')
        resolved = trackers.setup_entry(
            self.root, "app", git=FakeGit(root=self.root, origin_url="git@github.com:acme/app\n")
        )
        self.assertEqual(resolved.tracker_repo, "acme/app")
        fake = FakeTracker()
        code = self.run_setup({"app": resolved}, {"app": fake}, refuse_provisioner())
        self.assertEqual(code, 0)
        self.assertIn("created label ready on acme/app\n", self.out.getvalue())


class BoardEnsureTest(SetupRootCase):
    def write(self, text):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_labels_entry_with_the_flag_provisions_and_writes_the_inert_table(self):
        fake = FakeTracker()
        made = provision(created=("project", "field:Agent"))
        provisioner = FakeProvisioner(provision=made)
        code = self.run_setup(
            {"app": entry()},
            {"app": fake},
            lambda: provisioner,
            board=True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(provisioner.calls), 1)
        kind, spec = provisioner.calls[0]
        self.assertEqual(kind, "ensure_board")
        self.assertEqual(spec.owner, "acme")
        self.assertEqual(spec.title, "app board")
        self.assertEqual(spec.repo, "acme/app")
        self.assertIsNone(spec.number)
        self.assertIsNone(spec.project_id)
        text = self.toml_text()
        self.assertIn("[repo.board]", text)
        self.assertIn("project_number = 3", text)
        self.assertIn("agent_field", text)
        self.assertNotIn("area_column", text)
        self.assertNotIn("pool_column", text)
        out = self.out.getvalue()
        self.assertIn("created project on acme/app", out)
        self.assertIn("wrote [repo.board] for app", out)
        self.assertIn("tracker.variant", out)

    def test_a_board_entry_without_a_table_is_provisioned_whole(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        made = provision(created=("project",), kept=("field:Agent", "link:acme/app"))
        provisioner = FakeProvisioner(provision=made)
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
        )
        self.assertEqual(code, 0)
        kind, spec = provisioner.calls[0]
        self.assertEqual(kind, "ensure_board")
        self.assertEqual(spec.title, "app board")
        text = self.toml_text()
        self.assertIn("[repo.board]", text)
        self.assertIn("next: korapy doctor", self.out.getvalue())

    def test_a_plain_rerun_after_area_and_pool_is_a_noop(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        flagged = standard_fields() + (
            iface.FieldSpec("area", "Area", "single_select", ("Mobile",)),
            iface.FieldSpec("pool", "Pool", "single_select", ("team-a",)),
        )
        first = provision(created=("project", "field:Area", "field:Pool"), fields=flagged)
        provisioner = FakeProvisioner(provision=first)
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
            areas=["Mobile"],
            pools=["team-a"],
        )
        self.assertEqual(code, 0)
        self.assertIn("area_column", self.toml_text())
        kind, spec = provisioner.calls[0]
        area = [field for field in spec.fields if field.concept == "area"][0]
        self.assertEqual(area.options, ("Mobile",))
        self.out = io.StringIO()
        provisioner.provision = provision(fields=flagged)
        code = self.run_setup(
            {
                "app": entry(
                    variant="board", board=matching_board(area_column="Area", pool_column="Pool")
                )
            },
            {"app": fake},
            lambda: provisioner,
        )
        self.assertEqual(code, 1)
        self.assertIn("kept", self.out.getvalue())

    def test_a_different_table_refuses_with_exit_4(self):
        self.write(TABLED.replace('project_owner = "acme"', 'project_owner = "someone-else"'))
        before = self.toml_text()
        fake = FakeTracker()
        provisioner = FakeProvisioner(provision=provision())
        code = self.run_setup(
            {"app": entry(variant="board", board=matching_board(project_owner="someone-else"))},
            {"app": fake},
            lambda: provisioner,
        )
        self.assertEqual(code, 4)
        self.assertIn("different values", self.err.getvalue())
        self.assertEqual(before, self.toml_text())

    def test_a_board_tracker_error_is_exit_3(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        provisioner = FakeProvisioner(
            provision=provision(), ensure_error=iface.TrackerError("down")
        )
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
        )
        self.assertEqual(code, 3)
        self.assertIn("down", self.err.getvalue())
        self.assertNotIn("[repo.board]", self.toml_text())

    def test_a_board_value_error_is_exit_4(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        provisioner = FakeProvisioner(
            provision=provision(),
            ensure_error=ValueError("the board in the config no longer exists"),
        )
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
        )
        self.assertEqual(code, 4)
        self.assertIn("no longer exists", self.err.getvalue())

    def test_a_provisioner_without_ensure_board_is_exit_4(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        code = self.run_setup({"app": entry(variant="board")}, {"app": fake}, lambda: object())
        self.assertEqual(code, 4)
        self.assertIn("provision", self.err.getvalue())

    def test_mixed_entries_aggregate_4_over_3(self):
        self.write(
            BOARD_BOOTSTRAP
            + '\n[[repo]]\nname = "lib"\ntracker.repo = "acme/lib"\ntracker.variant = "board"\n'
            + '\n[[repo]]\nname = "tool"\ntracker.repo = "acme/tool"\ntracker.variant = "board"\n'
            + '\n[repo.board]\nproject_owner = "someone-else"\nproject_number = 9\n'
        )
        entries = {
            "app": entry(variant="board"),
            "lib": entry(name="lib", repo="acme/lib", variant="board"),
            "tool": entry(name="tool", repo="acme/tool", variant="board"),
        }
        mapping = {"app": FakeTracker(), "lib": FakeTracker(), "tool": FakeTracker()}
        good = FakeProvisioner(provision=provision(created=("project",)))
        bad = FakeProvisioner(provision=provision(), ensure_error=iface.TrackerError("down"))
        other = FakeProvisioner(provision=provision())
        fakes = [good, bad, other]
        with contextlib.redirect_stdout(io.StringIO()):
            code = githubcli.setup_root(
                self.root,
                [],
                entry_factory=entry_factory(entries),
                tracker_factory=tracker_factory(mapping),
                provisioner_factory=lambda: fakes.pop(0),
                out=self.out,
                err=self.err,
            )
        self.assertEqual(code, 4)
        self.assertIn("[repo.board]", self.toml_text())
        self.assertIn("tool", self.err.getvalue())
        self.assertIn("acme/lib", self.err.getvalue())
        self.out = io.StringIO()
        self.err = io.StringIO()
        again = FakeProvisioner(provision=provision())
        failing = FakeProvisioner(provision=provision(), ensure_error=iface.TrackerError("down"))
        rounds = [again, failing]
        with contextlib.redirect_stdout(io.StringIO()):
            code = githubcli.setup_root(
                self.root,
                ["app", "lib"],
                entry_factory=entry_factory(entries),
                tracker_factory=tracker_factory(mapping),
                provisioner_factory=lambda: rounds.pop(0),
                out=self.out,
                err=self.err,
            )
        self.assertEqual(code, 3)

    def test_created_plus_a_board_refusal_is_still_1(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        made = provision(created=("project",), refused=("link is refused",))
        provisioner = FakeProvisioner(provision=made)
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
        )
        self.assertEqual(code, 1)
        self.assertIn("refused on acme/app", self.out.getvalue())

    def test_an_all_kept_board_run_is_exit_1(self):
        self.write(MATCHING_TABLED)
        before = self.toml_text()
        fake = FakeTracker(labels=[("acme/app", NAMES)])
        kept = provision(kept=("project", "field:Agent", "link:acme/app"))
        provisioner = FakeProvisioner(provision=kept)
        code = self.run_setup(
            {"app": entry(variant="board", board=matching_board())},
            {"app": fake},
            lambda: provisioner,
        )
        self.assertEqual(code, 1)
        self.assertEqual(before, self.toml_text())

    def test_flags_reach_the_spec(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        provisioner = FakeProvisioner(provision=provision())
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
            areas=["Mobile", "Web"],
            pools=["team-a"],
            board_owner="someone-else",
            board_title="Custom title",
        )
        self.assertEqual(code, 0)
        _, spec = provisioner.calls[0]
        self.assertEqual(spec.owner, "someone-else")
        self.assertEqual(spec.title, "Custom title")
        by_concept = {field.concept: field for field in spec.fields}
        self.assertEqual(by_concept["area"].options, ("Mobile", "Web"))
        self.assertEqual(by_concept["pool"].options, ("team-a",))

    def test_a_bad_board_owner_flag_is_exit_4(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        provisioner = FakeProvisioner(provision=provision())
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
            board_owner="",
        )
        self.assertEqual(code, 4)
        self.assertEqual(provisioner.calls, [])


class PlanTest(SetupRootCase):
    def write(self, text):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_plan_lists_would_create_and_changes_nothing(self):
        before = self.toml_text()
        fake = FakeTracker()
        preview = provision(created=("project", "field:Agent"))
        provisioner = FakeProvisioner(dry_report=preview)
        code = self.run_setup(
            {"app": entry(variant="board")}, {"app": fake}, lambda: provisioner, plan=True
        )
        self.assertEqual(code, 0)
        self.assertEqual([kind for kind, _ in provisioner.calls], ["dry_run"])
        self.assertNotIn("ensure_labels", [call[0] for call in fake.calls])
        self.assertIn(("missing_labels", "acme/app"), fake.calls)
        out = self.out.getvalue()
        self.assertIn("would create label ready on acme/app", out)
        self.assertIn("would create project on acme/app", out)
        self.assertEqual(before, self.toml_text())

    def test_plan_over_a_converged_root_is_exit_1(self):
        self.write(MATCHING_TABLED)
        before = self.toml_text()
        fake = FakeTracker(labels=[("acme/app", NAMES)])
        preview = provision(kept=("project", "field:Agent", "link:acme/app"))
        provisioner = FakeProvisioner(dry_report=preview)
        code = self.run_setup(
            {"app": entry(variant="board", board=matching_board())},
            {"app": fake},
            lambda: provisioner,
            plan=True,
        )
        self.assertEqual(code, 1)
        self.assertNotIn("would create", self.out.getvalue())
        self.assertEqual(before, self.toml_text())

    def test_plan_shows_the_mapping_write_as_a_diff(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        preview = provision(created=("project",))
        provisioner = FakeProvisioner(dry_report=preview)
        code = self.run_setup(
            {"app": entry(variant="board")}, {"app": fake}, lambda: provisioner, plan=True
        )
        self.assertEqual(code, 0)
        out = self.out.getvalue()
        self.assertIn("[repo.board]", out)
        self.assertIn("project_number", out)
        self.assertIn("--- ", out)
        self.assertIn("+++ ", out)

    def test_plan_without_an_address_notes_the_deferred_table(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        preview = provision(created=("project",), number=None, project_id="")
        provisioner = FakeProvisioner(dry_report=preview)
        code = self.run_setup(
            {"app": entry(variant="board")}, {"app": fake}, lambda: provisioner, plan=True
        )
        self.assertEqual(code, 0)
        self.assertIn("[repo.board]", self.out.getvalue())
        self.assertNotIn("[repo.board]", self.toml_text())

    def test_plan_counts_a_failed_entry_3(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        provisioner = FakeProvisioner(
            dry_report=provision(), ensure_error=iface.TrackerError("down")
        )
        code = self.run_setup(
            {"app": entry(variant="board")}, {"app": fake}, lambda: provisioner, plan=True
        )
        self.assertEqual(code, 3)

    def test_plan_counts_a_bad_spec_4(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        provisioner = FakeProvisioner(
            dry_report=provision(), ensure_error=ValueError("no longer exists")
        )
        code = self.run_setup(
            {"app": entry(variant="board")}, {"app": fake}, lambda: provisioner, plan=True
        )
        self.assertEqual(code, 4)

    def test_plan_without_a_preview_is_exit_4(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()

        class NoPreview:
            def ensure_board(self, spec):
                raise AssertionError("plan must not ensure")

        code = self.run_setup(
            {"app": entry(variant="board")}, {"app": fake}, lambda: NoPreview(), plan=True
        )
        self.assertEqual(code, 4)
        self.assertIn("preview", self.err.getvalue())

    def test_plan_previews_population_without_adding(self):
        self.write(MATCHING_TABLED)
        fake = FakeTracker(labels=[("acme/app", NAMES)])
        preview = provision(kept=("project", "field:Agent"))
        pop = populated(added=("acme/app#7",), kept=("acme/app#8",))
        provisioner = FakeProvisioner(dry_report=preview, populate_report=pop)
        code = self.run_setup(
            {"app": entry(variant="board", board=matching_board())},
            {"app": fake},
            lambda: provisioner,
            plan=True,
            populate=True,
        )
        self.assertEqual(code, 0)
        kinds = [kind for kind, *_ in provisioner.calls]
        self.assertIn("dry_run", kinds)
        self.assertIn("preview_populate", kinds)
        self.assertNotIn("populate", kinds)
        self.assertNotIn("ensure_board", kinds)
        self.assertIn("would add acme/app#7 to the board", self.out.getvalue())


class PopulateWiringTest(SetupRootCase):
    def write(self, text):
        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def populated_entry(self, plan=False, **flags):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        made = provision(created=("project",))
        pop = populated(added=("acme/app#7",), kept=("acme/app#8",))
        provisioner = FakeProvisioner(provision=made, populate_report=pop)
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
            populate=True,
            plan=plan,
            **flags,
        )
        return code, provisioner

    def test_populate_after_ensure_uses_the_provision_id(self):
        code, provisioner = self.populated_entry()
        self.assertEqual(code, 0)
        kinds = [kind for kind, *_ in provisioner.calls]
        self.assertEqual(kinds, ["ensure_board", "populate"])
        self.assertEqual(provisioner.calls[1], ("populate", "PVT_1", "acme/app"))
        self.assertIn("added acme/app#7 to the board", self.out.getvalue())

    def test_populate_refusals_are_exit_1(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        provisioner = FakeProvisioner(
            provision=provision(created=("project",)),
            populate_report=populated(refused=("could not add acme/app#7",)),
        )
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
            populate=True,
        )
        self.assertEqual(code, 1)
        self.assertIn("refused on acme/app", self.out.getvalue())

    def test_populate_tracker_error_is_exit_3(self):
        self.write(BOARD_BOOTSTRAP)
        fake = FakeTracker()
        provisioner = FakeProvisioner(
            provision=provision(created=("project",)),
            populate_error=iface.TrackerError("down"),
        )
        code = self.run_setup(
            {"app": entry(variant="board")},
            {"app": fake},
            lambda: provisioner,
            populate=True,
        )
        self.assertEqual(code, 3)

    def test_populate_on_a_tabled_labels_entry_needs_no_ensure(self):
        from tests.test_board_planner import table

        self.write(TOML)
        fake = FakeTracker()
        board = table(project_number=None, project_id="PVT_9")
        pop = populated(added=("acme/app#7",))
        provisioner = FakeProvisioner(populate_report=pop)
        code = self.run_setup(
            {"app": entry(board=board)}, {"app": fake}, lambda: provisioner, populate=True
        )
        self.assertEqual(code, 0)
        kinds = [kind for kind, *_ in provisioner.calls]
        self.assertEqual(kinds, ["populate"])
        self.assertEqual(provisioner.calls[0], ("populate", "PVT_9", "acme/app"))

    def test_populate_without_an_address_is_exit_4(self):
        fake = FakeTracker()
        provisioner = FakeProvisioner(populate_report=populated())
        code = self.run_setup({"app": entry()}, {"app": fake}, lambda: provisioner, populate=True)
        self.assertEqual(code, 4)
        self.assertIn("pass --board", self.err.getvalue())
        self.assertEqual(provisioner.calls, [])


class ParserTest(unittest.TestCase):
    def test_github_setup_parses_the_board_flags(self):
        args = cli.build_parser().parse_args(["github", "setup"])
        self.assertEqual(args.func.__name__, "cmd_setup")
        self.assertEqual(args.repo, [])
        self.assertFalse(args.board)
        self.assertFalse(args.populate)
        self.assertFalse(args.plan)
        self.assertIsNone(args.area)
        self.assertIsNone(args.pool)
        self.assertIsNone(args.board_owner)
        self.assertIsNone(args.board_title)

    def test_github_setup_takes_repeatable_area_and_pool(self):
        args = cli.build_parser().parse_args(
            [
                "github",
                "setup",
                "--board",
                "--populate",
                "--area",
                "Mobile",
                "--area",
                "Web",
                "--pool",
                "team-a",
                "--board-owner",
                "someone-else",
                "--board-title",
                "Custom title",
            ]
        )
        self.assertTrue(args.board)
        self.assertTrue(args.populate)
        self.assertEqual(args.area, ["Mobile", "Web"])
        self.assertEqual(args.pool, ["team-a"])
        self.assertEqual(args.board_owner, "someone-else")
        self.assertEqual(args.board_title, "Custom title")

    def test_github_needs_a_verb(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["github"])

    def test_cmd_setup_without_root_exits_4(self):
        empty = tempfile.mkdtemp(prefix="korapy-github-noroot-")
        self.addCleanup(lambda: os.rmdir(empty))
        namespace = argparse.Namespace(
            repo=[],
            board=False,
            populate=False,
            plan=False,
            area=None,
            pool=None,
            board_owner=None,
            board_title=None,
            func=githubcli.cmd_setup,
        )
        with mock.patch("os.getcwd", return_value=empty):
            self.assertEqual(githubcli.cmd_setup(namespace), 4)


class ProductionWiringTest(SetupRootCase):
    def test_a_board_entry_without_a_table_provisions_through_the_real_wiring(self):
        import json as json_lib

        from tests.test_board_fields import FakePacer
        from tests.test_board_provision import (
            ProvisionFakeRun,
            STATUS_OPTIONS,
            by_id,
            owner_body,
            project_node,
            select_node,
            standard_field_nodes,
        )

        with open(os.path.join(self.root, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(BOARD_BOOTSTRAP)
        resolved = trackers.setup_entry(self.root, "app", git=FakeGit(root=self.root))
        self.assertEqual(resolved.variant, "board")
        self.assertIsNone(resolved.board)
        self.assertEqual(resolved.tracker_repo, "acme/app")

        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user", "U_1", "acme")])
        run.script(
            "BoardTitles",
            [
                {
                    "user": {
                        "projectsV2": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    },
                    "organization": None,
                }
            ],
        )
        run.add_rest("repos/acme/app", {"node_id": "R_1"})
        run.script(
            "BoardCreate",
            [
                {
                    "createProjectV2": {
                        "projectV2": {
                            "id": "PVT_9",
                            "number": 7,
                            "title": "app board",
                            "public": False,
                        }
                    }
                }
            ],
        )
        run.script(
            "BoardById",
            by_id(
                project_node(
                    pid="PVT_9",
                    number=7,
                    title="app board",
                    repos=("acme/app",),
                    fields=[select_node("F_board", "Status", STATUS_OPTIONS)],
                ),
                project_node(
                    pid="PVT_9",
                    number=7,
                    title="app board",
                    repos=("acme/app",),
                    fields=standard_field_nodes(),
                ),
            ),
        )
        run.add_rest("repos/acme/app/labels?per_page=100&page=1", [])
        run.add_rest("repos/acme/app/labels", {})
        run.script(
            "TextFieldCreate",
            [
                {"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": name}}}
                for name in ("Agent", "Agent state", "Blocked by")
            ],
        )
        run.script(
            "SelectFieldCreate",
            [{"createProjectV2Field": {"projectV2Field": {"id": "F_new", "name": "Priority"}}}],
        )

        def fake_run(args, **kwargs):
            if len(args) > 2 and args[0] == "api" and args[1] == "--method":
                path = next(part for part in args if part.startswith("repos/"))
                answer = run.rest.get(path, {})
                if isinstance(answer, Exception):
                    raise answer
                return json_lib.dumps(answer)
            return run(args, **kwargs)

        args = cli.build_parser().parse_args(["github", "setup"])
        with (
            mock.patch("os.getcwd", return_value=self.root),
            mock.patch("korapy.github.gh.run", fake_run),
            # The production factories default to the shared bucket; the
            # suite tripwire forbids it, so the default factory serves
            # the established fake pacer instead. The run and git seams
            # stay the only production seams under test.
            mock.patch("korapy.github.gh._pacer", lambda: FakePacer()),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(githubcli.cmd_setup(args), 0)
        queries = run.queries()
        self.assertTrue(any("BoardCreate" in query for query in queries))
        self.assertTrue(any("BoardTitles" in query for query in queries))
        text = self.toml_text()
        self.assertIn("[repo.board]", text)
        self.assertIn("project_number = 7", text)
        self.assertIn("agent_field", text)
        self.assertIn("created project", out.getvalue())


if __name__ == "__main__":
    unittest.main()
