# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""``partition_cpus`` splits by physical core, using fake sysfs topologies."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.workers import partition_cpus


def fake_sysfs(root: Path, siblings: dict[int, str]) -> Path:
    for cpu, text in siblings.items():
        directory = root / f"cpu{cpu}" / "topology"
        directory.mkdir(parents=True)
        (directory / "thread_siblings_list").write_text(text + "\n")
    return root


def pairs_n_n_plus(cores: int) -> dict[int, str]:
    """c7i.4xlarge style: siblings are n and n + cores."""
    return {n: f"{n},{n + cores}" for n in range(cores)} | {n + cores: f"{n},{n + cores}" for n in range(cores)}


def adjacent(cores: int) -> dict[int, str]:
    return {c * 2 + t: f"{c * 2}-{c * 2 + 1}" for c in range(cores) for t in range(2)}


class PartitionCpusTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def split(self, siblings: dict[int, str], allowed: set[int] | None = None):
        fake_sysfs(self.root, siblings)
        allowed = set(siblings) if allowed is None else allowed
        with mock.patch.object(os, "sched_getaffinity", create=True, return_value=allowed):
            return partition_cpus(len(siblings), sysfs=self.root)

    def test_siblings_n_and_n_plus_8(self) -> None:
        server, clients = self.split(pairs_n_n_plus(8))
        self.assertEqual(server, {0, 1, 2, 3, 8, 9, 10, 11})
        self.assertEqual(clients, {4, 5, 6, 7, 12, 13, 14, 15})

    def test_adjacent_siblings(self) -> None:
        server, clients = self.split(adjacent(8))
        self.assertEqual(server, set(range(8)))
        self.assertEqual(clients, set(range(8, 16)))

    def test_no_smt(self) -> None:
        server, clients = self.split({n: str(n) for n in range(8)})
        self.assertEqual(server, {0, 1, 2, 3})
        self.assertEqual(clients, {4, 5, 6, 7})

    def test_odd_core_count_gives_server_the_extra_core(self) -> None:
        server, clients = self.split(adjacent(5))
        self.assertEqual(server, {0, 1, 2, 3, 4, 5})
        self.assertEqual(clients, {6, 7, 8, 9})

    def test_restricted_affinity_ignores_disallowed_cpus(self) -> None:
        allowed = {2, 3, 4, 5, 10, 11, 12, 13}
        server, clients = self.split(pairs_n_n_plus(8), allowed)
        self.assertEqual(server, {2, 3, 10, 11})
        self.assertEqual(clients, {4, 5, 12, 13})

    def test_never_shares_a_core(self) -> None:
        for siblings in (pairs_n_n_plus(8), adjacent(8), adjacent(3)):
            with self.subTest(cores=len(siblings) // 2):
                self.setUp()
                server, clients = self.split(siblings)
                self.assertFalse(server & clients)
                self.assertEqual(server | clients, set(siblings))
                for cpu in server:
                    self.assertFalse(set(map(int, siblings[cpu].replace("-", ",").split(","))) & clients)

    def test_missing_topology_falls_back_to_numeric_split(self) -> None:
        with mock.patch.object(os, "sched_getaffinity", create=True, return_value=set(range(16))):
            self.assertEqual(partition_cpus(16, sysfs=self.root), (set(range(8)), set(range(8, 16))))
            self.assertEqual(partition_cpus(5, sysfs=self.root), ({0, 1, 2}, {3, 4}))

    def test_without_affinity_support_falls_back_to_numeric_split(self) -> None:
        original = getattr(os, "sched_getaffinity", None)
        if original is not None:
            delattr(os, "sched_getaffinity")
        try:
            self.assertEqual(partition_cpus(4, sysfs=self.root), ({0, 1}, {2, 3}))
        finally:
            if original is not None:
                os.sched_getaffinity = original

    def test_single_core_is_unchanged(self) -> None:
        fake_sysfs(self.root, pairs_n_n_plus(4))
        with mock.patch.object(os, "sched_getaffinity", create=True, return_value=set(range(8))):
            self.assertEqual(partition_cpus(8, single_core=True, sysfs=self.root), ({0}, {1, 2, 3, 5, 6, 7}))

    def test_empty_total(self) -> None:
        self.assertEqual(partition_cpus(0, sysfs=self.root), (set(), set()))


if __name__ == "__main__":
    unittest.main()
