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
//    Copyright 2026 (c) Sterfive (Author: Etienne Rossignon)

package o6.benchmark;

import static org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.Unsigned.uint;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.PrivateKey;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.RSAPrivateCrtKeySpec;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import org.eclipse.milo.opcua.sdk.core.AccessLevel;
import org.eclipse.milo.opcua.sdk.core.Reference;
import org.eclipse.milo.opcua.sdk.server.AddressSpaceFilter;
import org.eclipse.milo.opcua.sdk.server.ManagedAddressSpaceFragmentWithLifecycle;
import org.eclipse.milo.opcua.sdk.server.OpcUaServer;
import org.eclipse.milo.opcua.sdk.server.OpcUaServerConfig;
import org.eclipse.milo.opcua.sdk.server.OpcUaServerConfigLimits;
import org.eclipse.milo.opcua.sdk.server.SimpleAddressSpaceFilter;
import org.eclipse.milo.opcua.sdk.server.EndpointConfig;
import org.eclipse.milo.opcua.sdk.server.identity.AnonymousIdentityValidator;
import org.eclipse.milo.opcua.sdk.server.items.DataItem;
import org.eclipse.milo.opcua.sdk.server.items.MonitoredItem;
import org.eclipse.milo.opcua.sdk.server.nodes.UaVariableNode;
import org.eclipse.milo.opcua.sdk.server.util.SubscriptionModel;
import org.eclipse.milo.opcua.stack.core.NodeIds;
import org.eclipse.milo.opcua.stack.core.channel.EncodingLimits;
import org.eclipse.milo.opcua.stack.core.security.CertificateStore;
import org.eclipse.milo.opcua.stack.core.security.CertificateValidator;
import org.eclipse.milo.opcua.stack.core.security.DefaultApplicationGroup;
import org.eclipse.milo.opcua.stack.core.security.DefaultCertificateManager;
import org.eclipse.milo.opcua.stack.core.security.MemoryCertificateQuarantine;
import org.eclipse.milo.opcua.stack.core.security.MemoryCertificateStore;
import org.eclipse.milo.opcua.stack.core.security.MemoryTrustListManager;
import org.eclipse.milo.opcua.stack.core.security.RsaSha256CertificateFactory;
import org.eclipse.milo.opcua.stack.core.security.SecurityPolicy;
import org.eclipse.milo.opcua.stack.core.transport.TransportProfile;
import org.eclipse.milo.opcua.stack.core.types.builtin.DataValue;
import org.eclipse.milo.opcua.stack.core.types.builtin.LocalizedText;
import org.eclipse.milo.opcua.stack.core.types.builtin.NodeId;
import org.eclipse.milo.opcua.stack.core.types.builtin.QualifiedName;
import org.eclipse.milo.opcua.stack.core.types.builtin.Variant;
import org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.UInteger;
import org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.UShort;
import org.eclipse.milo.opcua.stack.core.types.enumerated.MessageSecurityMode;
import org.eclipse.milo.opcua.stack.core.types.structured.BuildInfo;
import org.eclipse.milo.opcua.stack.core.util.SelfSignedCertificateBuilder;
import org.eclipse.milo.opcua.stack.core.util.SelfSignedCertificateGenerator;
import org.eclipse.milo.opcua.stack.transport.server.tcp.OpcTcpServerTransport;
import org.eclipse.milo.opcua.stack.transport.server.tcp.OpcTcpServerTransportConfig;

/**
 * The Eclipse Milo benchmark server: the shared address space of
 * common/servers/open62541_server.c.
 */
public final class BenchmarkServer {

  static final int FIRST_NODE_ID = 1001;
  static final int NODE_COUNT = 100;
  static final int ARRAY_FIRST_NODE_ID = 2001;
  static final int MAX_ARRAY_SIZES = 16;
  static final String SERVER_READY = "benchmark server ready";
  static final String APPLICATION_URI = "urn:o6:benchmark:server";
  // Room for a 4k frame (8,294,400 Int32 values, 33 MB) plus framing.
  static final int MAX_MESSAGE_SIZE = 64 << 20;

  private BenchmarkServer() {}

  static final class Options {
    int port = 4840;
    String security = "None";
    String certificate = System.getenv("O6_BENCHMARK_CERTIFICATE");
    String privateKey = System.getenv("O6_BENCHMARK_PRIVATE_KEY");
    String trustCertificate;
    List<Integer> arraySizes = new ArrayList<>();
    boolean runtimeInfo;

    static Options parse(String[] args) {
      Options options = new Options();
      for (int i = 0; i < args.length; i++) {
        String flag = args[i];
        if (flag.equals("--runtime-info")) {
          options.runtimeInfo = true;
          continue;
        }
        if (i + 1 >= args.length) {
          throw new IllegalArgumentException(flag + " needs a value");
        }
        String value = args[++i];
        switch (flag) {
          case "--port" -> {
            options.port = Integer.parseInt(value);
            if (options.port <= 0 || options.port > 65535) {
              throw new IllegalArgumentException("invalid --port");
            }
          }
          case "--security" -> {
            if (!value.equals("None") && !value.equals("Basic256Sha256")) {
              throw new IllegalArgumentException("unknown --security value");
            }
            options.security = value;
          }
          case "--certificate" -> options.certificate = value;
          case "--private-key" -> options.privateKey = value;
          case "--trust-certificate" -> options.trustCertificate = value;
          case "--array-sizes" -> {
            for (String token : value.split(",")) {
              token = token.trim();
              if (token.isEmpty()) {
                continue;
              }
              int size = Integer.parseInt(token);
              if (size <= 0) {
                throw new IllegalArgumentException("array sizes must be positive");
              }
              if (!options.arraySizes.contains(size)) {
                options.arraySizes.add(size);
              }
            }
            if (options.arraySizes.size() > MAX_ARRAY_SIZES) {
              throw new IllegalArgumentException("too many array sizes");
            }
          }
          default -> throw new IllegalArgumentException("unknown argument " + flag);
        }
      }
      return options;
    }
  }

  static X509Certificate readCertificate(String path) throws Exception {
    byte[] bytes = Files.readAllBytes(Path.of(path));
    return (X509Certificate)
        CertificateFactory.getInstance("X.509").generateCertificate(new ByteArrayInputStream(bytes));
  }

  /** The shared generator writes PKCS#1 DER; also accept PKCS#8. */
  static PrivateKey readPrivateKey(String path) throws Exception {
    byte[] bytes = Files.readAllBytes(Path.of(path));
    KeyFactory factory = KeyFactory.getInstance("RSA");
    try {
      return factory.generatePrivate(new PKCS8EncodedKeySpec(bytes));
    } catch (Exception notPkcs8) {
      var rsa = org.bouncycastle.asn1.pkcs.RSAPrivateKey.getInstance(bytes);
      return factory.generatePrivate(
          new RSAPrivateCrtKeySpec(
              rsa.getModulus(),
              rsa.getPublicExponent(),
              rsa.getPrivateExponent(),
              rsa.getPrime1(),
              rsa.getPrime2(),
              rsa.getExponent1(),
              rsa.getExponent2(),
              rsa.getCoefficient()));
    }
  }

  static KeyPair identity(Options options, X509Certificate[] chain) throws Exception {
    if (options.security.equals("None")) {
      // Milo binds every endpoint to a certificate, even an unsecured one.
      KeyPair keyPair = SelfSignedCertificateGenerator.generateRsaKeyPair(2048);
      chain[0] =
          new SelfSignedCertificateBuilder(keyPair)
              .setCommonName("o6 benchmark server")
              .setApplicationUri(APPLICATION_URI)
              .addDnsName("localhost")
              .addIpAddress("127.0.0.1")
              .build();
      return keyPair;
    }
    if (options.certificate == null || options.privateKey == null) {
      throw new IllegalArgumentException("encrypted runs require a certificate and a private key");
    }
    chain[0] = readCertificate(options.certificate);
    return new KeyPair(chain[0].getPublicKey(), readPrivateKey(options.privateKey));
  }

  /** Benchmark variables, held in the server namespace so they sit at ns=1. */
  static final class Values extends ManagedAddressSpaceFragmentWithLifecycle {
    private final AddressSpaceFilter filter =
        SimpleAddressSpaceFilter.create(getNodeManager()::containsNode);
    private final SubscriptionModel subscriptions;

    Values(OpcUaServer server, List<Integer> arraySizes) {
      super(server, server.getServerNamespace());
      subscriptions = new SubscriptionModel(server, this);
      getLifecycleManager().addLifecycle(subscriptions);
      getLifecycleManager().addStartupTask(() -> addNodes(arraySizes));
    }

    private void addNodes(List<Integer> arraySizes) {
      UShort ns = getServer().getServerNamespace().getNamespaceIndex();
      if (ns.intValue() != 1) {
        throw new IllegalStateException("benchmark namespace must be 1, got " + ns);
      }
      for (int index = 0; index < NODE_COUNT; index++) {
        add(ns, FIRST_NODE_ID + index, "BenchmarkValue" + index, new Variant(index), null);
      }
      for (int index = 0; index < arraySizes.size(); index++) {
        int size = arraySizes.get(index);
        Integer[] payload = new Integer[size];
        for (int element = 0; element < size; element++) {
          payload[element] = element % 1000;
        }
        add(ns, ARRAY_FIRST_NODE_ID + index, "BenchmarkArray" + size, new Variant(payload), size);
      }
    }

    private void add(UShort ns, int id, String name, Variant value, Integer arraySize) {
      var builder =
          new UaVariableNode.UaVariableNodeBuilder(getNodeContext())
              .setNodeId(new NodeId(ns, uint(id)))
              .setBrowseName(new QualifiedName(ns, name))
              .setDisplayName(LocalizedText.english(name))
              .setDataType(NodeIds.Int32)
              .setTypeDefinition(NodeIds.BaseDataVariableType)
              .setAccessLevel(AccessLevel.READ_WRITE)
              .setUserAccessLevel(AccessLevel.READ_WRITE)
              .setValue(new DataValue(value));
      if (arraySize == null) {
        builder.setValueRank(-1);
      } else {
        builder.setValueRank(1).setArrayDimensions(new UInteger[] {uint(arraySize)});
      }
      UaVariableNode node = builder.build();
      getNodeManager().addNode(node);
      node.addReference(
          new Reference(
              node.getNodeId(), NodeIds.Organizes, NodeIds.ObjectsFolder.expanded(), false));
    }

    @Override
    public AddressSpaceFilter getFilter() {
      return filter;
    }

    @Override
    public void onDataItemsCreated(List<DataItem> dataItems) {
      subscriptions.onDataItemsCreated(dataItems);
    }

    @Override
    public void onDataItemsModified(List<DataItem> dataItems) {
      subscriptions.onDataItemsModified(dataItems);
    }

    @Override
    public void onDataItemsDeleted(List<DataItem> dataItems) {
      subscriptions.onDataItemsDeleted(dataItems);
    }

    @Override
    public void onMonitoringModeChanged(List<MonitoredItem> monitoredItems) {
      subscriptions.onMonitoringModeChanged(monitoredItems);
    }
  }

  /**
   * Milo numbers SecureChannel tokens from 0. The open62541 client keeps its previous token in a
   * slot initialised with id 0, matches the first token against that empty slot and rejects it as
   * expired, so the first connection after every server start fails. Starting the private counter
   * at 1 changes nothing else; Milo exposes no setting for it.
   */
  static void skipTokenIdZero(OpcUaServer server) throws ReflectiveOperationException {
    var field = OpcUaServer.class.getDeclaredField("secureChannelTokenIds");
    field.setAccessible(true);
    ((java.util.concurrent.atomic.AtomicLong) field.get(server)).compareAndSet(0, 1);
  }

  public static void main(String[] args) throws Exception {
    Options options = Options.parse(args);
    if (options.runtimeInfo) {
      System.out.printf(
          "{\"java\":\"%s\",\"vm\":\"%s\",\"sdk\":\"%s\"}%n",
          System.getProperty("java.runtime.version"),
          System.getProperty("java.vm.name"),
          OpcUaServer.SDK_VERSION);
      return;
    }

    X509Certificate[] chain = new X509Certificate[1];
    KeyPair keyPair = identity(options, chain);
    var store = new MemoryCertificateStore();
    store.set(
        NodeIds.RsaSha256ApplicationCertificateType,
        new CertificateStore.Entry(keyPair.getPrivate(), chain));
    var factory =
        new RsaSha256CertificateFactory() {
          @Override
          protected KeyPair createRsaSha256KeyPair() {
            return keyPair;
          }

          @Override
          protected X509Certificate[] createRsaSha256CertificateChain(KeyPair pair) {
            return chain;
          }
        };
    // Every other server in this repository accepts any client certificate.
    var group =
        DefaultApplicationGroup.createAndInitialize(
            new MemoryTrustListManager(),
            store,
            factory,
            new CertificateValidator.InsecureCertificateValidator());
    var certificates = new DefaultCertificateManager(new MemoryCertificateQuarantine(), group);

    boolean secure = options.security.equals("Basic256Sha256");
    Set<EndpointConfig> endpoints = new LinkedHashSet<>();
    endpoints.add(
        EndpointConfig.newBuilder()
            .setBindAddress("127.0.0.1")
            .setBindPort(options.port)
            .setHostname("127.0.0.1")
            .setPath("")
            .setCertificate(chain[0])
            .setTransportProfile(TransportProfile.TCP_UASC_UABINARY)
            .setSecurityPolicy(secure ? SecurityPolicy.Basic256Sha256 : SecurityPolicy.None)
            .setSecurityMode(secure ? MessageSecurityMode.SignAndEncrypt : MessageSecurityMode.None)
            .addTokenPolicies(OpcUaServerConfig.USER_TOKEN_POLICY_ANONYMOUS)
            .build());

    OpcUaServerConfigLimits limits =
        new OpcUaServerConfigLimits() {
          @Override
          public UInteger getMaxSessions() {
            return uint(1024);
          }

          @Override
          public UInteger getMaxArrayLength() {
            return uint(1 << 26);
          }

          @Override
          public UInteger getMaxNodesPerRead() {
            return uint(100_000);
          }

          @Override
          public UInteger getMaxNodesPerWrite() {
            return uint(100_000);
          }
        };

    OpcUaServerConfig config =
        OpcUaServerConfig.builder()
            .setApplicationUri(APPLICATION_URI)
            .setProductUri("urn:o6:benchmark")
            .setApplicationName(LocalizedText.english("o6 benchmark server"))
            .setBuildInfo(
                new BuildInfo(
                    "urn:o6:benchmark",
                    "o6",
                    "o6 benchmark server",
                    OpcUaServer.SDK_VERSION,
                    "",
                    org.eclipse.milo.opcua.stack.core.types.builtin.DateTime.now()))
            .setEndpoints(endpoints)
            .setCertificateManager(certificates)
            .setIdentityValidator(AnonymousIdentityValidator.INSTANCE)
            .setEncodingLimits(new EncodingLimits(65535, 4096, MAX_MESSAGE_SIZE, 128))
            .setLimits(limits)
            .build();

    OpcUaServer server =
        new OpcUaServer(
            config,
            profile -> new OpcTcpServerTransport(OpcTcpServerTransportConfig.newBuilder().build()));
    skipTokenIdZero(server);
    Values values = new Values(server, options.arraySizes);
    values.startup();
    server.startup().get();

    CountDownLatch stopped = new CountDownLatch(1);
    Runtime.getRuntime()
        .addShutdownHook(
            new Thread(
                () -> {
                  try {
                    values.shutdown();
                    server.shutdown().get();
                  } catch (Exception ignored) {
                    // Exiting anyway.
                  } finally {
                    stopped.countDown();
                  }
                }));

    System.out.write(
        ("Milo "
                + SERVER_READY
                + " at opc.tcp://127.0.0.1:"
                + options.port
                + " using #"
                + options.security
                + "\n")
            .getBytes(StandardCharsets.UTF_8));
    System.out.flush();
    stopped.await();
  }
}
