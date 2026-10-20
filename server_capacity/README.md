# Server capacity

Searches for high scalar Read throughput using one fixed open62541 C client
against each selected server SDK. Discovery varies client count and pipeline
depth; fresh confirmation samples repeat promising candidates.
The [server-only SDKs](../common/SDK_SERVERS.md), including `async-opcua` (Rust),
are available as opt-in implementations.

```sh
python -m bench.server_capacity new bench.db
python -m bench.server_capacity config bench.db implementation open62541 o6-python asyncua
python -m bench.server_capacity sample 3 bench.db --name capacity-1
python -m bench.server_capacity show bench.db --name capacity-1
```

Defaults include Node and .NET; build their [optional workers](../README.md#setup)
or narrow `implementation` first. `config bench.db --help` lists the settings.

`probe_ms` controls discovery windows and `confirm_ms` the repeated measurements.
`max_clients`, `max_outstanding`, `max_probes`, and `budget_seconds` bound the
search. Warmup and drain are outside the measurement window. Confirmation uses
at least three samples and should be increased for stable comparisons.

A capped or interrupted search provides observations, not proof of the server's
maximum capacity. Reports show search status and failed measurements alongside
successful candidates. `--amend` continues a saved run within the configured
budget; use a new run name when changing measurement settings.
