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

using System.Runtime.InteropServices;
using Benchmark.Common;
using Benchmark.Server;
using Opc.Ua;

try
{
    if (args.Length > 0 && args[0] == "--counter") return await CounterWorker.Run(args[1..]);
    var options = new WorkerOptions(args);
    if (options.RuntimeInfo) { RuntimeInfo.Print(options); return 0; }
    using var pki = new PkiDirectory();
    var config = await Security.ConfigurationAsync(options, true, pki.Path);
    await config.ValidateAsync(ApplicationType.Server);
    using var server = new BenchmarkServer(options);
    var stopped = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
    using var interrupt = PosixSignalRegistration.Create(PosixSignal.SIGINT, context => { context.Cancel = true; stopped.TrySetResult(); });
    using var terminate = PosixSignalRegistration.Create(PosixSignal.SIGTERM, context => { context.Cancel = true; stopped.TrySetResult(); });
    await server.StartAsync(config);
    Console.WriteLine("benchmark server ready");
    Console.Out.Flush();
    await stopped.Task;
    await server.StopAsync();
    return 0;
}
catch (Exception error)
{
    Console.Error.WriteLine(error);
    return 1;
}
