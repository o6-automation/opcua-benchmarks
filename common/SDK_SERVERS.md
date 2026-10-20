# Server-only SDK workers

Additional OPC UA SDKs can be measured as servers, against the
existing clients:

| Name | SDK | Language, toolchain | Source |
| --- | --- | --- | --- |
| `milo` | Eclipse Milo 1.2.0-RC1 (`milo-sdk-server`) | Java, Temurin JDK 21.0.12.1, Maven 3.9.16 | `common/milo/` |
| `s2opc` | S2OPC Toolkit 1.7.3 | C, mbedtls 3.6.7, expat 2.9.0 | `common/s2opc/` |
| `gopcua` | gopcua v0.9.1 (`github.com/gopcua/opcua/server`) | Go 1.27.1 | `common/gopcua/` |
| `async-opcua` | [FreeOpcUa async-opcua 0.19.0](https://github.com/FreeOpcUa/async-opcua/tree/0.19.0) | Rust 1.99.1 | `common/async-opcua/` |
| `node-opcua-fronts` | node-opcua `FrontThreadEngine`, the package of `common/node` | Node.js of `common/node` (`--node` first) | `common/node-fronts/` |

From the repository root, run `python -m bench.build` with `--milo`, `--s2opc`
or `--gopcua`, `--async-opcua`, or use `--node --node-opcua-fronts` for the front-thread variant.
Combine these flags to build multiple SDKs. The bootstrap supports Linux
x86_64. Archives are checksum-pinned in `common/sdk_toolchains.json`; Go
dependencies are pinned by `go.sum`, and Rust dependencies by `Cargo.lock`. Toolchains, caches
and the S2OPC libraries stay under ignored `deps/` directories, and sampling
performs no downloads. Preflight compares sources, build steps, pins and the
built artifacts; rebuild after changing any of them.

Select the names as an `implementation` in server_limits, server_capacity and
[subscription](../subscription/README.md), or as the server half of a throughput
`client:server` pair. These adapters provide servers only.

Each server exposes the address space of `common/servers/open62541_server.c`:
100 writable Int32 scalars at `ns=1;i=1001..1100` and the requested Int32
arrays from `ns=1;i=2001`, organised under the Objects folder, with anonymous
access on one endpoint at `opc.tcp://127.0.0.1:<port>` (SecurityPolicy None, or
Basic256Sha256 SignAndEncrypt with the runner's certificate). Like the
open62541 server, they accept any client certificate. Limits are raised where
an SDK's default would refuse a suite's request: 64 MiB messages, arrays up to
4k frames, 100,000 operations per request, and enough sessions for the widest
client count.

SDK-specific notes, which a reader of the results should know:

- **Milo** keeps the variables in its server namespace (ns=1 is the
  application URI there). Its heap is capped at 75% of the worker's memory
  share instead of an address-space limit, which the JVM cannot start under.
  Milo numbers SecureChannel tokens from 0, which the open62541 client
  mistakes for its empty previous-token slot and rejects as expired, so the
  first connection after each server start failed. The server starts Milo's
  private token counter at 1 (by reflection; there is no setting).
  Milo 1.2 revises sampling intervals up to multiples of 25 ms by default,
  which the subscription suite rejects, so the server turns that bucketing
  off and samples at the requested interval, as Milo 1.1 did.
- **S2OPC** sizes its tables at compile time. The build raises sessions,
  secure connections, sockets, operations per message, chunk counts and the
  array length (`S2OPC_LIMITS` in `bench/build_sdks.py`, recorded in the build
  provenance). Namespace 0 is S2OPC's own base NodeSet, loaded with its expat
  loader; S2OPC requires ns=1 to be the application URI, so the variables
  live there.
- **gopcua** has no hook to validate the value of a Write, so its server
  stores whatever it is sent. The benchmark clients always write the declared
  Int32 shape, so this changes no measured path. Its `GOMEMLIMIT` is set from
  the memory share.
- **async-opcua** is the Rust SDK, distinct from Python's `asyncua`. It supports
  all four suites, including Read/Write scalar, batch and array throughput,
  None and Basic256Sha256 SignAndEncrypt, and both subscription observers.
  Its Tokio runtime sizes its worker pool from the assigned CPU affinity.
  The workload node manager is registered before diagnostics to keep `ns=1`.
  Subscription counters use SDK read callbacks and the public `SyncSampler`
  with a 1 ms polling period: the convenience `SimpleNodeManager` otherwise
  imposes a 100 ms polling floor even when it accepts faster sampling.
  The build applies [session-nonce.patch](async-opcua/session-nonce.patch) to
  the pinned upstream source. It generates a fresh 32-byte ActivateSession
  nonce under SecurityPolicy None, allowing the native clients to connect.
  Encrypted sessions and measured Read/Write/subscription paths are unchanged.
  The source commit, patch, locked dependencies, compiler and binary are
  recorded in build provenance; runtime metadata also identifies the patch.
  Upstream 0.19.0 checks array Write types but accepts mismatched scalar
  types. The benchmark always writes Int32 values; this SDK behavior is retained.
  Its notification queues also set an overflow information bit when replacing
  a queued value at size one. The shared client accepts Good-severity values
  with information bits and still rejects Bad/Uncertain status, wrong types
  and decreasing counters.
- **node-opcua-fronts** is the same node-opcua package as the `node-opcua`
  row, served by `FrontThreadEngine` instead of one `OPCUAServer` thread: the
  main thread is the engine (it owns the compact store and applies every
  Write), and one front thread per CPU of the server's share but one listens on
  the same port (SO_REUSEPORT). Linux spreads connections over the fronts by
  hash, so a single-client case uses one front. `O6_NODE_FRONTS` overrides the
  count; the server logs the count it used.

For an async-opcua throughput run:

```sh
python -m bench.build --async-opcua
python -m bench.throughput new rust.db
python -m bench.throughput config rust.db pair open62541:async-opcua
python -m bench.throughput sample 3 rust.db --name rust
```

For the other suites, initialize the suite with `new`, set `implementation`
to `async-opcua`, and sample with the usual suite command. A build needs network
access; measurements use only the installed artifacts.
