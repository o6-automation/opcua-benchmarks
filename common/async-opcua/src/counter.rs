// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 (c) o6 Automation GmbH (Author: Daniel Opitz)

use async_trait::async_trait;
use opcua::core::sync::RwLock;
use opcua::server::{
    address_space::{AddressSpace, Variable},
    diagnostics::{DiagnosticsNodeManagerBuilder, NamespaceMetadata},
    node_manager::{
        memory::{
            InMemoryNodeManagerBuilder, InMemoryNodeManagerImpl, InMemoryNodeManagerImplBuilder,
            SimpleNodeManagerBuilder, SimpleNodeManagerImpl,
        },
        MonitoredItemRef, MonitoredItemUpdateRef, ParsedReadValueId, RequestContext, ServerContext,
        SyncSampler,
    },
    CreateMonitoredItem,
};
use opcua::types::{
    AttributeId, DataValue, Identifier, MonitoringMode, NodeId, StatusCode, TimestampsToReturn,
};
use serde_json::json;
use std::{
    io::BufRead,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, OnceLock,
    },
    time::Duration,
};

fn now_ns() -> u64 {
    // The coordinator uses absolute Linux CLOCK_MONOTONIC timestamps.
    let mut clock = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    assert_eq!(
        unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut clock) },
        0
    );
    clock.tv_sec as u64 * 1_000_000_000 + clock.tv_nsec as u64
}

struct Window {
    start: u64,
    end: u64,
    measured_start: u64,
    measured_end: u64,
    interval: u64,
    intervals: Option<Vec<AtomicU64>>,
}

impl Window {
    fn parse(line: &str, now: u64, measure_server: bool) -> Result<Self, String> {
        let parts: Vec<_> = line.split_whitespace().collect();
        if parts.len() != 6 || parts[0] != "arm" {
            return Err("Expected arm and five monotonic timestamps".into());
        }
        let numbers = parts[1..]
            .iter()
            .map(|s| s.parse::<u64>())
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| e.to_string())?;
        let (start, end, measured_start, measured_end, interval) =
            (numbers[0], numbers[1], numbers[2], numbers[3], numbers[4]);
        if start <= now
            || measured_start < start
            || measured_end <= measured_start
            || end < measured_end
            || interval == 0
            || (measured_end - measured_start) % interval != 0
            || (measured_end - measured_start) / interval > 1000
        {
            return Err("Invalid measurement window".into());
        }
        Ok(Self {
            start,
            end,
            measured_start,
            measured_end,
            interval,
            intervals: measure_server
                .then(|| zeros(((measured_end - measured_start) / interval) as usize)),
        })
    }

    fn acknowledgement(&self) -> serde_json::Value {
        json!({"event": "armed", "start_ns": self.start, "end_ns": self.end,
            "measured_start_ns": self.measured_start, "measured_end_ns": self.measured_end,
            "interval_ns": self.interval})
    }
}

fn zeros(count: usize) -> Vec<AtomicU64> {
    (0..count).map(|_| AtomicU64::new(0)).collect()
}

fn snapshot(values: &[AtomicU64]) -> Vec<u64> {
    values.iter().map(|v| v.load(Ordering::Relaxed)).collect()
}

struct Counters {
    values: Vec<AtomicU64>,
    measured: Option<Vec<AtomicU64>>,
    window: OnceLock<Window>,
}

impl Counters {
    fn read(&self, index: usize) -> DataValue {
        let now = now_ns();
        let mut value = self.values[index].load(Ordering::Relaxed);
        if let Some(window) = self.window.get() {
            if window.start <= now && now < window.end {
                value = self.values[index].fetch_add(1, Ordering::Relaxed) + 1;
                if window.measured_start <= now && now < window.measured_end {
                    if let (Some(measured), Some(intervals)) = (&self.measured, &window.intervals) {
                        measured[index].fetch_add(1, Ordering::Relaxed);
                        intervals[((now - window.measured_start) / window.interval) as usize]
                            .fetch_add(1, Ordering::Relaxed);
                    }
                }
            }
        }
        DataValue::new_now(value)
    }

    fn index(&self, node: &NodeId) -> Option<usize> {
        match node.identifier {
            Identifier::Numeric(id)
                if node.namespace == 1 && id >= 1 && id as usize <= self.values.len() =>
            {
                Some(id as usize - 1)
            }
            _ => None,
        }
    }
}

struct CounterBuilder(Arc<Counters>);

impl InMemoryNodeManagerImplBuilder for CounterBuilder {
    type Impl = CounterManager;

    fn build(self, context: ServerContext, address: &mut AddressSpace) -> CounterManager {
        let base = SimpleNodeManagerBuilder::new(super::namespace(), "benchmark-counters")
            .build(context, address);
        for i in 0..self.0.values.len() {
            let id = NodeId::new(1, i as u32 + 1);
            let mut variable = Variable::new(&id, format!("Counter{i}"), "Counter", 0u64);
            variable.set_minimum_sampling_interval(1.0);
            assert!(address.add_variables(vec![variable], &NodeId::objects_folder_id())[0]);
            let state = self.0.clone();
            base.add_read_callback(id, move |_, _, _| Ok(state.read(i)));
        }
        CounterManager {
            base,
            state: self.0,
            sampler: SyncSampler::new(),
        }
    }
}

struct CounterManager {
    base: SimpleNodeManagerImpl,
    state: Arc<Counters>,
    sampler: SyncSampler,
}

#[async_trait]
impl InMemoryNodeManagerImpl for CounterManager {
    async fn init(&self, _address: &mut AddressSpace, context: ServerContext) {
        // SimpleNodeManager's built-in sampler polls at >=100 ms. The SDK's
        // public sampler accepts the 1 ms floor needed by this suite.
        self.sampler
            .run(Duration::from_millis(1), context.subscriptions.clone());
    }

    fn name(&self) -> &str {
        self.base.name()
    }
    fn namespaces(&self) -> Vec<NamespaceMetadata> {
        self.base.namespaces()
    }

    async fn read_values(
        &self,
        context: &RequestContext,
        address: &RwLock<AddressSpace>,
        nodes: &[&ParsedReadValueId],
        max_age: f64,
        timestamps: TimestampsToReturn,
    ) -> Vec<DataValue> {
        self.base
            .read_values(context, address, nodes, max_age, timestamps)
            .await
    }

    async fn create_value_monitored_items(
        &self,
        context: &RequestContext,
        address: &RwLock<AddressSpace>,
        items: &mut [&mut &mut CreateMonitoredItem],
    ) {
        for item in items {
            let id = item.item_to_monitor().node_id.clone();
            let Some(index) = self.state.index(&id) else {
                item.set_status(StatusCode::BadNodeIdUnknown);
                continue;
            };
            let values = self
                .base
                .read_values(
                    context,
                    address,
                    &[item.item_to_monitor()],
                    0.0,
                    item.timestamps_to_return(),
                )
                .await;
            let value = values.into_iter().next().unwrap();
            item.set_status(value.status());
            if value.status().is_bad() {
                continue;
            }
            item.set_initial_value(value);
            let state = self.state.clone();
            self.sampler.add_sampler(
                id,
                AttributeId::Value,
                move || Some(state.read(index)),
                item.monitoring_mode(),
                item.handle(),
                Duration::from_secs_f64(item.sampling_interval() / 1000.0),
            );
        }
    }

    async fn modify_monitored_items(
        &self,
        _context: &RequestContext,
        items: &[&MonitoredItemUpdateRef],
    ) {
        for item in items {
            self.sampler.update_sampler(
                item.node_id(),
                item.attribute(),
                item.handle(),
                Duration::from_secs_f64(item.update().revised_sampling_interval / 1000.0),
            );
        }
    }

    async fn set_monitoring_mode(
        &self,
        _context: &RequestContext,
        mode: MonitoringMode,
        items: &[&MonitoredItemRef],
    ) {
        for item in items {
            self.sampler
                .set_sampler_mode(item.node_id(), item.attribute(), item.handle(), mode);
        }
    }

    async fn delete_monitored_items(&self, _context: &RequestContext, items: &[&MonitoredItemRef]) {
        for item in items {
            self.sampler
                .remove_sampler(item.node_id(), item.attribute(), item.handle());
        }
    }
}

pub async fn run(args: &[String]) -> Result<(), String> {
    if args.len() != 4 {
        return Err("Expected port items sampling_ms measurement".into());
    }
    let port = args[0].parse::<u16>().map_err(|e| e.to_string())?;
    let count = args[1].parse::<usize>().map_err(|e| e.to_string())?;
    let sampling = args[2].parse::<u64>().map_err(|e| e.to_string())?;
    if port == 0
        || !(1..=1_048_576).contains(&count)
        || ![1, 2, 5, 10, 20, 50, 100].contains(&sampling)
        || !["client", "server"].contains(&args[3].as_str())
    {
        return Err("Invalid counter settings".into());
    }
    let measure_server = args[3] == "server";
    let state = Arc::new(Counters {
        values: zeros(count),
        measured: measure_server.then(|| zeros(count)),
        window: OnceLock::new(),
    });
    let pki = tempfile::tempdir().map_err(|e| e.to_string())?;
    let (server, handle) = super::builder(port, "None", pki.path())?
        .with_node_manager(InMemoryNodeManagerBuilder::new(CounterBuilder(
            state.clone(),
        )))
        .with_node_manager(DiagnosticsNodeManagerBuilder)
        .build()
        .map_err(|e| format!("{e:?}"))?;
    if handle.get_namespace_index(super::NAMESPACE) != Some(1) {
        return Err("Counter namespace must be 1".into());
    }
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
        .await
        .map_err(|e| e.to_string())?;
    let mut server_task = tokio::spawn(server.run_with(listener));
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
    std::thread::spawn(move || {
        for line in std::io::stdin().lock().lines() {
            if sender.send(line).is_err() {
                break;
            }
        }
    });
    super::emit(
        json!({"event": "ready", "sdk": "async-opcua", "version": super::SDK_VERSION,
        "rustc": env!("BENCH_RUSTC"), "clock": "CLOCK_MONOTONIC", "sdk_clock": "CLOCK_MONOTONIC",
        "counter_source": "read_callback", "sampler_poll_ms": 1, "session_nonce_patch": true}),
    );
    let result = async {
        let line = receiver
            .recv()
            .await
            .ok_or("Control channel closed")?
            .map_err(|e| e.to_string())?;
        let window = Window::parse(&line, now_ns(), measure_server)?;
        let acknowledgement = window.acknowledgement();
        let deadline = window.end;
        state.window.set(window).map_err(|_| "Already armed")?;
        super::emit(acknowledgement);
        tokio::time::sleep(Duration::from_nanos(deadline.saturating_sub(now_ns()))).await;
        if let Some(measured) = &state.measured {
            super::emit(json!({"event": "result", "counts": snapshot(measured),
                "interval_counts": snapshot(state.window.get().unwrap().intervals.as_ref().unwrap())}));
        } else {
            super::emit(json!({"event": "done"}));
        }
        while let Some(line) = receiver.recv().await {
            if line.map_err(|e| e.to_string())?.trim() == "quit" {
                break;
            }
            return Err("Expected quit".to_owned());
        }
        Ok(())
    };
    let result = tokio::select! {
        result = result => result,
        result = &mut server_task => return result.map_err(|e| e.to_string())?,
    };
    handle.cancel();
    server_task.await.map_err(|e| e.to_string())??;
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arm_rejects_invalid_boundaries() {
        for line in [
            "arm 10 30 15 30 0",
            "arm 10 30 15 40 5",
            "arm 10 30 15 30 7",
            "arm 5 30 15 30 5",
            "arm 10 30 5 30 5",
            "arm 10 30 30 30 5",
        ] {
            assert!(Window::parse(line, 5, true).is_err(), "{line}");
        }
        let window = Window::parse("arm 10 30 15 30 5", 5, true).unwrap();
        assert_eq!(window.intervals.unwrap().len(), 3);
        assert!(Window::parse("arm 10 30 15 30 5", 5, false)
            .unwrap()
            .intervals
            .is_none());
    }

    #[test]
    fn callback_records_only_the_selected_window() {
        let state = Counters {
            values: zeros(1),
            measured: Some(zeros(1)),
            window: OnceLock::new(),
        };
        state.read(0);
        assert_eq!(snapshot(&state.values), [0]);
        let now = now_ns();
        state
            .window
            .set(Window {
                start: now - 1,
                end: now + 1_000_000_000,
                measured_start: now - 1,
                measured_end: now + 1_000_000_000,
                interval: 2_000_000_000,
                intervals: Some(zeros(1)),
            })
            .ok()
            .unwrap();
        state.read(0);
        assert_eq!(snapshot(&state.values), [1]);
        assert_eq!(snapshot(state.measured.as_ref().unwrap()), [1]);
        assert_eq!(
            snapshot(state.window.get().unwrap().intervals.as_ref().unwrap()),
            [1]
        );
    }
}
