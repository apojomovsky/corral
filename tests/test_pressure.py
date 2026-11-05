"""Host pressure: load per CPU and memory availability, saturation backoff.

A saturated reading refuses new launches (exit 75) even when shares are
free; an unknown reading never blocks. Tests inject fixture text and fake
seams; only ``production_read`` touches ``/proc`` and it never does here.
"""

import unittest
from unittest import mock

from korapy import pressure as P

LOADAVG = "0.80 0.60 0.40 1/200 12345\n"

MEMINFO = """MemTotal:        8000000 kB
MemFree:         1000000 kB
MemAvailable:    4000000 kB
Buffers:          100000 kB
"""


def reading(load_per_cpu=None, mem_available_pct=None):
    return P.Reading(load_per_cpu=load_per_cpu, mem_available_pct=mem_available_pct)


def check_ratio(test, value, want):
    assert value is not None
    test.assertAlmostEqual(value, want)


class Parse(unittest.TestCase):
    def test_load_divides_the_one_minute_average_by_cpu_count(self):
        found = P.read(LOADAVG, MEMINFO, 4)
        check_ratio(self, found.load_per_cpu, 0.2)
        check_ratio(self, found.mem_available_pct, 50.0)

    def test_a_missing_file_reads_as_unknown(self):
        found = P.read(None, MEMINFO, 4)
        self.assertIsNone(found.load_per_cpu)
        check_ratio(self, found.mem_available_pct, 50.0)

    def test_a_missing_meminfo_reads_as_unknown(self):
        found = P.read(LOADAVG, None, 4)
        check_ratio(self, found.load_per_cpu, 0.2)
        self.assertIsNone(found.mem_available_pct)

    def test_garbage_reads_as_unknown_instead_of_raising(self):
        found = P.read("hot garbage\n", "also garbage\n", 4)
        self.assertEqual(found, reading())

    def test_a_missing_cpu_count_leaves_only_load_unknown(self):
        found = P.read(LOADAVG, MEMINFO, None)
        self.assertIsNone(found.load_per_cpu)
        check_ratio(self, found.mem_available_pct, 50.0)

    def test_a_zero_cpu_count_leaves_load_unknown(self):
        found = P.read(LOADAVG, MEMINFO, 0)
        self.assertIsNone(found.load_per_cpu)

    def test_a_meminfo_without_available_reads_as_unknown(self):
        found = P.read(LOADAVG, "MemTotal: 8000000 kB\n", 4)
        self.assertIsNone(found.mem_available_pct)


class Saturate(unittest.TestCase):
    def test_a_quiet_host_is_not_saturated(self):
        saturated, _detail = P.saturated(
            reading(0.2, 50.0), max_load_per_cpu=2.0, min_mem_available_pct=10.0
        )
        self.assertFalse(saturated)

    def test_load_above_max_saturates_and_names_load(self):
        saturated, detail = P.saturated(
            reading(8.0, 50.0), max_load_per_cpu=2.0, min_mem_available_pct=10.0
        )
        self.assertTrue(saturated)
        self.assertIn("load", detail)
        self.assertIn("8", detail)

    def test_low_available_memory_saturates_and_names_memory(self):
        saturated, detail = P.saturated(
            reading(0.2, 3.0), max_load_per_cpu=2.0, min_mem_available_pct=10.0
        )
        self.assertTrue(saturated)
        self.assertIn("MemAvailable", detail)
        self.assertIn("3", detail)

    def test_both_over_the_line_names_both(self):
        saturated, detail = P.saturated(
            reading(8.0, 3.0), max_load_per_cpu=2.0, min_mem_available_pct=10.0
        )
        self.assertTrue(saturated)
        self.assertIn("load", detail)
        self.assertIn("MemAvailable", detail)

    def test_exactly_at_the_line_is_not_saturated(self):
        saturated, _detail = P.saturated(
            reading(2.0, 10.0), max_load_per_cpu=2.0, min_mem_available_pct=10.0
        )
        self.assertFalse(saturated)

    def test_unknown_never_saturates(self):
        saturated, _detail = P.saturated(
            reading(), max_load_per_cpu=2.0, min_mem_available_pct=10.0
        )
        self.assertFalse(saturated)

    def test_half_unknown_still_decides_on_the_known_half(self):
        saturated, _detail = P.saturated(
            reading(None, 3.0), max_load_per_cpu=2.0, min_mem_available_pct=10.0
        )
        self.assertTrue(saturated)


def seams(files, ncpu=4):
    def read_text(path):
        if path not in files:
            raise OSError(f"no such file: {path}")
        return files[path]

    return (
        mock.patch.object(P, "_read_text", read_text),
        mock.patch.object(P, "_cpu_count", lambda: ncpu),
    )


class ProductionRead(unittest.TestCase):
    def test_production_reads_through_the_seams(self):
        files = {"/proc/loadavg": LOADAVG, "/proc/meminfo": MEMINFO}
        for patcher in seams(files):
            self.enterContext(patcher)
        found = P.production_read()
        check_ratio(self, found.load_per_cpu, 0.2)
        check_ratio(self, found.mem_available_pct, 50.0)

    def test_unreadable_files_read_as_unknown_instead_of_raising(self):
        for patcher in seams({}):
            self.enterContext(patcher)
        self.assertEqual(P.production_read(), reading())

    def test_a_missing_cpu_count_reads_load_as_unknown(self):
        files = {"/proc/loadavg": LOADAVG, "/proc/meminfo": MEMINFO}
        for patcher in seams(files, ncpu=None):
            self.enterContext(patcher)
        found = P.production_read()
        self.assertIsNone(found.load_per_cpu)
        check_ratio(self, found.mem_available_pct, 50.0)
