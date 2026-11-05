"""Agent profiles, ticket 4: temporary overrides.

Offline: duration parsing, expiry predicates and the session record are
plain data plus temp-dir files. No subprocesses, no network, no real
state dirs.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from corral import accounts, profiles, settings
from tests.test_profiles_resolver import NUMBER, REPO, ROLE, T0, over, parsed, sel


def resolve(parsed_profiles, selections=None, **kw):
    args = {"repo": REPO, "role": ROLE, "number": NUMBER, "now": T0}
    args.update(kw)
    return profiles.resolve(parsed_profiles, selections or sel(), **args)  # ty: ignore[invalid-argument-type]


def session_dir():
    return tempfile.mkdtemp(prefix="corral-session-")


class SharedParser(unittest.TestCase):
    def test_a_temp_accepts_the_same_duration_strings_as_quota_marks(self):
        durations = ["90", "2h", "1.5d", "15m", "30s", " 2H ", 90, 1.5, "1.5h"]
        for value in durations:
            with self.subTest(value=value):
                self.assertEqual(
                    profiles.override_until(temp=value, now=T0),
                    T0 + accounts.parse_duration(value, "quota"),
                )

    def test_the_temp_path_uses_the_accounts_parser(self):
        with mock.patch.object(accounts, "parse_duration", return_value=7200.0) as parse_duration:
            self.assertEqual(profiles.override_until(temp="2h", now=T0), T0 + 7200.0)
        self.assertEqual(parse_duration.call_count, 1)
        self.assertEqual(parse_duration.call_args[0][0], "2h")

    def test_bad_durations_are_exit_4_naming_the_value(self):
        for value in ("", "soon", "0", "-5m", "1w", "h", "1.2.3h", 0, -3, True, ["2h"]):
            with self.subTest(value=value), self.assertRaises(settings.SettingsError) as ctx:
                profiles.override_until(temp=value, now=T0)
            self.assertIn(repr(value), str(ctx.exception))

    def test_temp_and_until_together_are_refused_naming_both(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            profiles.override_until(temp="2h", until=1900000000, now=T0)
        self.assertIn("--temp", str(ctx.exception))
        self.assertIn("--until", str(ctx.exception))

    def test_neither_temp_nor_until_reads_as_no_override(self):
        self.assertIsNone(profiles.override_until(now=T0))

    def test_until_accepts_epoch_seconds(self):
        self.assertEqual(profiles.override_until(until=1900000000, now=T0), 1900000000.0)
        self.assertEqual(profiles.override_until(until=1900000000.5, now=T0), 1900000000.5)
        self.assertEqual(profiles.override_until(until="1900000000", now=T0), 1900000000.0)

    def test_a_past_until_is_allowed_and_already_expired(self):
        until = profiles.override_until(until=T0 - 1, now=T0)
        self.assertEqual(until, T0 - 1)
        self.assertTrue(profiles.is_expired(over("global", "fast", until), T0))

    def test_bad_until_instants_are_exit_4(self):
        for value in ("soon", "", True, float("nan"), float("inf"), [1900000000]):
            with self.subTest(value=value), self.assertRaises(settings.SettingsError):
                profiles.override_until(until=value, now=T0)

    def test_a_bad_now_is_exit_4(self):
        for now in ("soon", float("nan"), None):
            with self.subTest(now=now), self.assertRaises(settings.SettingsError):
                profiles.override_until(temp="2h", now=now)


class Expiry(unittest.TestCase):
    def test_a_live_override_is_not_expired(self):
        self.assertFalse(profiles.is_expired(over("global", "fast", T0 + 60), T0))

    def test_an_override_is_expired_at_and_past_its_until(self):
        self.assertTrue(profiles.is_expired(over("global", "fast", T0), T0))
        self.assertTrue(profiles.is_expired(over("global", "fast", T0 - 1), T0))

    def test_a_bad_now_is_exit_4(self):
        for now in ("soon", float("nan"), None):
            with self.subTest(now=now), self.assertRaises(settings.SettingsError):
                profiles.is_expired(over("global", "fast", T0 + 60), now)

    def test_an_expired_override_survives_save_and_load_until_reset(self):
        selections = sel(default="sixth", override=over("global", "fast", T0 - 1, "trying it"))
        with tempfile.TemporaryDirectory(prefix="corral-config-") as directory:
            path = os.path.join(directory, "profiles.toml")
            profiles.save_selections(selections, path)
            self.assertEqual(profiles.load_selections(path), selections)
        found, provenance = resolve(parsed("fast", "sixth"), selections)
        self.assertEqual((found.name, provenance.layer), ("sixth", 6))
        chain = profiles.explain(
            parsed("fast", "sixth"), selections, repo=REPO, role=ROLE, number=NUMBER, now=T0
        )
        by_layer = {entry.layer: entry for entry in chain}
        self.assertEqual(by_layer[3].state, "expired")
        self.assertEqual(by_layer[3].profile, "fast")

    def test_clearing_the_override_reads_as_no_override(self):
        selections = sel(default="sixth")
        with tempfile.TemporaryDirectory(prefix="corral-config-") as directory:
            path = os.path.join(directory, "profiles.toml")
            profiles.save_selections(selections, path)
            self.assertIsNone(profiles.load_selections(path).override)


class SessionRecord(unittest.TestCase):
    def test_record_writes_the_resolved_profile_and_reads_it_back(self):
        selections = sel(override=over("global", "fast", T0 + 3600))
        found, provenance = resolve(parsed("fast", "sixth"), selections)
        directory = session_dir()
        profiles.record_profile(directory, found, provenance, model="provider-a/model-x")
        recorded = profiles.recorded_profile(directory)
        self.assertEqual(
            recorded,
            profiles.RecordedProfile(
                name="fast",
                runtime="omp",
                model="provider-a/model-x",
                layer=3,
                layer_name="temp",
                source=provenance.source,
                until=T0 + 3600,
            ),
        )
        with open(os.path.join(directory, profiles.PROFILE_FILE), encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["profile"], "fast")

    def test_a_session_without_a_record_reads_as_none(self):
        self.assertIsNone(profiles.recorded_profile(session_dir()))

    def test_a_corrupt_record_fails_closed(self):
        bad = [
            "{oops",
            "[]",
            "{}",
            {"profile": "fast"},
            {
                "profile": "",
                "runtime": "omp",
                "model": None,
                "layer": 3,
                "layer_name": "temp",
                "source": "x",
                "until": None,
            },
            {
                "profile": "fast",
                "runtime": "unknown-rt",
                "model": None,
                "layer": 3,
                "layer_name": "temp",
                "source": "x",
                "until": None,
            },
            {
                "profile": "fast",
                "runtime": "omp",
                "model": 7,
                "layer": 3,
                "layer_name": "temp",
                "source": "x",
                "until": None,
            },
            {
                "profile": "fast",
                "runtime": "omp",
                "model": None,
                "layer": 9,
                "layer_name": "temp",
                "source": "x",
                "until": None,
            },
            {
                "profile": "fast",
                "runtime": "omp",
                "model": None,
                "layer": 3,
                "layer_name": "temp",
                "source": "x",
                "until": "soon",
            },
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                directory = session_dir()
                path = os.path.join(directory, profiles.PROFILE_FILE)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(raw if isinstance(raw, str) else json.dumps(raw))
                with self.assertRaises(settings.SettingsError):
                    profiles.recorded_profile(directory)

    def test_a_failed_record_writes_nothing(self):
        directory = os.path.join(tempfile.mkdtemp(prefix="corral-session-"), "fresh")
        bad = profiles.Profile(name="fast", runtime="unknown-rt")
        provenance = profiles.Provenance(
            layer=3, layer_name="temp", source="override", profile="fast"
        )
        with self.assertRaises(settings.SettingsError):
            profiles.record_profile(directory, bad, provenance)
        self.assertFalse(os.path.exists(directory))

    def test_a_runtime_refinement_is_recorded_for_the_resume(self):
        found, provenance = resolve(parsed("fast", "sixth"), sel(default="sixth"))
        directory = session_dir()
        profiles.record_profile(directory, found, provenance, runtime="muse")
        recorded = profiles.recorded_profile(directory)
        assert recorded is not None
        self.assertEqual((recorded.name, recorded.runtime), ("sixth", "muse"))

    def test_a_session_launched_under_an_override_resumes_its_record_after_expiry(self):
        selections = sel(default="sixth", override=over("global", "fast", T0 + 3600))
        found, provenance = resolve(parsed("fast", "sixth"), selections)
        directory = session_dir()
        profiles.record_profile(directory, found, provenance, model="provider-a/model-x")
        expired, _ = resolve(parsed("fast", "sixth"), selections, now=T0 + 7200)
        self.assertEqual(expired.name, "sixth")
        recorded = profiles.recorded_profile(directory)
        assert recorded is not None
        self.assertEqual((recorded.name, recorded.runtime), ("fast", "omp"))
        self.assertEqual(recorded.until, T0 + 3600)
