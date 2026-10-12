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

using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Opc.Ua;

namespace Benchmark.Common;

public sealed class QuietTelemetry : ITelemetryContext
{
    public ILoggerFactory LoggerFactory => NullLoggerFactory.Instance;
    public ActivitySource ActivitySource { get; } = new("Benchmark");
    public Meter CreateMeter() => new("Benchmark");
    public Meter CreateMeter(Assembly assembly) => CreateMeter();
    public ActivitySource GetActivitySource(Assembly assembly) => ActivitySource;
}

public static class Security
{
    public static readonly ITelemetryContext Telemetry = new QuietTelemetry();

    private static X509Certificate2 Certificate(WorkerOptions options, string uri)
    {
        using var rsa = RSA.Create(2048);
        if (options.Get("certificate") is string path && path.Length > 0)
        {
            byte[] key = File.ReadAllBytes(options.Get("private-key"));
            try { rsa.ImportRSAPrivateKey(key, out _); }
            catch (CryptographicException) { rsa.ImportPkcs8PrivateKey(key, out _); }
            using var certificate = X509CertificateLoader.LoadCertificateFromFile(path);
            return certificate.CopyWithPrivateKey(rsa);
        }
        var request = new CertificateRequest("CN=o6 benchmark", rsa, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
        var san = new SubjectAlternativeNameBuilder();
        san.AddUri(new Uri(uri));
        san.AddDnsName("localhost");
        san.AddIpAddress(IPAddress.Loopback);
        request.CertificateExtensions.Add(san.Build());
        request.CertificateExtensions.Add(new X509BasicConstraintsExtension(false, false, 0, true));
        request.CertificateExtensions.Add(new X509KeyUsageExtension(X509KeyUsageFlags.DigitalSignature | X509KeyUsageFlags.KeyEncipherment | X509KeyUsageFlags.DataEncipherment | X509KeyUsageFlags.NonRepudiation, true));
        return request.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-1), DateTimeOffset.UtcNow.AddDays(1));
    }

    public static async Task<ApplicationConfiguration> ConfigurationAsync(WorkerOptions options, bool server, string pkiPath)
    {
        string uri = "urn:o6:benchmark:" + (server ? "server" : "client");
        bool encrypted = options.Security != "None";
        var trusted = new CertificateTrustList { StoreType = "Directory", StorePath = Path.Combine(pkiPath, "trusted") };
        if (encrypted)
            trusted.TrustedCertificates = [new CertificateIdentifier { RawData = File.ReadAllBytes(options.Get("trust-certificate")) }];
        // 2.0 loads the application certificate from a store; keep it in the per-process PKI directory.
        var own = new CertificateIdentifier { StoreType = "Directory", StorePath = Path.Combine(pkiPath, "own") };
        using (var certificate = Certificate(options, uri))
        using (var store = new DirectoryCertificateStore(Telemetry))
        {
            store.Open(own.StorePath, false);
            using var stored = Opc.Ua.Security.Certificates.Certificate.From(certificate);
            await store.AddAsync(stored);
            own.Thumbprint = certificate.Thumbprint;
        }
        var config = new ApplicationConfiguration(Telemetry)
        {
            ApplicationName = "o6 benchmark " + (server ? "server" : "client"),
            ApplicationUri = uri,
            ProductUri = "urn:o6:benchmark",
            ApplicationType = server ? ApplicationType.Server : ApplicationType.Client,
            SecurityConfiguration = new SecurityConfiguration
            {
                ApplicationCertificate = own,
                TrustedPeerCertificates = trusted,
                TrustedIssuerCertificates = new CertificateTrustList { StoreType = "Directory", StorePath = Path.Combine(pkiPath, "issuers") },
                RejectedCertificateStore = new CertificateStoreIdentifier { StoreType = "Directory", StorePath = Path.Combine(pkiPath, "rejected") },
                AutoAcceptUntrustedCertificates = false,
                RejectSHA1SignedCertificates = true,
                MinimumCertificateKeySize = 2048,
                AddAppCertToTrustedStore = false
            },
            TransportQuotas = Quotas(options),
            ClientConfiguration = new ClientConfiguration { DefaultSessionTimeout = 60_000 },
            ServerConfiguration = ServerConfiguration(options)
        };
        return config;
    }

    public static TransportQuotas Quotas(WorkerOptions options)
    {
        var quotas = new TransportQuotas();
        if (options.Profile == "throughput")
        {
            quotas.OperationTimeout = Contract.RequestTimeout;
            quotas.MaxMessageSize = Contract.MaxMessageSize;
            quotas.MaxByteStringLength = Contract.MaxMessageSize;
            quotas.MaxArrayLength = Contract.MaxArrayLength;
        }
        return quotas;
    }

    public static ServerConfiguration ServerConfiguration(WorkerOptions options) => new()
    {
        BaseAddresses = [$"opc.tcp://127.0.0.1:{options.Port}"],
        SecurityPolicies = [new ServerSecurityPolicy
        {
            SecurityMode = options.Security == "None" ? MessageSecurityMode.None : MessageSecurityMode.SignAndEncrypt,
            SecurityPolicyUri = options.Security == "None" ? SecurityPolicies.None : SecurityPolicies.Basic256Sha256
        }],
        UserTokenPolicies = [new UserTokenPolicy(UserTokenType.Anonymous)],
        // Admit the throughput suite's full aggregate pipeline. Keep the
        // server_limits profile at the SDK constructor default.
        MaxQueuedRequestCount = options.Profile == "throughput"
            ? Math.Max(new ServerConfiguration().MaxQueuedRequestCount, options.MaxQueuedRequests)
            : new ServerConfiguration().MaxQueuedRequestCount,
        // No discovery registration or persistent stores; resource quotas retain
        // their library defaults in the server_limits experiment.
        RegistrationEndpoint = null
    };
}

public sealed class PkiDirectory : IDisposable
{
    public string Path { get; }
    public PkiDirectory()
    {
        string? root = Environment.GetEnvironmentVariable("O6_BENCHMARK_PKI_ROOT");
        Path = root is null ? Directory.CreateTempSubdirectory("o6-dotnet-").FullName
            : System.IO.Path.Combine(root, $"dotnet-{Environment.ProcessId}");
        Directory.CreateDirectory(Path);
    }
    public void Dispose() => Directory.Delete(Path, true);
}
