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

#include <open62541/client.h>
#include <open62541/client_config_default.h>
#include <open62541/client_subscriptions.h>
#include "worker.h"

typedef struct {
    uint64_t value, baseline, received, previous_ns;
} Observation;
static uint64_t errors, delivery_intervals, min_delta, max_delta, max_gap_ns;

static void changed(UA_Client *client, UA_UInt32 subscription, void *context,
                    UA_UInt32 item, void *item_context, UA_DataValue *value) {
    (void)client; (void)subscription; (void)context; (void)item;
    /* Server mode has no recorder allocation and never reads the receipt clock. */
    if(measure_server) return;
    if(!start_ns) return;
    uint64_t now = now_ns();
    if(now < start_ns || now >= measured_end_ns) return;
    Observation *state = (Observation *)item_context;
    /* Queue replacement may add informational bits while the value stays Good. */
    if((value->hasStatus && !UA_StatusCode_isGood(value->status)) || !value->hasValue ||
       !UA_Variant_hasScalarType(&value->value, &UA_TYPES[UA_TYPES_UINT64])) { ++errors; return; }
    uint64_t next = *(UA_UInt64 *)value->value.data;
    if(next < state->value) { ++errors; return; }
    if(now < measured_start_ns) {
        state->baseline = state->value = next;
        return;
    }
    if(!next) return;
    /* Only consecutive receipts within the retained window form full intervals. */
    if(state->previous_ns) {
        uint64_t delta = next - state->value, gap = now - state->previous_ns;
        if(!delivery_intervals || delta < min_delta) min_delta = delta;
        if(delta > max_delta) max_delta = delta;
        if(gap > max_gap_ns) max_gap_ns = gap;
        ++delivery_intervals;
    }
    record_progress(now, next - state->value);
    state->value = next;
    state->previous_ns = now;
    ++state->received;
}

static void status_changed(UA_Client *client, UA_UInt32 subscription, void *context,
                           UA_StatusChangeNotification *notification) {
    (void)client; (void)subscription; (void)context;
    if(notification->status != UA_STATUSCODE_GOOD) ++errors;
}

int main(int argc, char **argv) {
    if(argc != 6) return 2;
    unsigned publishing_ms = (unsigned)strtoul(argv[5], NULL, 10);
    if(!publishing_ms || publishing_ms > 60000) return 2;
    arguments(argc - 1, argv);
    recording_enabled = !measure_server;
    Observation *states = measure_server ? NULL : (Observation *)calloc(count, sizeof(*states));
    if(!measure_server && !states) return 2;
    UA_ClientConfig initial_config = {0};
    if(UA_ClientConfig_setDefault(&initial_config) || configure_clock(initial_config.eventLoop)) {
        UA_ClientConfig_clear(&initial_config);
        return 2;
    }
    UA_Client *client = UA_Client_newWithConfig(&initial_config);
    if(!client) return 2;
    UA_ClientConfig *config = UA_Client_getConfig(client);
    config->timeout = 10000;
    config->requestedSessionTimeout = 60000;
    config->outStandingPublishRequests = 4;
    char endpoint[80];
    snprintf(endpoint, sizeof(endpoint), "opc.tcp://127.0.0.1:%u", port);
    if(UA_Client_connect(client, endpoint)) return 2;
    UA_CreateSubscriptionRequest request = UA_CreateSubscriptionRequest_default();
    request.requestedPublishingInterval = publishing_ms;
    request.requestedLifetimeCount = 10000;
    request.requestedMaxKeepAliveCount = 10;
    request.maxNotificationsPerPublish = 0;
    request.publishingEnabled = true;
    UA_CreateSubscriptionResponse sub = UA_Client_Subscriptions_create(client, request, NULL, status_changed, NULL);
    if(sub.responseHeader.serviceResult) return 2;
    double revised_sampling = -1;
    for(unsigned first = 0; first < count; first += 256) {
        unsigned size = count - first;
        if(size > 256) size = 256;
        UA_MonitoredItemCreateRequest items[256];
        UA_Client_DataChangeNotificationCallback callbacks[256];
        void *contexts[256];
        for(unsigned j = 0; j < size; ++j) {
            items[j] = UA_MonitoredItemCreateRequest_default(UA_NODEID_NUMERIC(1, first + j + 1));
            items[j].monitoringMode = UA_MONITORINGMODE_REPORTING;
            items[j].requestedParameters.samplingInterval = sampling_ms;
            items[j].requestedParameters.queueSize = 1;
            items[j].requestedParameters.discardOldest = true;
            callbacks[j] = changed;
            contexts[j] = states ? &states[first + j] : NULL;
        }
        UA_CreateMonitoredItemsRequest batch;
        UA_CreateMonitoredItemsRequest_init(&batch);
        batch.subscriptionId = sub.subscriptionId;
        batch.timestampsToReturn = UA_TIMESTAMPSTORETURN_NEITHER;
        batch.itemsToCreateSize = size;
        batch.itemsToCreate = items;
        UA_CreateMonitoredItemsResponse response = UA_Client_MonitoredItems_createDataChanges(client, batch, contexts, callbacks, NULL);
        if(response.responseHeader.serviceResult || response.resultsSize != size) return 2;
        for(unsigned j = 0; j < size; ++j) {
            UA_MonitoredItemCreateResult *result = &response.results[j];
            if(result->statusCode || result->revisedQueueSize != 1) return 2;
            if(revised_sampling < 0) revised_sampling = result->revisedSamplingInterval;
            if(revised_sampling != result->revisedSamplingInterval) return 2;
        }
        UA_CreateMonitoredItemsResponse_clear(&response);
    }
    fprintf(output, "{\"event\":\"ready\",\"sdk_clock\":\"CLOCK_MONOTONIC\","
                    "\"publishing_ms\":%.17g,\"publishing_enabled\":true,"
                    "\"sampling_ms\":%.17g,\"items\":%u,\"queue_size\":1}\n",
            sub.revisedPublishingInterval, revised_sampling, count);
    UA_CreateSubscriptionResponse_clear(&sub);
    bool finished = false;
    while(true) {
        if(control() < 0) break;
        if(UA_Client_run_iterate(client, 1) || errors) return 2;
        if(!finished && start_ns && now_ns() >= end_ns + 2000000000) {
            finished = true;
            if(measure_server) fputs("{\"event\":\"done\"}\n", output);
            else {
                fputs("{\"event\":\"result\",\"counts\":[", output);
                uint64_t notifications = 0, covered = 0, intervals_min = UINT64_MAX;
                for(unsigned i = 0; i < count; ++i) {
                    Observation *s = &states[i];
                    fprintf(output, "%s%" PRIu64, i ? "," : "", s->value - s->baseline);
                    notifications += s->received;
                    if(s->received) ++covered;
                    uint64_t intervals = s->received ? s->received - 1 : 0;
                    if(intervals < intervals_min) intervals_min = intervals;
                }
                fprintf(output, "],\"delivery\":{\"notifications\":%" PRIu64
                        ",\"items_with_notifications\":%" PRIu64 ",\"intervals_min\":%" PRIu64
                        ",\"intervals_total\":%" PRIu64 ",\"min_delta\":%" PRIu64
                        ",\"max_delta\":%" PRIu64 ",\"max_gap_ms\":%.6f}",
                        notifications, covered, intervals_min, delivery_intervals, min_delta, max_delta, max_gap_ns / 1e6);
                write_intervals();
                fputs("}\n", output);
            }
        }
    }
    UA_Client_disconnect(client);
    UA_Client_delete(client);
    free(states);
    free(interval_counts);
    return 0;
}
