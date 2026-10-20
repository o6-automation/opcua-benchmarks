# Subscription capacity

Measures application counter progress while varying monitored-item count.
open62541, o6-python, node-opcua, .NET, Milo and async-opcua increment UInt64 counters in SDK read
callbacks during the armed workload window. Subscription sampling drives these
callbacks. One open62541 client provides the same setup and measurement behavior
across server SDKs. asyncua, gopcua and S2OPC use periodic application updates
because their subscriptions notify on changes rather than sampling callbacks.

```sh
python -m bench.subscription new bench.db
python -m bench.subscription config bench.db implementation open62541 o6-python asyncua
python -m bench.subscription sample 1 bench.db --name subscriptions-1
python -m bench.subscription show bench.db --name subscriptions-1
```

Defaults include all eight SDKs; build the [optional workers](../README.md#setup)
or narrow the implementation list. Use `config bench.db --help` for all settings.
Rust's `async-opcua` is opt-in; build it with `python -m bench.build --async-opcua`
and select `implementation async-opcua`. See its
[sampler configuration and compatibility patch](../common/SDK_SERVERS.md).

## Measurement

The `client` observer measures the latest counter delivered to the client.
The `server` observer counts read-callback increments (completed application
updates for asyncua, gopcua and S2OPC). These observers run as separate searches.
Callback-driven SDKs advance on reads only; the others use the requested sampling
interval as their application update period and skip missed update slots.
Publishing intervals must be at least as long as sampling intervals.

Each observation includes at least 1,000 expected increments and five publishing
intervals, with another 10% for warmup rounded to whole publishes. Therefore a
large sampling interval still requires a long observation even with few items.

The adaptive search starts at `items_start`, doubles passing counts toward
`items_max`, and refines the passing/failing bracket to 10% or one item. Each
candidate is measured once. Every item must achieve at least 99% of expected
progress to pass; passing the cap establishes only an "at least" bound.
Execution errors leave the search incomplete.

Set `search` to `linear` for a sweep of 1, 2, 4, … items up to `items_max`,
including that maximum. It stops below 50% worst-item progress without bracket
refinement; exactly 50% continues. The capacity pass threshold remains 99%.

Subscriptions use reporting mode, queue size one, and discard-oldest. Intermediate
values may be replaced. asyncua and gopcua report the publishing interval as their
revised sampling interval; S2OPC reports zero (change-triggered sampling). Their
application update period still matches the requested workload. These results
include application-update work, so their counter generation differs from the
callback-driven SDKs.

Reports include approximate confidence intervals for mean progress based on a
circular block bootstrap of publishing intervals. These are informational, not
confidence intervals for maximum capacity or the worst item, and do not trigger
repeat measurements.

Settings, source and worker fingerprints, and per-case targets are stored with
the named run. `--amend` continues compatible saved searches; changing the
workload or rebuilding workers can require a new run. Older subscription data
may remain viewable but is not directly comparable to this counter workload.

The public o6 package uses its built-in subscription limits. If the SDK revises
requested intervals or queue sizes, the case is recorded as a setup failure;
select supported intervals instead. Long o6 observations can also exceed the
evaluation package's runtime limit.

Native client and server event loops use `CLOCK_MONOTONIC`, matching the
measurement coordinator. Reports disclose each observation's counter source;
older application-update results are retained and labeled with their original
workload rather than converted into callback measurements.
