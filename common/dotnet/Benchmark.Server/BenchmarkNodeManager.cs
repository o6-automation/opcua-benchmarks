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

using Benchmark.Common;
using Opc.Ua;
using Opc.Ua.Server;

namespace Benchmark.Server;

public sealed class BenchmarkNodeManager : AsyncCustomNodeManager
{
    private readonly int[] sizes;
    public BenchmarkNodeManager(IServerInternal server, ApplicationConfiguration configuration, int[] sizes)
        : base(server, configuration, server.Telemetry.CreateLogger<BenchmarkNodeManager>(), Contract.Namespace)
    {
        this.sizes = sizes;
    }

    public override async ValueTask CreateAddressSpaceAsync(IDictionary<NodeId, IList<IReference>> externalReferences,
        CancellationToken cancellationToken = default)
    {
        if (NamespaceIndex != 1) throw new InvalidOperationException($"Benchmark namespace must be 1, got {NamespaceIndex}");
        if (!externalReferences.TryGetValue(ObjectIds.ObjectsFolder, out var references))
            externalReferences[ObjectIds.ObjectsFolder] = references = new List<IReference>();
        for (int i = 0; i < Contract.NodeCount; i++)
            await AddVariableAsync(Contract.FirstNodeId + (uint)i, i, references, cancellationToken);
        for (int i = 0; i < sizes.Length; i++)
            await AddVariableAsync(Contract.FirstArrayId + (uint)i, Contract.ArrayPayload(sizes[i]), references, cancellationToken);
    }

    private async ValueTask AddVariableAsync(uint id, object value, IList<IReference> references, CancellationToken cancellationToken)
    {
        var node = new BaseDataVariableState(null)
        {
            NodeId = new NodeId(id, NamespaceIndex),
            BrowseName = new QualifiedName($"Value{id}", NamespaceIndex),
            DisplayName = new LocalizedText($"Value{id}"),
            TypeDefinitionId = VariableTypeIds.BaseDataVariableType,
            ReferenceTypeId = ReferenceTypeIds.Organizes,
            DataType = DataTypeIds.Int32,
            ValueRank = value is int[] ? ValueRanks.OneDimension : ValueRanks.Scalar,
            ArrayDimensions = value is int[] array ? [(uint)array.Length] : default,
            AccessLevel = AccessLevels.CurrentReadOrWrite,
            UserAccessLevel = AccessLevels.CurrentReadOrWrite,
            Value = value is int[] payload ? new Variant(payload) : new Variant((int)value),
            StatusCode = StatusCodes.Good,
            Timestamp = DateTimeUtc.Now
        };
        node.AddReference(ReferenceTypeIds.Organizes, true, ObjectIds.ObjectsFolder);
        references.Add(new NodeStateReference(ReferenceTypeIds.Organizes, false, node.NodeId));
        await AddPredefinedNodeAsync(SystemContext, node, cancellationToken);
    }
}
