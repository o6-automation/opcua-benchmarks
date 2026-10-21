# .NET workers

From the repository root, run `python -m bench.build --dotnet`.
The bootstrap supports Linux x64/arm64 and installs SDK 10.0.401 and runtime
10.0.12 locally. OPC Foundation UA-.NETStandard 2.0.1 comes from the official
nuget.org packages. SDK archives and package versions are pinned in
`toolchain.json`, `Directory.Packages.props`, and `locks/` (with content
hashes); restores run in locked mode and builds use Release configuration.
Server address spaces use `AsyncCustomNodeManager`.

The SDK, NuGet cache, and CLI state stay under ignored `deps/` directories.
Linux requires the usual .NET native libraries, including OpenSSL, ICU, zlib,
libstdc++, and Kerberos. Sampling never restores packages or downloads tools.

Select `ua-dotnet` in server suites or `client:server` throughput pairs after
building. Worker preflight checks source, dependency, and artifact fingerprints;
rerun the build after changing them. Saved runs require compatible provenance
when amended.

Encrypted throughput uses Basic256Sha256 SignAndEncrypt, explicit peer trust,
and certificate URI/domain validation. The throughput profile allows 64 MiB
messages and arrays through 4k; server-limit profiles retain SDK defaults.
Managed heap limits reserve 25% of the worker memory share for native/runtime
use and do not guarantee a total RSS bound. Workers use Server GC; JIT uses
runtime defaults.

The throughput runner sizes `MaxQueuedRequestCount` to the largest selected
aggregate pipeline (clients × outstanding requests per client) plus the three
slots UA-.NETStandard 2.0 reserves in the request queue for bootstrap,
reconnect, and control work, with a minimum of the SDK default of 200. This
prevents queue admission failures under the configured load. The effective
limit is recorded in `ua_dotnet_diagnostics.max_queued_request_count`. The
`server_limits` profile keeps the SDK default; these throughput results
describe the configured server.

The contract and clock test helper builds separately from the SDK workers:
`deps/dotnet/dotnet build tests/dotnet/Benchmark.Tests.csproj -c Release -o tests/dotnet/build`.
