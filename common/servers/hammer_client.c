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

/* The "hammer" client: the one fixed load generator every suite that drives
 * a server with a C client points at. Unlike throughput's client, which
 * aborts on the first unexpected status because a throughput run is only
 * meaningful if every call actually succeeded, this client's entire job is
 * to keep firing through errors, timeouts, and a dying connection and
 * report what happened — a service that refuses a call, a request that
 * times out, and a channel that closes mid-run are exactly the outcomes it
 * exists to measure, not failures of the client.
 *
 * Two load shapes, selected by the runner:
 *
 *   Closed loop (default): fire as fast as outstanding capacity allows,
 *   waiting on the previous request to return before issuing the next. The
 *   existing server-limits behaviour, byte-for-byte.
 *
 *   Open loop (--open-loop): fire on a fixed schedule derived from
 *   --target-rate. Each request is timestamped with its *intended* send
 *   time (the scheduled time), not when the generator actually pushed it
 *   onto the wire, so queueing delay inside the generator is charged to
 *   the measurement rather than erased by it. This is the coordinated-
 *   omission fix the server_limits suite needs — a server that
 *   stalls now lands in the tail instead of vanishing into a generator
 *   that politely slowed down to match.
 *
 * The histogram helpers and clock live in common/histogram.h and
 * common/contract.h; the option struct and parser live in
 * common/hammer_client.h. */

#include "hammer_client.h"

#include <open62541/client.h>
#include <open62541/client_config_default.h>
#include <open62541/client_highlevel_async.h>

typedef struct ClientContext {
    UA_Client *client;
    uint32_t random_state;
    size_t max_outstanding;
    size_t issued;
    size_t retired;    /* completed, successfully or not, this phase */
    UA_Boolean counting;         /* false during warmup: retired counts, nothing else does */
    UA_Boolean connection_broken; /* run_iterate itself failed: the channel is gone */
    /* A send the client declined for a reason of its own rather than the
     * server's — its outstanding-request cap, most likely. That is a limit of
     * this load generator, not a measurement of the server, so it is reported
     * on its own and never folded into the error counts below. */
    UA_StatusCode send_refused;
    uint64_t succeeded;
    uint64_t timeouts;
    uint64_t connection_lost;
    uint64_t other_errors;
    uint64_t histogram[O6_HISTOGRAM_BUCKETS];
    /* The service the binary is driving. Scalar Read is the original path
     * (and the default) and uses the high-level readValueAttribute_async
     * helper; Write and batched Read build a request manually and post it
     * through the lower-level send-async API. The field is set from
     * O6_LimitsOptions.service before the first issue. */
    const char *service;
    /* Node range for the LCG walker. The walker picks an index in
     * [0, node_count) and returns first_node_id + index — the same
     * convention the rest of the rig uses, so a runner that does not
     * pass --first-node-id/--nodes hits the same nodes the server has. */
    uint32_t first_node_id;
    size_t node_count;
    /* Scratch for the batched Read path. Allocated lazily on the first
     * batched issue so a scalar-only run never pays the cost. The
     * ReadValueId struct is the same shape the open62541 send-async API
     * consumes, with the index range filled in by the walker. */
    size_t batch_size;
    UA_ReadValueId *batch_read_ids;
    /* Mirrors the batched-read case: one WriteValue per node in the
     * batch, with the value payload (an Int32 equal to the node id) set
     * up once and reused on every issue. */
    UA_WriteValue *batch_write_values;
    /* Write only: write a String "value-<node id>" instead of an Int32
     * (--write-type string), for servers whose nodes hold strings. */
    int write_string;
} ClientContext;

/* The value a Write sets on a node: an Int32 equal to the node's numeric
 * identifier, or with --write-type string the String "value-<id>". The
 * variant owns a copy of its value (UA_Variant_setScalarCopy). */
static UA_StatusCode
set_write_value(const ClientContext *context, UA_Variant *variant, uint32_t node_id) {
    if(context->write_string) {
        char text[32];
        UA_String string;
        snprintf(text, sizeof(text), "value-%u", (unsigned)node_id);
        string = UA_STRING(text);
        return UA_Variant_setScalarCopy(variant, &string, &UA_TYPES[UA_TYPES_STRING]);
    }
    {
        UA_Int32 value = (UA_Int32)node_id;
        return UA_Variant_setScalarCopy(variant, &value, &UA_TYPES[UA_TYPES_INT32]);
    }
}

typedef struct PendingRead {
    ClientContext *context;
    uint64_t issue_ns;  /* intended send time for open-loop; actual send for closed-loop */
} PendingRead;

static UA_NodeId
next_node_id(ClientContext *context) {
    /* ``o6_next_node_index`` already takes its output modulo O6_NODE_COUNT
     * (the size of the server's scalar layout), so the index lands in
     * [0, 100) by default. A second modulo against the configured
     * ``node_count`` keeps the walk bounded to whatever the runner asked
     * for — a runner that points at a smaller or larger range still hits
     * only those node ids, and a runner that leaves it at the default
     * gets the same walk every other runner does. */
    const uint32_t index = o6_next_node_index(&context->random_state) %
        (uint32_t)context->node_count;
    return UA_NODEID_NUMERIC(1, context->first_node_id + index);
}

/* Requests that are still outstanding when a phase ends (deadline passed, or
 * the connection broke) are neither a success nor a service-reported error —
 * nothing ever answered them. Counted as connection_lost, because that is
 * what not answering a request within the grace period means for the purpose
 * of this benchmark. */
static UA_Boolean
is_connection_status(UA_StatusCode status) {
    return status == UA_STATUSCODE_BADCONNECTIONCLOSED ||
           status == UA_STATUSCODE_BADSECURECHANNELCLOSED ||
           status == UA_STATUSCODE_BADSERVERNOTCONNECTED ||
           status == UA_STATUSCODE_BADNOTCONNECTED ||
           status == UA_STATUSCODE_BADCOMMUNICATIONERROR ||
           status == UA_STATUSCODE_BADCONNECTIONREJECTED;
}

static UA_Boolean
is_timeout_status(UA_StatusCode status) {
    return status == UA_STATUSCODE_BADTIMEOUT ||
           status == UA_STATUSCODE_BADREQUESTTIMEOUT;
}

static void
account(ClientContext *context, UA_StatusCode status, uint64_t issue_ns) {
    if(!context->counting)
        return;
    if(status == UA_STATUSCODE_GOOD) {
        uint64_t latency_us = (o6_limits_now_ns() - issue_ns) / 1000;
        context->succeeded++;
        context->histogram[o6_histogram_bucket_for(latency_us)]++;
    } else if(is_timeout_status(status)) {
        context->timeouts++;
    } else if(is_connection_status(status)) {
        context->connection_lost++;
    } else {
        context->other_errors++;
    }
}

static void
on_hammer_read(UA_Client *client, void *userdata, UA_UInt32 request_id,
              UA_StatusCode status, UA_DataValue *value) {
    PendingRead *pending = (PendingRead*)userdata;
    ClientContext *context = pending->context;
    (void)client;
    (void)request_id;
    if(status == UA_STATUSCODE_GOOD &&
       (!value || !value->hasValue ||
        !UA_Variant_hasScalarType(&value->value, &UA_TYPES[UA_TYPES_INT32])))
        status = UA_STATUSCODE_BADTYPEMISMATCH;
    account(context, status, pending->issue_ns);
    context->retired++;
    free(pending);
}

/* Write callback. The high-level ``UA_Client_writeValueAttribute_async``
 * posts a UA_WriteResponse to its ``UA_ClientAsyncWriteCallback`` —
 * the same callback the lower-level ``UA_Client_sendAsyncWriteRequest``
 * uses — so the success check is the service header's statusCode plus
 * the per-node ``results[0]`` when the response carries one. A
 * BadTypeMismatch or BadWriteNotSupported on a real wire (a server that
 * refused the value, say) lands in the same other_errors bucket the
 * rest of the generator uses. */
static void
on_hammer_write(UA_Client *client, void *userdata, UA_UInt32 request_id,
                UA_WriteResponse *response) {
    PendingRead *pending = (PendingRead*)userdata;
    ClientContext *context = pending->context;
    UA_StatusCode status;
    (void)client;
    (void)request_id;
    if(!response) {
        account(context, UA_STATUSCODE_BADUNEXPECTEDERROR, pending->issue_ns);
        context->retired++;
        free(pending);
        return;
    }
    status = response->responseHeader.serviceResult;
    if(status == UA_STATUSCODE_GOOD &&
       response->resultsSize == 0)
        status = UA_STATUSCODE_BADUNEXPECTEDERROR;
    for(size_t i = 0; status == UA_STATUSCODE_GOOD && i < response->resultsSize; ++i)
        if(response->results[i] != UA_STATUSCODE_GOOD)
            status = UA_STATUSCODE_BADUNEXPECTEDERROR;
    account(context, status, pending->issue_ns);
    context->retired++;
    free(pending);
}

/* Batched Read callback. The SDK's send-async API hands the full
 * ReadResponse to the callback, so the success check is against the
 * service-level header and the first result's type — mirroring what
 * on_hammer_read does for a scalar Read, but on a ReadResponse rather
 * than a DataValue. */
static void
on_hammer_batch_read(UA_Client *client, void *userdata, UA_UInt32 request_id,
                     UA_ReadResponse *response) {
    PendingRead *pending = (PendingRead*)userdata;
    ClientContext *context = pending->context;
    UA_StatusCode status;
    (void)client;
    (void)request_id;
    if(!response) {
        account(context, UA_STATUSCODE_BADUNEXPECTEDERROR, pending->issue_ns);
        context->retired++;
        free(pending);
        return;
    }
    status = response->responseHeader.serviceResult;
    if(status == UA_STATUSCODE_GOOD &&
       (response->resultsSize == 0 || !response->results[0].hasValue ||
        !UA_Variant_hasScalarType(&response->results[0].value,
                                  &UA_TYPES[UA_TYPES_INT32])))
        status = UA_STATUSCODE_BADTYPEMISMATCH;
    account(context, status, pending->issue_ns);
    context->retired++;
    free(pending);
}

/* Lazily allocate the batched Read scratch. One UA_ReadValueId per node
 * in the batch, all sharing the same nodeId namespace (1) and the same
 * attribute (Value). The per-issue fill below refreshes only the NodeId
 * and the timestamp headers, so the allocation cost is paid once and the
 * per-call work stays a walk over the LCG. */
static UA_StatusCode
ensure_batch_read_scratch(ClientContext *context) {
    if(context->batch_read_ids != NULL)
        return UA_STATUSCODE_GOOD;
    context->batch_read_ids = (UA_ReadValueId *)calloc(
        context->batch_size, sizeof(UA_ReadValueId));
    if(!context->batch_read_ids)
        return UA_STATUSCODE_BADOUTOFMEMORY;
    for(size_t index = 0; index < context->batch_size; ++index) {
        UA_ReadValueId_init(&context->batch_read_ids[index]);
        context->batch_read_ids[index].attributeId = UA_ATTRIBUTEID_VALUE;
        context->batch_read_ids[index].indexRange = UA_STRING_NULL;
    }
    return UA_STATUSCODE_GOOD;
}

/* Issue one request through the SDK's async API, with the same outcome
 * classification as run_phase / run_open_loop does for a completed one. */
static void
issue_one(ClientContext *context, uint64_t issue_ns) {
    PendingRead *pending = (PendingRead*)malloc(sizeof(PendingRead));
    UA_NodeId node_id;
    UA_StatusCode sent = UA_STATUSCODE_GOOD;
    if(!pending) {
        context->connection_broken = true;
        return;
    }
    pending->context = context;
    pending->issue_ns = issue_ns;

    if(strcmp(context->service, O6_LIMITS_SERVICE_WRITE) == 0 &&
       context->batch_size > 1) {
        UA_WriteRequest request;
        /* Batched write: batch_size WriteValues on consecutive nodes
         * first_node_id.., the value set_write_value gives, built once. */
        if(!context->batch_write_values) {
            context->batch_write_values = (UA_WriteValue *)calloc(
                context->batch_size, sizeof(UA_WriteValue));
            if(!context->batch_write_values) {
                free(pending);
                context->connection_broken = true;
                return;
            }
            for(size_t i = 0; i < context->batch_size; ++i) {
                UA_WriteValue *wv = &context->batch_write_values[i];
                UA_WriteValue_init(wv);
                wv->nodeId = UA_NODEID_NUMERIC(
                    1, context->first_node_id + (uint32_t)(i % context->node_count));
                wv->attributeId = UA_ATTRIBUTEID_VALUE;
                wv->value.hasValue = true;
                if(set_write_value(context, &wv->value.value,
                                   wv->nodeId.identifier.numeric) != UA_STATUSCODE_GOOD) {
                    free(pending);
                    context->connection_broken = true;
                    return;
                }
            }
        }
        UA_WriteRequest_init(&request);
        request.nodesToWrite = context->batch_write_values;
        request.nodesToWriteSize = context->batch_size;
        sent = UA_Client_sendAsyncWriteRequest(
            context->client, &request, on_hammer_write, pending, NULL);
    } else if(strcmp(context->service, O6_LIMITS_SERVICE_WRITE) == 0) {
        /* Scalar write: one Int32 equal to the node's numeric identifier
         * (the same convention the throughput C client and the
         * Python asyncua/o6 clients use, so a Write run lands on the same
         * values the rest of the rig produces), or a String with
         * --write-type string. */
        UA_Variant variant;
        node_id = next_node_id(context);
        UA_Variant_init(&variant);
        sent = set_write_value(context, &variant, node_id.identifier.numeric);
        if(sent == UA_STATUSCODE_GOOD)
            sent = UA_Client_writeValueAttribute_async(
                context->client, node_id, &variant, on_hammer_write, pending, NULL);
        UA_Variant_clear(&variant);
    } else if(strcmp(context->service, O6_LIMITS_SERVICE_READ_BATCH) == 0 ||
              strcmp(context->service, O6_LIMITS_SERVICE_READ_BATCH_100) == 0) {
        /* Batched Read: fill the scratch, post a UA_ReadRequest through
         * the lower-level send-async API. The result is a UA_ReadResponse
         * (rather than the per-node DataValue the high-level API gives),
         * which is what on_hammer_batch_read expects. */
        UA_ReadRequest request;
        sent = ensure_batch_read_scratch(context);
        if(sent != UA_STATUSCODE_GOOD) {
            free(pending);
            context->connection_broken = true;
            return;
        }
        for(size_t index = 0; index < context->batch_size; ++index) {
            context->batch_read_ids[index].nodeId = next_node_id(context);
        }
        UA_ReadRequest_init(&request);
        request.nodesToRead = context->batch_read_ids;
        request.nodesToReadSize = context->batch_size;
        sent = UA_Client_sendAsyncReadRequest(
            context->client, &request, on_hammer_batch_read, pending, NULL);
    } else {
        /* Default and the historical path: scalar Read through the
         * high-level helper. One malloc'd PendingRead per issue — the
         * same shape on_hammer_read was written against. */
        node_id = next_node_id(context);
        sent = UA_Client_readValueAttribute_async(
            context->client, node_id, on_hammer_read, pending, NULL);
    }

    if(sent != UA_STATUSCODE_GOOD) {
        free(pending);
        if(is_connection_status(sent)) {
            context->issued++;
            context->retired++;
            account(context, sent, issue_ns);
            context->connection_broken = true;
        } else if(context->send_refused == UA_STATUSCODE_GOOD) {
            context->send_refused = sent;
        }
        return;
    }
    context->issued++;
}

/* The closed-loop phase: fire requests for up to ``deadline_ns``, keeping
 * ``max_outstanding`` in flight, then drain whatever is still outstanding
 * for up to ``grace_ns`` more before giving up on it. Used for both the
 * untimed warm-up (which establishes the session and lets the pipe fill
 * before anything is counted) and the timed window, distinguished only by
 * ``context->counting`` — so a connection that is already struggling shows
 * it in warm-up the same way it would in the measured window. */
static void
run_closed_loop(ClientContext *context, uint64_t deadline_ns, uint64_t grace_ns) {
    context->issued = 0;
    context->retired = 0;
    while(!context->connection_broken &&
          (o6_limits_now_ns() < deadline_ns || context->retired < context->issued)) {
        while(!context->connection_broken &&
              o6_limits_now_ns() < deadline_ns &&
              context->issued - context->retired < context->max_outstanding) {
            uint64_t issue_ns = o6_limits_now_ns();
            issue_one(context, issue_ns);
            if(context->connection_broken || context->send_refused != UA_STATUSCODE_GOOD)
                break;
        }
        if(context->connection_broken)
            break;
        if(context->retired < context->issued &&
           UA_Client_run_iterate(context->client, O6_LIMITS_EVENT_WAIT_MS) !=
               UA_STATUSCODE_GOOD) {
            context->connection_broken = true;
            break;
        }
    }
    if(context->retired < context->issued) {
        uint64_t drain_deadline = o6_limits_now_ns() + grace_ns;
        while(context->retired < context->issued &&
              o6_limits_now_ns() < drain_deadline) {
            if(UA_Client_run_iterate(context->client, O6_LIMITS_EVENT_WAIT_MS) !=
               UA_STATUSCODE_GOOD)
                break;
        }
    }
    /* Anything still outstanding was abandoned: the server (or the network
     * between here and it) never answered within the grace period. */
    while(context->retired < context->issued) {
        account(context, UA_STATUSCODE_BADCONNECTIONCLOSED, 0);
        context->retired++;
    }
}

/* The open-loop phase: at each iteration, the next request is due at
 * ``next_issue_ns``; send it (or skip it as abandoned if its time is past
 * the deadline and the connection is gone) and advance the schedule by one
 * period. Each pending record carries its intended send time as
 * ``issue_ns`` so the latency read off it accounts for the time spent
 * waiting its turn in the generator's queue.
 *
 * The scheduling is the simple "one period per tick, drift-corrected"
 * pattern: ``next_issue_ns`` starts at the phase's first tick and is
 * advanced by the period each iteration, so a tick that ran late (because
 * the previous send blocked, or because the event loop took a while to
 * come back) does not stretch the schedule forward — it compresses
 * subsequent ones. A late tick that falls past the deadline still counts
 * as a missed send, because the measurement window ended before it could
 * have been issued, and that is exactly the outcome the server_limits
 * suite wants recorded.
 *
 * The issue loop still caps at ``max_outstanding`` so a generator running
 * ahead of its target cannot stack requests indefinitely while the
 * connection is healthy; once the cap is hit, ``run_iterate`` is driven
 * until the in-flight count drops back below it. */
static void
run_open_loop(ClientContext *context, uint64_t deadline_ns, uint64_t grace_ns,
              uint64_t target_rate) {
    /* The send period in nanoseconds; one second is the natural unit of
     * ``target_rate`` (ops/sec). At least one nanosecond to avoid a division
     * by zero if a runner ever asked for a rate past what fits in uint64. */
    uint64_t period_ns = UINT64_C(1000000000) / target_rate;
    uint64_t next_issue_ns = o6_limits_now_ns();
    context->issued = 0;
    context->retired = 0;
    while(!context->connection_broken && o6_limits_now_ns() < deadline_ns) {
        uint64_t now_ns = o6_limits_now_ns();
        if(now_ns >= next_issue_ns &&
           context->issued - context->retired < context->max_outstanding) {
            issue_one(context, next_issue_ns);
            next_issue_ns += period_ns;
            if(context->connection_broken || context->send_refused != UA_STATUSCODE_GOOD)
                break;
            continue;
        }
        /* The schedule is ahead of us or the pipeline is full; pump events
         * briefly so the in-flight count comes back down. */
        if(context->retired < context->issued) {
            if(UA_Client_run_iterate(context->client,
                                     O6_LIMITS_EVENT_WAIT_MS) !=
               UA_STATUSCODE_GOOD) {
                context->connection_broken = true;
                break;
            }
        } else {
            /* Nothing in flight and nothing due yet — sleep to the next tick
             * so the loop does not spin. clock_gettime has microsecond
             * resolution; the OS will round up. */
            struct timespec wait;
            uint64_t until_ns = next_issue_ns < deadline_ns
                ? next_issue_ns : deadline_ns;
            uint64_t wait_ns = (until_ns > now_ns) ? (until_ns - now_ns) : 0;
            wait.tv_sec = (time_t)(wait_ns / UINT64_C(1000000000));
            wait.tv_nsec = (long)(wait_ns % UINT64_C(1000000000));
            nanosleep(&wait, NULL);
        }
    }
    if(context->retired < context->issued) {
        uint64_t drain_deadline = o6_limits_now_ns() + grace_ns;
        while(context->retired < context->issued &&
              o6_limits_now_ns() < drain_deadline) {
            if(UA_Client_run_iterate(context->client, O6_LIMITS_EVENT_WAIT_MS) !=
               UA_STATUSCODE_GOOD)
                break;
        }
    }
    while(context->retired < context->issued) {
        account(context, UA_STATUSCODE_BADCONNECTIONCLOSED, 0);
        context->retired++;
    }
}

static void
print_result(const O6_LimitsOptions *options, const ClientContext *context,
            uint64_t start_ns, uint64_t end_ns, int connect_failed,
            UA_StatusCode connect_status) {
    size_t index;
    printf("{\"connect_failed\":%d,\"connect_status\":\"%s\","
           "\"connection_broken\":%d,\"send_refused\":\"%s\","
           "\"outstanding\":%zu,"
           "\"open_loop\":%d,\"target_rate\":%" PRIu64 ","
           "\"attempted\":%zu,\"succeeded\":%" PRIu64 ","
           "\"timeouts\":%" PRIu64 ",\"connection_lost\":%" PRIu64 ","
           "\"other_errors\":%" PRIu64 ",\"start_ns\":%" PRIu64 ","
           "\"end_ns\":%" PRIu64 ",\"histogram_us\":[",
           connect_failed, UA_StatusCode_name(connect_status),
           context->connection_broken,
           context->send_refused == UA_STATUSCODE_GOOD
               ? "" : UA_StatusCode_name(context->send_refused),
           options->max_outstanding,
           options->open_loop, options->target_rate,
           context->issued, context->succeeded, context->timeouts,
           context->connection_lost, context->other_errors, start_ns, end_ns);
    for(index = 0; index < O6_HISTOGRAM_BUCKETS; ++index)
        printf("%s%" PRIu64, index == 0 ? "" : ",", context->histogram[index]);
    printf("]}\n");
    fflush(stdout);
}

int
main(int argc, char **argv) {
    O6_LimitsOptions options = {
        .endpoint = O6_DEFAULT_ENDPOINT,
        .duration_ms = 5000,
        .warmup_ms = 500,
        .timeout_ms = 2000,
        .grace_ms = 2000,
        .max_outstanding = 1,
        .seed = 1,
        .open_loop = 0,
        .target_rate = 0,
    };
    ClientContext context;
    UA_StatusCode status;
    uint64_t start_ns;
    uint64_t end_ns;

    if(!o6_limits_parse_options(argc, argv, &options)) {
        o6_limits_usage(argv[0]);
        return EXIT_FAILURE;
    }

    memset(&context, 0, sizeof(context));
    context.random_state = options.seed;
    context.max_outstanding = options.max_outstanding;
    context.service = options.service;
    context.first_node_id = options.first_node_id;
    context.node_count = options.node_count;
    context.batch_size = options.batch_size;
    context.write_string = options.write_string;

    context.client = UA_Client_new();
    if(!context.client)
        return EXIT_FAILURE;
    status = UA_ClientConfig_setDefault(UA_Client_getConfig(context.client));
    if(status == UA_STATUSCODE_GOOD) {
        UA_ClientConfig *config = UA_Client_getConfig(context.client);
        config->timeout = options.timeout_ms;
        /* open62541 caps a client at 32 outstanding application service calls
         * by default and refuses the next one outright with
         * BadTooManyOperations. Left alone, every server would appear to "give
         * up" the moment this benchmark asked for a pipeline deeper than 32 —
         * the same load level for every implementation, because the limit is
         * here rather than in any of them. The cap is raised to exactly what
         * this run was told to keep in flight, which is the invariant the
         * issue loop below already maintains. The fix is shared with the
         * throughput clients (common/contract.h). */
        o6_configure_client(config, options.max_outstanding);
    }
    if(status == UA_STATUSCODE_GOOD)
        status = UA_Client_connect(context.client, options.endpoint);
    if(status != UA_STATUSCODE_GOOD) {
        /* A refused or timed-out connection attempt is itself a measurement —
         * the server (or its listen backlog) already gave up on this client —
         * so it is reported like any other outcome rather than treated as the
         * harness failing to start. */
        print_result(&options, &context, 0, 0, 1, status);
        UA_Client_delete(context.client);
        return EXIT_SUCCESS;
    }

    puts(O6_LIMITS_READY_MARKER);
    fflush(stdout);
    if(getchar() == EOF) {
        UA_Client_disconnect(context.client);
        UA_Client_delete(context.client);
        return EXIT_FAILURE;
    }

    /* Warm-up: untimed, uncounted, but drained the same way as the timed
     * window so leftover in-flight requests cannot bleed into it. The
     * closed-loop warm-up keeps the existing semantics regardless of the
     * timed window's shape — open-loop is measured cold anyway, because
     * what the warm-up is filling is the channel/pipe, not the schedule. */
    run_closed_loop(&context,
                    o6_limits_now_ns() + options.warmup_ms * UINT64_C(1000000),
                    options.grace_ms * UINT64_C(1000000));

    context.counting = true;
    start_ns = o6_limits_now_ns();
    if(options.open_loop)
        run_open_loop(&context,
                      start_ns + options.duration_ms * UINT64_C(1000000),
                      options.grace_ms * UINT64_C(1000000),
                      options.target_rate);
    else
        run_closed_loop(&context,
                        start_ns + options.duration_ms * UINT64_C(1000000),
                        options.grace_ms * UINT64_C(1000000));
    end_ns = o6_limits_now_ns();

    print_result(&options, &context, start_ns, end_ns, 0, UA_STATUSCODE_GOOD);
    UA_Client_disconnect(context.client);
    UA_Client_delete(context.client);
    return EXIT_SUCCESS;
}
