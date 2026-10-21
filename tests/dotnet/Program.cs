// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 o6 Automation GmbH

using System.Text.Json;
using Benchmark.Common;

if (args[0] == "clock")
    Console.WriteLine(MonotonicClock.Now());
else if (args[0] == "lcg")
{
    var cursor = new NodeCursor(uint.Parse(args[1]));
    Console.WriteLine(JsonSerializer.Serialize(Enumerable.Range(0, 1000).Select(_ => cursor.Next()).ToArray()));
}
else if (args[0] == "contract")
{
    var sizes = new[] { 1000, 10, 100 };
    Console.WriteLine(JsonSerializer.Serialize(new { first = Contract.FirstNodeId, count = Contract.NodeCount,
        arrays = sizes.Select(size => Contract.ArrayNodeId(sizes, size)), payload = Contract.ArrayPayload(1010) }));
}
else throw new ArgumentException("Expected clock, lcg, or contract");
