# OPC UA benchmarks

Benchmarks for open62541, o6\Python, asyncua, OPC Foundation UA-.NETStandard,
and node-opcua, plus the Eclipse Milo, S2OPC and gopcua servers.
Configuration, measurements, and HTML reports are stored in SQLite.

| Suite | Measures |
| --- | --- |
| [throughput](throughput/README.md) | Read/write throughput across client/server pairings, payload sizes, and security policies |
| [server_limits](server_limits/README.md) | Throughput and latency as client count and pipeline depth increase |
| [server_capacity](server_capacity/README.md) | Adaptive search for scalar Read server capacity |
| [subscription](subscription/README.md) | Application counter progress and monitored-item capacity |

## Setup

Use Linux with Python 3.11–3.14, Git, a C compiler, CMake 3.21+, and OpenSSL
development headers (`build-essential cmake libssl-dev python3-venv` on Debian/Ubuntu).
Run these commands from the repository root:

```sh
git submodule update --init --recursive
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m bench.build
```

Optional workers have local, pinned toolchains for Linux x64/arm64:

```sh
python -m bench.build --node     # Node.js and node-opcua
python -m bench.build --dotnet   # .NET and OPC Foundation workers
python -m bench.build --milo     # Eclipse Milo (x64 only)
python -m bench.build --gopcua   # gopcua (x64 only)
python -m bench.build --s2opc    # S2OPC (x64 only)
```

The flags can be combined. Setup downloads dependencies; sampling uses the
installed artifacts. See [Node](common/node/README.md),
[.NET](common/dotnet/README.md) and [server-only SDKs](common/SDK_SERVERS.md)
for details. Use `--rebuild` for a clean build
and `--verbose` for compiler output.

## Run

```sh
python -m bench bench.db                 # create settings and open the scheduler
python -m bench.throughput new bench.db  # configure a single suite without the UI
python -m bench.throughput config bench.db pair open62541:open62541
python -m bench.throughput sample 3 bench.db --name first-run
python -m bench.show bench.db --name first-run
```

`config <db> --help` lists each suite's settings. Narrow the matrix before
sampling: defaults can take a long time, and server-capacity and subscription
defaults include the optional Node and .NET workers.

Use `sample ... --name <name> --amend` to continue a saved run. Keep its measurement
settings consistent. Reports are stored in the database and served on localhost;
`--build-only` generates them without starting the server. `bench.show --force`
refreshes cached reports. Stop serving with Ctrl-C.

Benchmarks use local port 4840 and can saturate CPUs and memory. Run on an idle
machine and keep setup, hardware, and configuration consistent when comparing
results. These workloads measure specific operations, not overall SDK quality.

The pip-installed o6 evaluation package enforces its own runtime limit per
process. Longer cases may fail or remain incomplete. This repository does not
extend that limit.

## Singlecore Server Benchmark

The script compares server SDKs in two scenarios:

- **Reads:** Across all nine SDKs, including `node-opcua-fronts`, ten native
  clients keep 100 scalar Read requests in flight each, without encryption and
  with Basic256Sha256, to load the server and compare throughput and encryption
  overhead.
- **Subscriptions:** A linear sweep of monitored items uses 10 ms and 100 ms
  sampling with 1,000 ms publishing. Client-side progress shows how capacity
  changes with sampling frequency across all nine SDKs.

Servers are pinned to one logical CPU, with clients on separate physical cores.
The preset requires Linux and at least two available physical cores. Saved runs
from before single-core pinning was enforced can still be plotted; start a new
database to measure with the corrected CPU allocation.

Start a fresh run or continue the latest saved run:

```sh
python singlecore_server_bench.py
python singlecore_server_bench.py --continue
```

Edit the settings at the top of the script to adjust the preset. It builds the
workers and saves results and reports to
`results/bench-singlecore-server-<YYYY-MM-DD>.db`. A normal run clears the
previous results; `--continue` keeps completed measurements and resumes with
the saved settings, including across dates.
An optional database path selects another file for running, continuing, or plotting.

Create the throughput and 10 ms / 100 ms subscription plots from saved results:

```sh
python singlecore_server_bench.py --plot
python singlecore_server_bench.py results/bench-singlecore-server-2026-10-12.db --plot
```

Without a database argument, `--plot` uses today's database. PNGs are saved in
`<database-name>.plots/` beside it; plotting does not run benchmarks or clear results.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the DCO sign-off requirement and
additional contributor license grant to o6 Automation GmbH.

## License

Copyright (c) 2026 o6 Automation GmbH (Author: Daniel Opitz)

Licensed under the [GNU Affero General Public License, version 3 or later](LICENSE)
(`AGPL-3.0-or-later`). Third-party dependencies retain their own licenses.

## Extensions

The harness supports additional benchmark suites and build steps through
[common.extensions](common/extensions.py), with shared configuration, scheduling,
result storage, and reporting.
