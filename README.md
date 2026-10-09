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
python -m bench.build --sdks     # Milo, S2OPC and gopcua servers (x64 only)
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

## Fixed-load comparison

For a scalar Read comparison of all five server SDKs using the same native
client, run:

```sh
./run-fixed-load.sh
```

The script builds the workers, creates a new database under `results/`, and
records three samples with 10 concurrent clients and 100 outstanding requests
per client (1,000 total). Each client performs 100,000 measured calls after
10,000 warmup calls. An editable configuration and HTML report are saved in the
database. The script prints the command to view the report and exits nonzero
if any SDK is short of the requested sample count.

Use Bash, not `sh`. Override `SAMPLES`, `ITERATIONS`, `WARMUP`, or
`MAX_OUTSTANDING` as needed; the last setting is per client. `WITH_SDKS=1`
adds the Milo, S2OPC and gopcua servers (eight in all). The runner's
120-second collection timeout can require fewer iterations on slower hosts.
Workers rebuilt since a saved run can change its fingerprint; start a new
named run instead of amending incompatible measurements.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the DCO sign-off requirement and
additional contributor license grant to o6 Automation GmbH.

## License

Copyright (c) 2026 o6 Automation GmbH (Author: Daniel Opitz)

Licensed under the [GNU Affero General Public License, version 3 or later](LICENSE)
(`AGPL-3.0-or-later`). Third-party dependencies retain their own licenses.
