"""The hold predicate every resume path checks. Fakes only."""

import unittest

from corral import hold, settings


def reader(mapping, calls=None, kinds=None):
    """A read_labels fake serving label lists from a {(kind, repo, number): labels} map."""

    def read(kind, repo, number):
        if calls is not None:
            calls.append((kind, repo, number))
        if kinds is not None:
            kinds.append(kind)
        key = (kind, repo, number)
        if key not in mapping:
            raise AssertionError(f"unexpected read {key}")
        value = mapping[key]
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value

    return read


def raising(err):
    def read(kind, repo, number):
        raise err

    return read


class LabelsHeldTest(unittest.TestCase):
    def test_no_hold(self):
        self.assertFalse(hold.labels_held([]))
        self.assertFalse(hold.labels_held(["ready"]))
        self.assertFalse(hold.labels_held(None))

    def test_held(self):
        self.assertTrue(hold.labels_held(["hold"]))
        self.assertTrue(hold.labels_held(["ready", "hold"]))

    def test_custom_hold_label(self):
        self.assertTrue(hold.labels_held(["paused"], hold_label="paused"))
        self.assertFalse(hold.labels_held(["hold"], hold_label="paused"))


class ReasonTest(unittest.TestCase):
    def test_free_ticket_resumes(self):
        read = reader({("issue", "app", 7): []})
        self.assertIsNone(hold.reason(read, ("app", 7)))

    def test_free_ticket_and_pr_resume(self):
        read = reader({("issue", "app", 7): [], ("pr", "app", 8): ["ready"]})
        self.assertIsNone(hold.reason(read, ("app", 7), [("app", 8)]))

    def test_held_ticket(self):
        read = reader({("issue", "app", 7): ["hold"]})
        reason = hold.reason(read, ("app", 7))
        self.assertIn("app#7", reason)
        self.assertIn("hold", reason)

    def test_held_linked_pr(self):
        read = reader({("issue", "app", 7): [], ("pr", "app", 8): ["hold"]})
        reason = hold.reason(read, ("app", 7), [("app", 8)])
        self.assertIn("app#7", reason)
        self.assertIn("app#8", reason)
        self.assertIn("hold", reason)

    def test_read_that_raises_is_a_hold(self):
        reason = hold.reason(raising(RuntimeError("flaky")), ("app", 7))
        self.assertIn("app#7", reason)
        self.assertIn("hold", reason)
        self.assertIn("unreadable", reason)

    def test_linked_read_that_raises_is_a_hold(self):
        read = reader({("issue", "app", 7): [], ("pr", "app", 8): RuntimeError("flaky")})
        reason = hold.reason(read, ("app", 7), [("app", 8)])
        self.assertIn("app#7", reason)
        self.assertIn("unreadable", reason)

    def test_junk_reads_are_holds(self):
        for junk in (None, "hold", {"labels": ["hold"]}, ["hold", 7], [None]):
            with self.subTest(junk=junk):
                read = reader({("issue", "app", 7): junk})
                reason = hold.reason(read, ("app", 7))
                self.assertIn("app#7", reason)
                self.assertIn("hold", reason)
                self.assertIn("unreadable", reason)

    def test_duplicates_in_linked_read_once(self):
        calls = []
        read = reader({("issue", "app", 7): [], ("pr", "app", 8): []}, calls=calls)
        self.assertIsNone(hold.reason(read, ("app", 7), [("app", 8), ("app", 8)]))
        self.assertEqual(calls, [("issue", "app", 7), ("pr", "app", 8)])

    def test_custom_hold_label_from_settings(self):
        resolved = settings.resolve(project={"board": {"hold_label": "paused"}}, env={})
        label = resolved["board.hold_label"]
        read = reader({("issue", "app", 7): ["paused"]})
        reason = hold.reason(read, ("app", 7), hold_label=label)
        self.assertIn("paused", reason)
        read = reader({("issue", "app", 7): ["hold"]})
        self.assertIsNone(hold.reason(read, ("app", 7), hold_label=label))

    def test_hold_label_default(self):
        self.assertEqual(settings.REGISTRY["board.hold_label"].default, "hold")
        self.assertIn("corral.hold", settings.PROVIDERS)


if __name__ == "__main__":
    unittest.main()
