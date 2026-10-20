// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 (c) o6 Automation GmbH (Author: Daniel Opitz)

mod counter;

use base64::Engine;
use opcua::server::{
    address_space::Variable,
    diagnostics::{DiagnosticsNodeManagerBuilder, NamespaceMetadata},
    node_manager::memory::{
        simple_node_manager, CoreNodeManagerBuilder, InMemoryNodeManagerBuilder, SimpleNodeManager,
    },
    ServerBuilder, ServerEndpoint, ANONYMOUS_USER_TOKEN_ID,
};
use opcua::types::NodeId;
use std::{collections::BTreeMap, io::Write, path::PathBuf};

const NAMESPACE: &str = "urn:o6:benchmark:nodes";
const SDK_VERSION: &str = "0.19.0";

fn emit(value: serde_json::Value) {
    println!("{value}");
    std::io::stdout().flush().unwrap();
}

fn builder(port: u16, security: &str, pki: &std::path::Path) -> Result<ServerBuilder, String> {
    let tokens = &[ANONYMOUS_USER_TOKEN_ID.to_owned()];
    let endpoint = match security {
        "None" => ServerEndpoint::new_none("", tokens),
        "Basic256Sha256" => ServerEndpoint::new_basic256sha256_sign_encrypt("", tokens),
        _ => return Err(format!("Unknown security policy {security}")),
    };
    let mut builder = ServerBuilder::new()
        .without_node_managers()
        .with_node_manager(InMemoryNodeManagerBuilder::new(CoreNodeManagerBuilder))
        .application_name("async-opcua benchmark")
        .application_uri("urn:o6:benchmark:server")
        .product_uri("urn:o6:benchmark")
        .host("127.0.0.1")
        .port(port)
        .pki_dir(pki)
        .create_sample_keypair(false)
        .trust_client_certs(true)
        .add_endpoint("benchmark", endpoint)
        .default_endpoint("benchmark")
        .discovery_urls(vec![format!("opc.tcp://127.0.0.1:{port}")])
        .subscription_poll_interval_ms(1);
    let limits = builder.limits_mut();
    limits.max_array_length = 1 << 24;
    limits.max_message_size = 64 << 20;
    limits.max_byte_string_length = 64 << 20;
    limits.max_chunk_count = 2048;
    limits.send_buffer_size = 65536;
    limits.receive_buffer_size = 65536;
    limits.max_sessions = 128;
    limits.operational.max_nodes_per_read = 100_000;
    limits.operational.max_nodes_per_write = 100_000;
    limits.operational.max_monitored_items_per_call = 100_000;
    limits.subscriptions.min_sampling_interval_ms = 1.0;
    limits.subscriptions.min_publishing_interval_ms = 1.0;
    limits.subscriptions.max_monitored_items_per_sub = 1_048_576;
    limits.subscriptions.max_monitored_item_queue_size = 1;
    limits.subscriptions.max_notifications_per_publish = u32::MAX as u64;
    limits.subscriptions.max_queued_notifications = 0;
    Ok(builder)
}

fn namespace() -> NamespaceMetadata {
    NamespaceMetadata {
        namespace_uri: NAMESPACE.to_owned(),
        ..Default::default()
    }
}

fn parse_sizes(text: &str) -> Result<Vec<usize>, String> {
    let mut sizes = Vec::new();
    for token in text.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let size = token.parse::<usize>().map_err(|e| e.to_string())?;
        if size == 0 || size > 1 << 24 {
            return Err(format!("Invalid array size {size}"));
        }
        if !sizes.contains(&size) {
            sizes.push(size);
        }
    }
    if sizes.len() > 16 {
        return Err("Too many array sizes".into());
    }
    Ok(sizes)
}

async fn throughput(args: &[String]) -> Result<(), String> {
    let mut options = BTreeMap::new();
    for pair in args.chunks(2) {
        if pair.len() != 2
            || ![
                "--port",
                "--security",
                "--certificate",
                "--private-key",
                "--trust-certificate",
                "--array-sizes",
            ]
            .contains(&pair[0].as_str())
        {
            return Err(format!("Invalid server arguments: {pair:?}"));
        }
        if options.insert(pair[0].as_str(), pair[1].as_str()).is_some() {
            return Err(format!("Duplicate option {}", pair[0]));
        }
    }
    let port: u16 = options
        .get("--port")
        .unwrap_or(&"4840")
        .parse::<u16>()
        .map_err(|e| e.to_string())?;
    if port == 0 {
        return Err("Invalid port".into());
    }
    let security = *options.get("--security").unwrap_or(&"None");
    let sizes = parse_sizes(options.get("--array-sizes").unwrap_or(&""))?;
    let pki = tempfile::tempdir().map_err(|e| e.to_string())?;
    let mut builder = builder(port, security, pki.path())?;
    if security != "None" {
        let certificate = options
            .get("--certificate")
            .ok_or("Missing --certificate")?;
        let key = options
            .get("--private-key")
            .ok_or("Missing --private-key")?;
        let der = std::fs::read(key).map_err(|e| e.to_string())?;
        // The harness supplies PKCS#1 DER; async-opcua loads PEM private keys.
        let encoded = base64::engine::general_purpose::STANDARD.encode(der);
        let body = encoded
            .as_bytes()
            .chunks(64)
            .map(|line| std::str::from_utf8(line).unwrap())
            .collect::<Vec<_>>()
            .join("\n");
        let pem =
            format!("-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----\n");
        let pem_path = pki.path().join("server-key.pem");
        {
            use std::os::unix::fs::OpenOptionsExt;
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&pem_path)
                .map_err(|e| e.to_string())?;
            file.write_all(pem.as_bytes()).map_err(|e| e.to_string())?;
        }
        // The SDK otherwise logs missing keys but still starts its listener.
        opcua::crypto::CertificateStore::read_pkey(&pem_path)?;
        opcua::crypto::CertificateStore::read_cert(std::path::Path::new(certificate))?;
        builder = builder
            .certificate_path(PathBuf::from(certificate))
            .private_key_path(pem_path);
    }
    // Register the workload before diagnostics so its public node IDs stay in ns=1.
    let (server, handle) = builder
        .with_node_manager(simple_node_manager(namespace(), "benchmark"))
        .with_node_manager(DiagnosticsNodeManagerBuilder)
        .build()
        .map_err(|e| format!("{e:?}"))?;
    let ns = handle
        .get_namespace_index(NAMESPACE)
        .ok_or("Missing benchmark namespace")?;
    if ns != 1 {
        return Err("Benchmark namespace must be 1".into());
    }
    let manager = handle
        .node_managers()
        .get_of_type::<SimpleNodeManager>()
        .unwrap();
    {
        let mut address = manager.address_space().write();
        let mut nodes = Vec::new();
        for id in 1001..1101u32 {
            let mut variable = Variable::new(
                &NodeId::new(ns, id),
                format!("Scalar{id}"),
                "Scalar",
                (id - 1001) as i32,
            );
            variable.set_writable(true);
            variable.set_user_access_level(variable.access_level());
            nodes.push(variable);
        }
        for (i, size) in sizes.into_iter().enumerate() {
            let mut variable = Variable::new(
                &NodeId::new(ns, 2001 + i as u32),
                format!("Array{size}"),
                "Array",
                (0..size).map(|j| (j % 1000) as i32).collect::<Vec<_>>(),
            );
            variable.set_writable(true);
            variable.set_user_access_level(variable.access_level());
            nodes.push(variable);
        }
        if address
            .add_variables(nodes, &NodeId::objects_folder_id())
            .contains(&false)
        {
            return Err("Could not add benchmark variables".into());
        }
    }
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
        .await
        .map_err(|e| e.to_string())?;
    println!("benchmark server ready");
    std::io::stdout().flush().map_err(|e| e.to_string())?;
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .map_err(|e| e.to_string())?;
    tokio::select! {
        result = server.run_with(listener) => result,
        _ = tokio::signal::ctrl_c() => { handle.cancel(); Ok(()) },
        _ = terminate.recv() => { handle.cancel(); Ok(()) },
    }
}

fn main() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--runtime-info"] {
        emit(
            serde_json::json!({"sdk": SDK_VERSION, "rustc": env!("BENCH_RUSTC"),
            "session_nonce_patch": true}),
        );
        return Ok(());
    }
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(std::thread::available_parallelism().map_or(1, usize::from))
        .enable_all()
        .build()
        .map_err(|e| e.to_string())?;
    runtime.block_on(async {
        if args.first().map(String::as_str) == Some("--counter") {
            counter::run(&args[1..]).await
        } else {
            throughput(&args).await
        }
    })
}
