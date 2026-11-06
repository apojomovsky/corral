"""The two-alias owner lookup: user-owned boards resolve.

Every owner query asks one login as both ``user`` and ``organization``.
For a user-owned board the provider answers the project under ``user``,
null under ``organization``, and one ``NOT_FOUND`` error on the
``organization`` path (mirrored for an organization-owned board), while
still printing the body. The lookup reads that body through the raw
``run`` seam and tolerates exactly the losing half's noise.

Canned full response bodies (``data`` plus ``errors``) at the injected
``run`` seam, plus a ``FakePacer`` for the shared bucket: the fakes
return the body verbatim, the way the runner prints it, so the tolerant
read is what is under test. Importing ``tests.test_board_fields``
installs the suite-wide tripwire that fails the run if any test reaches
the default pacer file or a real subprocess.
"""

import json
import unittest

from korapy import tracker as iface
from korapy.github import board as boardreader
from korapy.github import gh
from tests.test_board_doctor import titles_payload
from tests.test_board_fields import FakePacer
from tests.test_board_provision import (
    ProvisionFakeRun,
    bootstrap_provisioner_for,
    by_id,
    by_number,
    owner_body,
    project_node,
    provisioner_for,
    standard_field_nodes,
)
from tests.test_board_reader import (
    BoardFakeRun,
    config,
    full_schema_fields,
    schema_payload,
)

LOGIN = "example"


def not_found(path, login=LOGIN):
    """One expected losing-half error entry, naming the half it missed."""
    half = path[-1]
    kind = "User" if half == "user" else "Organization"
    return {
        "type": "NOT_FOUND",
        "path": list(path),
        "message": f"Could not resolve to an {kind} with the login of '{login}'.",
    }


def noisy_schema_body(login=LOGIN):
    """A user-owned schema answer: project under ``user``, noise on ``organization``."""
    return {"data": schema_payload(*full_schema_fields()), "errors": [not_found(["organization"])]}


def org_noisy_schema_body(login=LOGIN):
    """An organization-owned schema answer: project under ``organization``."""
    payload = schema_payload(*full_schema_fields())
    return {
        "data": {"user": None, "organization": payload["user"]},
        "errors": [not_found(["user"])],
    }


def noisy_titles_body(boards):
    """A user-owned title list: connection under ``user``, noise on ``organization``."""
    data = titles_payload(*boards)
    data["organization"] = None
    return {"data": data, "errors": [not_found(["organization"])]}


def noisy_by_number_body(project):
    """A user-owned addressed-project answer for the provisioner."""
    return {"data": by_number(project), "errors": [not_found(["organization"])]}


def reader_for(run, pacer=None, **over):
    return boardreader.BoardReader(
        config(**over), run, pacer=pacer if pacer is not None else FakePacer()
    )


class UserOwnedSchemaTest(unittest.TestCase):
    def test_user_owned_board_resolves_schema(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [noisy_schema_body()])
        found = reader_for(run).schema()
        self.assertEqual(found.project_id, "PVT_1")
        self.assertEqual(found.fields["priority"].id, "F_prio")

    def test_org_owned_board_resolves_schema(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [org_noisy_schema_body()])
        found = reader_for(run).schema()
        self.assertEqual(found.project_id, "PVT_1")
        self.assertEqual(found.fields["priority"].id, "F_prio")

    def test_production_style_raise_with_a_body_still_resolves(self):
        body = noisy_schema_body()
        run = BoardFakeRun().script(
            "BoardSchemaByNumber",
            [gh.GhError("other", "Could not resolve to an Organization", output=json.dumps(body))],
        )
        found = reader_for(run).schema()
        self.assertEqual(found.project_id, "PVT_1")


class TwoAliasFailureTest(unittest.TestCase):
    def test_both_halves_null_is_a_tracker_error(self):
        body = {
            "data": {"user": None, "organization": None},
            "errors": [not_found(["user"]), not_found(["organization"])],
        }
        run = BoardFakeRun().script("BoardSchemaByNumber", [body])
        with self.assertRaisesRegex(iface.TrackerError, "missing or ambiguous"):
            reader_for(run).schema()

    def test_both_halves_set_is_a_tracker_error(self):
        payload = schema_payload(*full_schema_fields())
        payload["organization"] = payload["user"]
        run = BoardFakeRun().script("BoardSchemaByNumber", [payload])
        with self.assertRaisesRegex(iface.TrackerError, "missing or ambiguous"):
            reader_for(run).schema()

    def test_not_found_on_an_unrelated_path_fails(self):
        body = {
            "data": schema_payload(*full_schema_fields()),
            "errors": [not_found(["user", "projectV2"])],
        }
        run = BoardFakeRun().script("BoardSchemaByNumber", [body])
        with self.assertRaisesRegex(iface.TrackerError, "board unreachable"):
            reader_for(run).schema()

    def test_rate_limit_entry_maps_and_starts_the_cooldown(self):
        pacer = FakePacer()
        body = {
            "data": schema_payload(*full_schema_fields()),
            "errors": [
                {
                    "type": "RATE_LIMITED",
                    "message": "API rate limit exceeded for installation ID 1.",
                }
            ],
        }
        run = BoardFakeRun().script("BoardSchemaByNumber", [body])
        with self.assertRaises(iface.TrackerRateLimited):
            reader_for(run, pacer=pacer).schema()
        self.assertTrue(pacer.cooldowns)


class ProvisionOwnerLookupTest(unittest.TestCase):
    def test_addressed_user_owned_board_resolves(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        run.script(
            "BoardByNumber",
            [noisy_by_number_body(project_node(fields=standard_field_nodes()))],
        )
        run.script("BoardById", by_id(project_node(fields=standard_field_nodes())))
        provisioner, spec = provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())
        self.assertEqual(run.mutations(), [])

    def test_title_search_user_owned_board_resolves(self):
        run = ProvisionFakeRun().script("OwnerResolve", [owner_body("user")])
        run.script("BoardTitles", [noisy_titles_body([("PVT_a", 2, "demo board")])])
        run.script(
            "BoardById",
            by_id(
                project_node(pid="PVT_a", number=2, fields=standard_field_nodes()),
                project_node(pid="PVT_a", number=2, fields=standard_field_nodes()),
            ),
        )
        provisioner, spec = bootstrap_provisioner_for(run)
        report = provisioner.ensure_board(spec)
        self.assertEqual(report.created, ())
        self.assertEqual(report.refused, ())


class DoctorResolvesUserOwnedBoardTest(unittest.TestCase):
    def test_snapshot_resolves_a_user_owned_board(self):
        run = BoardFakeRun().script("BoardSchemaByNumber", [noisy_schema_body()])
        run.script("ProvisionBoardTitles", [noisy_titles_body([("PVT_1", 3, "Board")])])
        snap = reader_for(run).snapshot()
        self.assertEqual(snap.title, "Board")
        self.assertEqual(snap.owner_boards, (iface.OwnerBoard(number=3, title="Board"),))
