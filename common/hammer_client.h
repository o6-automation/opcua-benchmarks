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

#ifndef O6_BENCHMARK_HAMMER_CLIENT_H
#define O6_BENCHMARK_HAMMER_CLIENT_H

/* The option struct and parser shared by every suite that compiles a hammer
 * client from common/servers/hammer_client.c. Mirrors the closed-loop fields
 * the server-limits runner already drove, and adds the two open-loop knobs
 * the server_limits runner needs:
 *
 *   --open-loop           issues on a fixed schedule instead of after the
 *                         previous request returns. Each latency sample is
 *                         timed from its *intended* send time so a stall in
 *                         the queue shows in the tail rather than being
 *                         absorbed by the generator slowing down to match.
 *   --target-rate <N>     the rate, in calls per second, the open-loop path
 *                         tries to maintain. Required with --open-loop;
 *                         ignored (and unsettable) without it.
 *
 * The two new flags reuse everything else — the histogram, the readiness
 * marker, the one-JSON-line result protocol, the per-call accounting —
 * so the closed-loop path is byte-for-byte the program it replaced, and
 * the open-loop path is the smallest delta that turns the same generator
 * into a coordinated-omission-free one. */

#include <errno.h>
#include <inttypes.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "contract.h"

/* The barrier marker the runner waits for before releasing every client of a
 * load step at once (a newline on stdin), so a step's timed window starts
 * with every process already connected rather than staggered by however long
 * each one took to dial in. */
#define O6_LIMITS_READY_MARKER "O6_LIMITS_READY"
/* How long UA_Client_run_iterate blocks waiting for network activity between
 * polls. Short enough that the issue loop above it keeps the pipe full. */
#define O6_LIMITS_EVENT_WAIT_MS 100u

/* The service the load generator drives. "read" is the default and the
 * only one the closed-loop ladder of every existing suite uses; "write"
 * and the batched-read services exist so a runner that wants them does
 * not have to ship a second client. The string the runner passes is
 * matched verbatim, and an unknown one is rejected at parse time so a
 * typo fails before any I/O. */
#define O6_LIMITS_SERVICE_READ "read"
#define O6_LIMITS_SERVICE_WRITE "write"
#define O6_LIMITS_SERVICE_READ_BATCH "read-batch"
#define O6_LIMITS_SERVICE_READ_BATCH_100 "read-batch-100"

typedef struct O6_LimitsOptions {
    const char *endpoint;
    uint64_t duration_ms;
    uint64_t warmup_ms;
    uint64_t timeout_ms;
    uint64_t grace_ms;
    size_t max_outstanding;
    uint32_t seed;
    int open_loop;       /* non-zero means replay on a fixed schedule */
    uint64_t target_rate; /* ops/sec; required when open_loop is non-zero */
    /* Which Read/Write variant the binary drives. Defaults to "read" so a
     * runner that does not know about the flag (server_limits' ladder) keeps
     * its behaviour. */
    const char *service;
    /* Node range for the LCG walker. Defaults match the open62541 server's
     * 100-scalar layout so a runner that does not pass them hits the same
     * nodes the server has. */
    uint32_t first_node_id;
    size_t node_count;
    /* Batched Read only: nodes per request. 1 = scalar Read (the default),
     * anything > 1 means the binary fills one Read with N scalar nodes per
     * issue. */
    size_t batch_size;
    /* Write only: the value written. 0 = an Int32 equal to the node id (the
     * default), 1 = a String "value-<node id>", for servers whose nodes hold
     * strings (--write-type string). */
    int write_string;
} O6_LimitsOptions;

static inline uint64_t
o6_limits_now_ns(void) {
    return o6_now_ns();
}

static int
o6_limits_parse_u64(const char *text, uint64_t *result) {
    char *end = NULL;
    unsigned long long parsed;
    errno = 0;
    parsed = strtoull(text, &end, 10);
    if(errno != 0 || !end || *end != '\0')
        return 0;
    *result = (uint64_t)parsed;
    return 1;
}

static int
o6_limits_parse_size(const char *text, size_t *result) {
    uint64_t parsed;
    if(!o6_limits_parse_u64(text, &parsed) || parsed == 0)
        return 0;
    *result = (size_t)parsed;
    return 1;
}

static void
o6_limits_usage(const char *program) {
    fprintf(stderr,
            "Usage: %s [--endpoint URL] [--duration-ms N] [--warmup-ms N] "
            "[--timeout-ms N] [--grace-ms N] [--outstanding N] [--seed N] "
            "[--open-loop] [--target-rate N] "
            "[--service S] [--nodes N] [--first-node-id N] [--batch-size N] "
            "[--write-type int32|string]\n",
            program);
}

static int
o6_limits_parse_options(int argc, char **argv, O6_LimitsOptions *options) {
    int index;
    for(index = 1; index < argc; ++index) {
        if(strcmp(argv[index], "--endpoint") == 0 && index + 1 < argc) {
            options->endpoint = argv[++index];
        } else if(strcmp(argv[index], "--duration-ms") == 0 && index + 1 < argc) {
            if(!o6_limits_parse_u64(argv[++index], &options->duration_ms))
                return 0;
        } else if(strcmp(argv[index], "--warmup-ms") == 0 && index + 1 < argc) {
            if(!o6_limits_parse_u64(argv[++index], &options->warmup_ms))
                return 0;
        } else if(strcmp(argv[index], "--timeout-ms") == 0 && index + 1 < argc) {
            if(!o6_limits_parse_u64(argv[++index], &options->timeout_ms))
                return 0;
        } else if(strcmp(argv[index], "--grace-ms") == 0 && index + 1 < argc) {
            if(!o6_limits_parse_u64(argv[++index], &options->grace_ms))
                return 0;
        } else if(strcmp(argv[index], "--outstanding") == 0 && index + 1 < argc) {
            if(!o6_limits_parse_size(argv[++index], &options->max_outstanding))
                return 0;
        } else if(strcmp(argv[index], "--seed") == 0 && index + 1 < argc) {
            uint64_t seed;
            if(!o6_limits_parse_u64(argv[++index], &seed))
                return 0;
            options->seed = (uint32_t)seed;
        } else if(strcmp(argv[index], "--open-loop") == 0) {
            options->open_loop = 1;
        } else if(strcmp(argv[index], "--target-rate") == 0 && index + 1 < argc) {
            if(!o6_limits_parse_u64(argv[++index], &options->target_rate))
                return 0;
        } else if(strcmp(argv[index], "--service") == 0 && index + 1 < argc) {
            options->service = argv[++index];
        } else if(strcmp(argv[index], "--nodes") == 0 && index + 1 < argc) {
            if(!o6_limits_parse_size(argv[++index], &options->node_count))
                return 0;
        } else if(strcmp(argv[index], "--first-node-id") == 0 && index + 1 < argc) {
            uint64_t parsed;
            if(!o6_limits_parse_u64(argv[++index], &parsed) || parsed == 0 ||
               parsed > UINT32_MAX)
                return 0;
            options->first_node_id = (uint32_t)parsed;
        } else if(strcmp(argv[index], "--batch-size") == 0 && index + 1 < argc) {
            if(!o6_limits_parse_size(argv[++index], &options->batch_size))
                return 0;
        } else if(strcmp(argv[index], "--write-type") == 0 && index + 1 < argc) {
            const char *type = argv[++index];
            if(strcmp(type, "int32") == 0)
                options->write_string = 0;
            else if(strcmp(type, "string") == 0)
                options->write_string = 1;
            else
                return 0;
        } else {
            return 0;
        }
    }
    if(options->duration_ms == 0 || options->timeout_ms == 0 ||
       options->max_outstanding == 0)
        return 0;
    if(options->open_loop && options->target_rate == 0)
        return 0;
    /* Service validation. An empty service is the default ("read") — see
     * the field's docs above. */
    if(options->service == NULL)
        options->service = O6_LIMITS_SERVICE_READ;
    if(strcmp(options->service, O6_LIMITS_SERVICE_READ) != 0 &&
       strcmp(options->service, O6_LIMITS_SERVICE_WRITE) != 0 &&
       strcmp(options->service, O6_LIMITS_SERVICE_READ_BATCH) != 0 &&
       strcmp(options->service, O6_LIMITS_SERVICE_READ_BATCH_100) != 0)
        return 0;
    /* Node range defaults match the open62541 server's 100-scalar layout
     * (O6_FIRST_NODE_ID = 1001, NODE_COUNT = 100), so a runner that does
     * not pass the new flags hits the same nodes the server publishes. */
    if(options->first_node_id == 0)
        options->first_node_id = (uint32_t)O6_FIRST_NODE_ID;
    if(options->node_count == 0)
        options->node_count = (size_t)O6_NODE_COUNT;
    if(options->batch_size == 0)
        options->batch_size = 1;
    return 1;
}

#endif  /* O6_BENCHMARK_HAMMER_CLIENT_H */
