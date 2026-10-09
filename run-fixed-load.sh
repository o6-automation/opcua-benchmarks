#!/usr/bin/env bash
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

# Compare all five servers using 10 native C clients and scalar async Reads.
# Usage: ./run-fixed-load.sh [new-database-path]
# Environment overrides: BENCH_PYTHON, SAMPLES, ITERATIONS, WARMUP,
# MAX_OUTSTANDING (per client: 100 = 1,000 total; 1000 = 10,000 total),
# WITH_SDKS=1 (also Milo, S2OPC and gopcua: eight servers).
# Each sample performs ITERATIONS calls per client, rather than running for
# a fixed duration. Lower ITERATIONS if a client hits the runner's 120s timeout.
set -euo pipefail

if (( $# > 1 )) || [[ ${1:-} == --help || ${1:-} == -h ]]; then
    head -n 8 "${BASH_SOURCE[0]}"
    exit 0
fi

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bench_python=${BENCH_PYTHON:-"$repo_dir/.venv/bin/python"}
samples=${SAMPLES:-3}
iterations=${ITERATIONS:-100000}
warmup=${WARMUP:-10000}
max_outstanding=${MAX_OUTSTANDING:-100}
with_sdks=${WITH_SDKS:-0}

for setting in samples iterations warmup max_outstanding; do
    if [[ ! ${!setting} =~ ^[1-9][0-9]*$ ]]; then
        printf 'Invalid %s: expected a positive integer.\n' "$setting" >&2
        exit 1
    fi
done

# Resolve an explicit path before changing directories, so relative paths are
# interpreted from the caller's directory. Default runs get their own folder.
if (( $# == 1 )); then
    database=$1
    [[ $database == /* ]] || database="$PWD/$database"
    if [[ -e $database ]]; then
        printf 'Database already exists; choose a new path: %s\n' "$database" >&2
        exit 1
    fi
    mkdir -p -- "$(dirname -- "$database")"
else
    mkdir -p -- "$repo_dir/results"
    run_dir=$(mktemp -d "$repo_dir/results/fixed-load-$(date +%Y%m%d-%H%M%S)-XXXXXX")
    database="$run_dir/bench.db"
fi

cd -- "$repo_dir"
run_name=five-sdks
servers=(open62541 o6-python asyncua node-opcua ua-dotnet)
build_flags=(--node --dotnet)
if [[ $with_sdks == 1 ]]; then
    run_name=eight-sdks
    servers+=(milo s2opc gopcua)
    build_flags+=(--sdks)
fi
printf 'Database: %s\n10 clients, %s outstanding per client, %s samples, %s calls/client/sample\n' \
    "$database" "$max_outstanding" "$samples" "$iterations"

"$bench_python" -m bench.build "${build_flags[@]}"
suite=("$bench_python" -m bench.throughput)
"${suite[@]}" new "$database"
"${suite[@]}" config "$database" pair "${servers[@]/#/open62541:}"
"${suite[@]}" config "$database" operation read
"${suite[@]}" config "$database" payload scalar
"${suite[@]}" config "$database" security None
"${suite[@]}" config "$database" mode async
"${suite[@]}" config "$database" clients 10
"${suite[@]}" config "$database" max_outstanding "$max_outstanding"
"${suite[@]}" config "$database" warmup "$warmup"
"${suite[@]}" config "$database" iterations "$iterations"

sample_status=0
"${suite[@]}" sample "$samples" "$database" --name "$run_name" || sample_status=$?
# Preserve a partial report if an SDK fails, without hiding the failure.
"${suite[@]}" show "$database" --name "$run_name" --build-only
printf '\nView the report:\n  %q -m bench.throughput show %q --name %q\n' \
    "$bench_python" "$database" "$run_name"
if (( sample_status != 0 )); then
    exit "$sample_status"
fi

# The suite itself returns success even when individual configurations fail.
# Check that every server actually supplied the requested samples.
"$bench_python" - "$database" "$run_name" "$samples" "${#servers[@]}" <<'PY'
from contextlib import closing
import sys

from common.bench_db import BenchDB

with closing(BenchDB(sys.argv[1], "throughput", name=sys.argv[2])) as store:
    rows = list(store.results.values())
    complete = (
        not store.failures
        and len(rows) == int(sys.argv[4])
        and all(len(row.get("runs", [])) == int(sys.argv[3]) for row in rows)
    )
    if not complete:
        raise SystemExit("Benchmark incomplete: inspect failures and sample counts in the report.")
print(f"All {sys.argv[4]} SDKs completed the requested samples.")
PY
