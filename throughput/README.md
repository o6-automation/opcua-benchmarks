# Throughput

Measures Read/Write service throughput with separate client and server processes.
Supported implementations are `open62541`, `o6-python`, `asyncua`, `ua-dotnet`, and
`node-opcua`. The direct asyncua/o6 pairings are excluded. Node and .NET require
[optional builds](../README.md#setup).
The [server-only SDKs](../common/SDK_SERVERS.md), including Rust's `async-opcua`,
can be paired with any supported client, for example `open62541:async-opcua`.

```sh
python -m bench.throughput new bench.db
python -m bench.throughput config bench.db pair open62541:open62541 o6-python:open62541 asyncua:open62541
python -m bench.throughput config bench.db security None
python -m bench.throughput config bench.db payload scalar
python -m bench.throughput sample 3 bench.db --name throughput-1
python -m bench.throughput show bench.db --name throughput-1
```

Inspect all settings with `python -m bench.throughput config bench.db --help`.
The matrix combines `pair`, `operation`, `mode`, `clients`, `security`, and
`payload`. Use `--amend` to top up a saved run and `--skip-failed` to leave its
failed cells untouched. Changing measurement settings requires a new run name.

## Workload

Servers expose 100 writable Int32 variables at `ns=1;i=1001`–`1100`. Clients
use the same deterministic node sequence. Connections, certificates, NodeId
resolution, and warmup precede timing. Clients start through a shared barrier;
aggregate throughput spans the earliest client start to the latest finish.

| Payload | Service contents |
| --- | --- |
| `scalar` | One Int32 on one node |
| `batch:N` | N scalar nodes |
| `array:M` | One node containing M Int32 values |

Array sizes accept integers or `vga`, `hd`, `full_hd`, and `4k`. `iterations`
is a per-client service-call count. Large payloads are limited by:

```text
calls = min(iterations, max(min_service_calls, max_values // values_per_call))
```

Set `max_values` to zero to disable that reduction. Reports show calls/s and
payload MB/s; payload bytes exclude protocol and transport overhead.

`sync` has one outstanding request; `async` uses the configured pipeline depth.
Node's sequential mode still uses asynchronous I/O. Encrypted cases use
Basic256Sha256 SignAndEncrypt and temporary self-signed certificates generated
with `cryptography`, with explicit peer trust.

Worker memory limits and CPU placement reduce interference. Failed cells are
recorded and do not establish a throughput result. Short runs are useful for
checking setup; longer repeated samples are needed for performance comparisons.
