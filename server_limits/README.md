# Server limits

Measures server throughput, latency, errors, and memory as client count and
outstanding requests increase. A fixed open62541 C client drives each selected
server: `open62541`, `o6-python`, `asyncua`, or optional `ua-dotnet`/`node-opcua`.
The workload uses scalar Reads without encryption.
The [server-only SDKs](../common/SDK_SERVERS.md), including `async-opcua` (Rust),
are also supported through `implementation`.

```sh
python -m bench.server_limits new bench.db
python -m bench.server_limits config bench.db implementation open62541 o6-python asyncua
python -m bench.server_limits sample 7 bench.db --name limits-1
python -m bench.server_limits show bench.db --name limits-1
```

Use `config bench.db --help` to inspect settings. The load ladder doubles client
count and pipeline depth between the configured minima and maxima. Each step
has warmup, measurement, and drain windows; stop thresholds bound failed or
unproductive load increases. Restrict the maxima for a short run.

At least seven samples are retained per cell. Each sampling invocation that
adds measurements discards its first new sample. `--amend` tops up stored
samples; `--skip-failed` leaves failed configurations alone.

Clients and servers use separate CPU sets where available. Workers receive
OOM priority so excessive load preferentially terminates benchmark processes.
Latency histograms and error counts accompany throughput; high request rates
with errors should not be treated as usable capacity.

See [setup and runtime limitations](../README.md).
