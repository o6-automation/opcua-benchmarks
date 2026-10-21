// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 o6 Automation GmbH
#include <assert.h>
#include <open62541/server.h>
#include <open62541/server_config_default.h>
#include <open62541/client.h>
#include <open62541/client_config_default.h>
#include "../subscription/native/worker.h"

static void check_clock(UA_EventLoop *loop) {
    assert(configure_clock(loop) == UA_STATUSCODE_GOOD);
    const UA_Int32 *source = (const UA_Int32 *)UA_KeyValueMap_getScalar(
        &loop->params, UA_QUALIFIEDNAME(0, "clock-source-monotonic"), &UA_TYPES[UA_TYPES_INT32]);
    assert(source && *source == CLOCK_MONOTONIC);
    /* Check the running clock, not just its configuration or nominal rate. */
    UA_DateTime before = (UA_DateTime)(now_ns() / 100) + UA_DATETIME_UNIX_EPOCH;
    UA_DateTime actual = loop->dateTime_nowMonotonic(loop);
    UA_DateTime after = (UA_DateTime)(now_ns() / 100) + UA_DATETIME_UNIX_EPOCH;
    assert(before <= actual && actual <= after);
}

int main(void) {
    UA_ServerConfig server = {0};
    assert(UA_ServerConfig_setMinimal(&server, 0, NULL) == UA_STATUSCODE_GOOD);
    /* Master leaves the loop fresh; also exercise reconfiguration while running. */
    assert(server.eventLoop->state == UA_EVENTLOOPSTATE_FRESH);
    check_clock(server.eventLoop);
    check_clock(server.eventLoop);
    UA_ServerConfig_clear(&server);

    UA_ClientConfig client = {0};
    assert(UA_ClientConfig_setDefault(&client) == UA_STATUSCODE_GOOD);
    assert(client.eventLoop->state == UA_EVENTLOOPSTATE_FRESH);
    check_clock(client.eventLoop);
    UA_ClientConfig_clear(&client);
    return 0;
}
