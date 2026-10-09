# Server-only SDK workers

Three further OPC UA server SDKs can be measured as servers, against the
existing clients:

| Name | SDK | Language, toolchain | Source |
| --- | --- | --- | --- |
| `milo` | Eclipse Milo 1.1.8 (`milo-sdk-server`) | Java, Temurin JDK 21.0.12.1, Maven 3.9.16 | `common/milo/` |
| `s2opc` | S2OPC Toolkit 1.7.3 | C, mbedtls 3.6.7, expat 2.9.0 | `common/s2opc/` |
| `gopcua` | gopcua v0.9.1 (`github.com/gopcua/opcua/server`) | Go 1.27.1 | `common/gopcua/` |

From the repository root, run `python -m bench.build --sdks`, or pick one with
`--milo`, `--s2opc`, `--gopcua`. The bootstrap supports Linux
x86_64. Archives are checksum-pinned in `common/sdk_toolchains.json`; Go
dependencies are pinned by `go.sum`. Toolchains, caches
and the S2OPC libraries stay under ignored `deps/` directories, and sampling
performs no downloads. Preflight compares sources, build steps, pins and the
built artifacts; rebuild after changing any of them.

Select the names as an `implementation` in server_limits and server_capacity,
or as the server half of a throughput `client:server` pair. They have no
client, and the subscription suite does not use them.

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
- **S2OPC** sizes its tables at compile time. The build raises sessions,
  secure connections, sockets, operations per message, chunk counts and the
  array length (`S2OPC_LIMITS` in `bench/build_sdks.py`, recorded in the build
  provenance). Namespace 0 is S2OPC's own base NodeSet, loaded with its expat
  loader; S2OPC requires ns=1 to be the application URI, so the variables
  live there.
- **gopcua** has no hook to validate the value of a Write, so its server
  stores whatever it is sent. The benchmark clients always write the declared
  Int32 shape, so this changes no measured path, but the other servers check
  the type and gopcua does not. Its `GOMEMLIMIT` is set from the memory share.

async-opcua (Rust, the maintained successor of the `opcua` crate) was
tried and left out: under SecurityPolicy None it returns a null server nonce
from ActivateSession (hard-coded in 0.19.0), where Part 4 requires one of at
least 32 bytes, and the open62541 client rejects the session.
