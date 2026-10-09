# Node.js workers

From the repository root, run `python -m bench.build --node`.
The bootstrap supports Linux x64/arm64 and installs Node.js 24.21.0, npm
11.19.0, and node-opcua 2.187.1. Archives are checksum-pinned in `toolchain.json`;
`package-lock.json` pins the public npm dependency graph.

The runtime and npm state stay under ignored `deps/` directories. Installation
uses `npm ci --omit=dev --no-audit --no-fund --ignore-scripts`. Sampling performs
no downloads. Preflight checks runtime, package, and worker fingerprints;
rebuild after changing these inputs.

Select `node-opcua` in server suites or throughput `client:server` pairs. For
Node/.NET pairs, build both optional toolchains. Each worker uses one event loop
and one session. `sync` awaits one request at a time; `async` refills a bounded
request window. Measured requests do not silently retry failed sessions.

Encrypted workers use Basic256Sha256 SignAndEncrypt with explicit peer trust
and temporary PKI directories. Throughput permits 64 MiB messages and arrays
through 4k; resource-limit profiles retain SDK defaults. Heap and RSS monitoring
bound memory use. Saved runs require compatible Node provenance when amended.
