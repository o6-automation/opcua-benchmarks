// SPDX-License-Identifier: AGPL-3.0-or-later
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as published
// by the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program. If not, see <https://www.gnu.org/licenses/>.
//
//    Copyright 2026 (c) o6 Automation GmbH (Author: Daniel Opitz)

using System.Text.Json;
using Benchmark.Common;
using Benchmark.Client;
using Opc.Ua;
using Opc.Ua.Client;

try
{
    var options = new WorkerOptions(args);
    if (options.RuntimeInfo) { RuntimeInfo.Print(options); return 0; }
    using var pki = new PkiDirectory();
    var configuration = await Security.ConfigurationAsync(options, false, pki.Path);
    await configuration.ValidateAsync(ApplicationType.Client);
    string policy = options.Security == "None" ? SecurityPolicies.None : SecurityPolicies.Basic256Sha256;
    var mode = options.Security == "None" ? MessageSecurityMode.None : MessageSecurityMode.SignAndEncrypt;
    using var discovery = await DiscoveryClient.CreateAsync(configuration, new Uri(options.Endpoint), EndpointConfiguration.Create(configuration));
    var endpoints = await discovery.GetEndpointsAsync(default);
    var endpoint = (endpoints.ToArray() ?? []).FirstOrDefault(candidate => candidate.SecurityPolicyUri == policy && candidate.SecurityMode == mode
        && candidate.EndpointUrl is string url && new Uri(url).Scheme == "opc.tcp")
        ?? throw new InvalidOperationException("Server does not offer the requested security policy and mode");
    // Use the requested host, as CoreClientUtils does, while retaining certificate
    // domain and application-URI validation during session creation.
    var address = new Uri(options.Endpoint);
    endpoint.EndpointUrl = new UriBuilder(endpoint.EndpointUrl!) { Host = address.Host, Port = address.Port }.ToString();
    var configuredEndpoint = new ConfiguredEndpoint(null, endpoint, EndpointConfiguration.Create(configuration));
    using var session = await new DefaultSessionFactory(Security.Telemetry).CreateAsync(configuration,
        configuredEndpoint, false, true, "benchmark", 60_000, new UserIdentity(), default);
    session.KeepAliveInterval = Contract.RequestTimeout;
    session.OperationTimeout = Contract.RequestTimeout;
    Console.Error.WriteLine($"endpoint: {endpoint.SecurityPolicyUri} {endpoint.SecurityMode}");
    var workload = new Workload(session, options);
    long checksum = options.Mode == "async" ? await workload.RunAsync(options.Warmup) : workload.RunSync(options.Warmup);
    Console.WriteLine("O6_BENCHMARK_READY");
    Console.Out.Flush();
    if (Console.ReadLine() is null) throw new EndOfStreamException("benchmark barrier closed before release");
    long start = MonotonicClock.Now();
    checksum ^= options.Mode == "async" ? await workload.RunAsync(options.Iterations) : workload.RunSync(options.Iterations);
    long end = MonotonicClock.Now();
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        benchmark = options.Operation + "/" + options.Mode,
        implementation = "ua-dotnet", operations = options.Iterations,
        values_per_operation = options.BatchSize * options.ArraySize,
        start_ns = start, end_ns = end, checksum = checksum.ToString()
    }));
    Console.Out.Flush();
    await session.CloseAsync(5_000, true);
    return 0;
}
catch (Exception error)
{
    Console.Error.WriteLine(error);
    return 1;
}
