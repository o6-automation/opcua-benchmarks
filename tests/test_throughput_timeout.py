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
#
#    Copyright 2026 (c) o6 Automation GmbH (Author: Daniel Opitz)

"""Collection limits can change without changing measured workloads or saved keys."""

import io
import json
from unittest.mock import Mock

import pytest

from throughput import run


@pytest.mark.parametrize("override,expected", [(None, 120), ("600", 600)])
def test_collection_uses_timeout_override_and_preserves_results(monkeypatch, override, expected):
    monkeypatch.delenv("BENCH_SAMPLE_TIMEOUT", raising=False)
    if override is not None:
        monkeypatch.setenv("BENCH_SAMPLE_TIMEOUT", override)
    process = Mock(stdin=io.StringIO(), stdout=io.StringIO(), returncode=0)
    process.poll.return_value = 0
    process.communicate.return_value = (json.dumps(dict(start_ns=100, end_ns=300, operations=10)), None)
    monkeypatch.setattr(run.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(run, "read_until", lambda *_: [])
    result = run.run_sample(["test"], "async", "read", 1, 10, 1, 100, run.Payload(1, 1, ()))
    process.communicate.assert_called_once_with(timeout=expected)
    assert result["elapsed_ns"] == 200
    assert result["operations"] == 10
    assert "sample_timeout" not in result


@pytest.mark.parametrize("value", ["0", "-1", "", "abc", "1.5", "nan", "inf"])
def test_invalid_timeout_fails_before_starting_workers(monkeypatch, value):
    monkeypatch.setenv("BENCH_SAMPLE_TIMEOUT", value)
    popen = Mock()
    monkeypatch.setattr(run.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="BENCH_SAMPLE_TIMEOUT must be a positive integer"):
        run.run_sample(["test"], "async", "read", 1, 10, 1, 100, run.Payload(1, 1, ()))
    popen.assert_not_called()
