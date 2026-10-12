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

using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using Benchmark.Common;
using Opc.Ua;
using Opc.Ua.Server;

namespace Benchmark.Server;

public sealed class CounterNodes : AsyncCustomNodeManager
{
    private readonly int count;
    private readonly ulong[] values;
    private ulong[]? measured, intervals;
    private long start, end, measuredStart, measuredEnd, intervalNs;
    // Serializes read callbacks, arming, and result serialization. Owned by the
    // worker because AsyncCustomNodeManager has no node manager lock.
    private readonly Lock counterLock = new();

    public CounterNodes(IServerInternal server, ApplicationConfiguration config, int count)
        : base(server, config, true, server.Telemetry.CreateLogger<CounterNodes>(), Contract.Namespace)
    { this.count = count; values = new ulong[count]; }

    public override async ValueTask CreateAddressSpaceAsync(IDictionary<NodeId, IList<IReference>> externalReferences,
        CancellationToken cancellationToken = default)
    {
        if (NamespaceIndex != 1) throw new InvalidOperationException("Expected namespace 1");
        if (!externalReferences.TryGetValue(ObjectIds.ObjectsFolder, out var references))
            externalReferences[ObjectIds.ObjectsFolder] = references = new List<IReference>();
        for (int i = 0; i < count; i++)
        {
            var node = new BaseDataVariableState(null)
            {
                NodeId = new NodeId((uint)i + 1, NamespaceIndex),
                BrowseName = new QualifiedName($"Counter{i}", NamespaceIndex), DisplayName = new LocalizedText($"Counter{i}"),
                TypeDefinitionId = VariableTypeIds.BaseDataVariableType, ReferenceTypeId = ReferenceTypeIds.Organizes,
                DataType = DataTypeIds.UInt64, ValueRank = ValueRanks.Scalar, MinimumSamplingInterval = 1,
                AccessLevel = AccessLevels.CurrentRead, UserAccessLevel = AccessLevels.CurrentRead,
                Value = new Variant((ulong)0), StatusCode = StatusCodes.Good, Timestamp = DateTimeUtc.Now
            };
            int index = i;
            node.OnReadValue = (ISystemContext context, NodeState state, NumericRange range,
                QualifiedName encoding, ref Variant value, ref StatusCode status, ref DateTimeUtc timestamp) =>
            {
                lock (counterLock)
                {
                    long admission = CounterWorker.Now();
                    if (start != 0 && admission >= start && admission < end)
                    {
                        values[index]++;
                        if (measured is not null && admission >= measuredStart && admission < measuredEnd)
                        {
                            measured[index]++;
                            intervals![(admission - measuredStart) / intervalNs]++;
                        }
                    }
                    value = new Variant(values[index]);
                    status = StatusCodes.Good;
                    timestamp = DateTimeUtc.Now;
                    return ServiceResult.Good;
                }
            };
            node.AddReference(ReferenceTypeIds.Organizes, true, ObjectIds.ObjectsFolder);
            references.Add(new NodeStateReference(ReferenceTypeIds.Organizes, false, node.NodeId));
            await AddPredefinedNodeAsync(SystemContext, node, cancellationToken);
        }
    }

    public void Arm(long start, long end, long measuredStart, long measuredEnd, long intervalNs, bool measureServer)
    {
        lock (counterLock)
        {
            this.start = start; this.end = end;
            this.measuredStart = measuredStart; this.measuredEnd = measuredEnd; this.intervalNs = intervalNs;
            if (measureServer)
            {
                measured = new ulong[count];
                intervals = new ulong[(measuredEnd - measuredStart) / intervalNs];
            }
        }
    }

    public object Result()
    {
        // Sampling threads and the completion task share this lock. After the
        // end deadline, callbacks cannot mutate the arrays being serialized.
        lock (counterLock) return new { @event = "result", counts = measured, interval_counts = intervals };
    }

}

public sealed class CounterServer(int count) : StandardServer(Security.Telemetry)
{
    public CounterNodes? Nodes { get; private set; }
    protected override IMasterNodeManager CreateMasterNodeManager(IServerInternal server, ApplicationConfiguration config)
    {
        Nodes = new CounterNodes(server, config, count);
        return new MasterNodeManager(server, config, null, Nodes);
    }
}

public static class CounterWorker
{
    [StructLayout(LayoutKind.Sequential)]
    private struct Timespec { public long Seconds, Nanoseconds; }
    [DllImport("libc", EntryPoint = "clock_gettime", SetLastError = true)]
    private static extern int GetTime(int id, out Timespec value);

    internal static long Now()
    {
        if (GetTime(1, out var value) != 0) throw new InvalidOperationException("CLOCK_MONOTONIC unavailable");
        return checked(value.Seconds * 1_000_000_000 + value.Nanoseconds);
    }

    public static async Task<int> Run(string[] args)
    {
        TextWriter protocol = Console.Out;
        Console.SetOut(Console.Error);
        void Emit(object value) { protocol.WriteLine(JsonSerializer.Serialize(value)); protocol.Flush(); }
        try
        {
            if (args.Length != 4) throw new ArgumentException("Expected port, items, sampling_ms, measurement");
            int port = int.Parse(args[0]), count = int.Parse(args[1]), sampling = int.Parse(args[2]);
            bool measureServer = args[3] == "server";
            if (args[3] is not ("server" or "client") || count < 1 || count > 1048576 || sampling < 1 || sampling > 100)
                throw new ArgumentException("Invalid worker arguments");
            var shared = new WorkerOptions(["--profile", "server_limits", "--port", port.ToString(CultureInfo.InvariantCulture)]);
            using var pki = new PkiDirectory();
            var config = await Security.ConfigurationAsync(shared, true, pki.Path);
            var limits = config.ServerConfiguration!;
            limits.ShutdownDelay = 0;
            limits.MinPublishingInterval = 1;
            limits.PublishingResolution = 1;
            limits.MaxPublishingInterval = 86400_000;
            limits.MaxNotificationQueueSize = 1;
            limits.MaxNotificationsPerPublish = int.MaxValue;
            limits.MaxPublishRequestCount = 4;
            limits.AvailableSamplingRates = [new SamplingRateGroup(sampling, 1, 0)];
            limits.OperationLimits.MaxMonitoredItemsPerCall = 256;
            await config.ValidateAsync(ApplicationType.Server);
            using var server = new CounterServer(count);
            await server.StartAsync(config);
            using var cancellation = new CancellationTokenSource();
            Task? producer = null;
            try
            {
                Emit(new { @event = "ready", sdk = "ua-dotnet", runtime = Environment.Version.ToString(), clock = "CLOCK_MONOTONIC",
                    counter_source = "read_callback" });
                while (true)
                {
                    Task<string?> input = Console.In.ReadLineAsync();
                    if (producer is not null && !producer.IsCompleted) await Task.WhenAny(input, producer);
                    if (producer is not null && producer.IsCompleted) await producer;
                    string? line = await input;
                    if (line is null || line == "quit") break;
                    string[] parts = line.Split(' ');
                    if (parts.Length != 6 || parts[0] != "arm" || producer is not null) throw new ArgumentException("Invalid control");
                    long start = long.Parse(parts[1]), end = long.Parse(parts[2]);
                    long measuredStart = long.Parse(parts[3]), measuredEnd = long.Parse(parts[4]);
                    long intervalNs = long.Parse(parts[5]);
                    if (start <= Now() || measuredStart < start || measuredEnd <= measuredStart || end < measuredEnd)
                        throw new ArgumentException("Missed arming deadline");
                    if (intervalNs <= 0 || (measuredEnd - measuredStart) % intervalNs != 0)
                        throw new ArgumentException("Invalid publishing interval");
                    server.Nodes!.Arm(start, end, measuredStart, measuredEnd, intervalNs, measureServer);
                    Emit(new { @event = "armed", start_ns = start, end_ns = end,
                        measured_start_ns = measuredStart, measured_end_ns = measuredEnd, interval_ns = intervalNs });
                    producer = Task.Run(async () =>
                    {
                        while (Now() < end)
                        {
                            await Task.Delay((int)Math.Max(1, Math.Ceiling((end - Now()) / 1e6)), cancellation.Token);
                        }
                        if (measureServer) Emit(server.Nodes!.Result());
                        else Emit(new { @event = "done" });
                    }, cancellation.Token);
                }
            }
            finally
            {
                cancellation.Cancel();
                try { if (producer is not null) await producer; }
                catch (OperationCanceledException) when (cancellation.IsCancellationRequested) { }
                finally
                {
                    Console.Error.WriteLine("Counter worker: stopping SDK");
                    await server.StopAsync();
                    Console.Error.WriteLine("Counter worker: SDK stopped");
                }
            }
            return 0;
        }
        catch (Exception error)
        {
            Emit(new { @event = "error", message = error.ToString() });
            return 2;
        }
    }
}
