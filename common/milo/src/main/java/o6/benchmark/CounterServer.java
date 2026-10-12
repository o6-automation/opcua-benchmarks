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

package o6.benchmark;

import static org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.Unsigned.uint;
import static org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.Unsigned.ulong;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.util.Arrays;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.eclipse.milo.opcua.sdk.core.AccessLevel;
import org.eclipse.milo.opcua.sdk.core.Reference;
import org.eclipse.milo.opcua.sdk.server.AddressSpaceFilter;
import org.eclipse.milo.opcua.sdk.server.ManagedAddressSpaceFragmentWithLifecycle;
import org.eclipse.milo.opcua.sdk.server.OpcUaServer;
import org.eclipse.milo.opcua.sdk.server.OpcUaServerConfigLimits;
import org.eclipse.milo.opcua.sdk.server.SimpleAddressSpaceFilter;
import org.eclipse.milo.opcua.sdk.server.nodes.UaVariableNode;
import org.eclipse.milo.opcua.sdk.server.nodes.filters.AttributeFilter;
import org.eclipse.milo.opcua.sdk.server.nodes.filters.AttributeFilterContext;
import org.eclipse.milo.opcua.sdk.server.sampling.SamplingManagerConfig;
import org.eclipse.milo.opcua.stack.core.AttributeId;
import org.eclipse.milo.opcua.stack.core.NodeIds;
import org.eclipse.milo.opcua.stack.core.UaException;
import org.eclipse.milo.opcua.stack.core.types.builtin.DataValue;
import org.eclipse.milo.opcua.stack.core.types.builtin.DateTime;
import org.eclipse.milo.opcua.stack.core.types.builtin.LocalizedText;
import org.eclipse.milo.opcua.stack.core.types.builtin.NodeId;
import org.eclipse.milo.opcua.stack.core.types.builtin.QualifiedName;
import org.eclipse.milo.opcua.stack.core.types.builtin.StatusCode;
import org.eclipse.milo.opcua.stack.core.types.builtin.Variant;
import org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.UInteger;

/** UInt64 counters advanced by Milo's subscription sampling callbacks. */
public final class CounterServer {
  private final long[] values;
  private final long[] counts;
  private long[] intervals;
  private long start, end, measuredStart, measuredEnd, interval;

  private CounterServer(int items, boolean measureServer) {
    values = new long[items];
    counts = measureServer ? new long[items] : null;
  }

  private synchronized DataValue read(int index) {
    // HotSpot on Linux uses CLOCK_MONOTONIC for System.nanoTime().
    long now = System.nanoTime();
    if (start <= now && now < end) {
      values[index]++;
      if (counts != null && measuredStart <= now && now < measuredEnd) {
        counts[index]++;
        intervals[(int) ((now - measuredStart) / interval)]++;
      }
    }
    return new DataValue(new Variant(ulong(values[index])), StatusCode.GOOD, DateTime.now());
  }

  private synchronized void arm(String line, PrintStream output) {
    String[] parts = line.trim().split("\\s+");
    if (start != 0 || parts.length != 6 || !parts[0].equals("arm")) {
      throw new IllegalArgumentException("invalid control");
    }
    long begin = Long.parseLong(parts[1]);
    long finish = Long.parseLong(parts[2]);
    long measuredBegin = Long.parseLong(parts[3]);
    long measuredFinish = Long.parseLong(parts[4]);
    long period = Long.parseLong(parts[5]);
    if (begin <= System.nanoTime() || measuredBegin < begin || measuredFinish <= measuredBegin
        || finish < measuredFinish || period <= 0 || (measuredFinish - measuredBegin) % period != 0
        || (measuredFinish - measuredBegin) / period > 1000) {
      throw new IllegalArgumentException("invalid measurement window");
    }
    start = begin;
    end = finish;
    measuredStart = measuredBegin;
    measuredEnd = measuredFinish;
    interval = period;
    if (counts != null) intervals = new long[(int) ((measuredEnd - measuredStart) / interval)];
    output.printf(
        "{\"event\":\"armed\",\"start_ns\":%d,\"end_ns\":%d,\"measured_start_ns\":%d,"
            + "\"measured_end_ns\":%d,\"interval_ns\":%d}%n",
        start, end, measuredStart, measuredEnd, interval);
  }

  private synchronized void finish(PrintStream output) {
    if (counts == null) {
      output.println("{\"event\":\"done\"}");
    } else {
      output.printf("{\"event\":\"result\",\"counts\":%s,\"interval_counts\":%s}%n",
          Arrays.toString(counts), Arrays.toString(intervals));
    }
  }

  private final class Counters extends ManagedAddressSpaceFragmentWithLifecycle {
    private final AddressSpaceFilter filter =
        SimpleAddressSpaceFilter.create(getNodeManager()::containsNode);

    Counters(OpcUaServer server) {
      super(server, server.getServerNamespace());
      getLifecycleManager().addStartupTask(this::addNodes);
    }

    private void addNodes() {
      var ns = getServer().getServerNamespace().getNamespaceIndex();
      if (ns.intValue() != 1) throw new IllegalStateException("counter namespace must be 1");
      for (int i = 0; i < values.length; i++) {
        final int index = i;
        String name = "Counter" + i;
        UaVariableNode node = new UaVariableNode.UaVariableNodeBuilder(getNodeContext())
            .setNodeId(new NodeId(ns, uint(i + 1)))
            .setBrowseName(new QualifiedName(ns, name))
            .setDisplayName(LocalizedText.english(name))
            .setDataType(NodeIds.UInt64)
            .setTypeDefinition(NodeIds.BaseDataVariableType)
            .setAccessLevel(AccessLevel.READ_ONLY)
            .setUserAccessLevel(AccessLevel.READ_ONLY)
            .setMinimumSamplingInterval(1.0)
            .setValueRank(-1)
            .setValue(new DataValue(new Variant(ulong(0))))
            .build();
        node.getFilterChain().addLast(new AttributeFilter() {
          @Override
          public Object getAttribute(AttributeFilterContext context, AttributeId attribute) {
            return attribute == AttributeId.Value ? CounterServer.this.read(index) : context.getAttribute(attribute);
          }

          @Override
          public Object readAttribute(AttributeFilterContext context, AttributeId attribute) throws UaException {
            return attribute == AttributeId.Value ? CounterServer.this.read(index) : context.readAttribute(attribute);
          }
        });
        getNodeManager().addNode(node);
        node.addReference(new Reference(node.getNodeId(), NodeIds.Organizes, NodeIds.ObjectsFolder.expanded(), false));
      }
    }

    @Override public AddressSpaceFilter getFilter() { return filter; }
    @Override protected SamplingManagerConfig samplingManagerConfig() { return BenchmarkServer.samplingManagerConfig(); }
  }

  private static void run(String[] args) throws Exception {
    if (args.length != 4) throw new IllegalArgumentException("expected port items sampling_ms measurement");
    int port = Integer.parseInt(args[0]);
    int items = Integer.parseInt(args[1]);
    int sampling = Integer.parseInt(args[2]);
    if (port < 1 || port > 65535 || items < 1 || items > 1048576 || sampling < 1 || sampling > 100
        || !(args[3].equals("client") || args[3].equals("server"))) {
      throw new IllegalArgumentException("invalid counter settings");
    }
    PrintStream output = System.out;
    System.setOut(System.err);
    CounterServer worker = new CounterServer(items, args[3].equals("server"));
    var options = new BenchmarkServer.Options();
    options.port = port;
    OpcUaServer server = BenchmarkServer.createServer(options, new OpcUaServerConfigLimits() {
      @Override public Double getMinPublishingInterval() { return 1.0; }
      @Override public Double getMinSupportedSampleRate() { return 1.0; }
      @Override public UInteger getMaxMonitoredItems() { return uint(items); }
      @Override public UInteger getMaxMonitoredItemsPerSession() { return uint(items); }
      @Override public UInteger getMaxArrayLength() { return uint(1 << 26); }
    });
    Counters counters = worker.new Counters(server);
    var timer = Executors.newSingleThreadScheduledExecutor();
    try {
      counters.startup();
      server.startup().get();
      output.printf("{\"event\":\"ready\",\"sdk\":\"milo\",\"version\":\"%s\",\"runtime\":\"%s\","
          + "\"clock\":\"CLOCK_MONOTONIC\",\"counter_source\":\"read_callback\"}%n",
          OpcUaServer.SDK_VERSION, System.getProperty("java.runtime.version"));
      BufferedReader input = new BufferedReader(new InputStreamReader(System.in));
      String line;
      while ((line = input.readLine()) != null && !line.equals("quit")) {
        worker.arm(line, output);
        timer.schedule(() -> worker.finish(output), Math.max(0, worker.end - System.nanoTime()), TimeUnit.NANOSECONDS);
      }
    } finally {
      timer.shutdownNow();
      counters.shutdown();
      server.shutdown().get();
    }
  }

  public static void main(String[] args) {
    try {
      run(args);
    } catch (Exception error) {
      error.printStackTrace(System.err);
      System.exit(1);
    }
    System.exit(0);
  }
}
