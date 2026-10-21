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

#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <poll.h>
#include <open62541/plugin/eventloop.h>

/* Call before constructing the client/server, while no SDK timers exist.
 * Accept both fresh loops and defaults that already started the loop. */
static UA_StatusCode configure_clock(UA_EventLoop *loop) {
    if(!loop) return UA_STATUSCODE_BADINTERNALERROR;
    if(loop->state == UA_EVENTLOOPSTATE_STARTED) {
        loop->stop(loop);
        while(loop->state == UA_EVENTLOOPSTATE_STOPPING) {
            UA_StatusCode status = loop->run(loop, 0);
            if(status) return status;
        }
    }
    if(loop->state != UA_EVENTLOOPSTATE_FRESH && loop->state != UA_EVENTLOOPSTATE_STOPPED)
        return UA_STATUSCODE_BADINTERNALERROR;
    /* Match the coordinator and recorder. RAW can advance at a different rate
     * from MONOTONIC, biasing both sampling counts and publishing boundaries. */
    UA_Int32 source = CLOCK_MONOTONIC;
    UA_StatusCode status = UA_KeyValueMap_setScalar(&loop->params,
        UA_QUALIFIEDNAME(0, "clock-source-monotonic"), &source, &UA_TYPES[UA_TYPES_INT32]);
    return status ? status : loop->start(loop);
}

static FILE *output;
static unsigned port, count, sampling_ms;
static bool measure_server;
static uint64_t start_ns, end_ns, measured_start_ns, measured_end_ns;
static bool recording_enabled;
static uint64_t interval_ns, *interval_counts;
static unsigned interval_count;

static void record_progress(uint64_t timestamp, uint64_t delta) {
    interval_counts[(timestamp - measured_start_ns) / interval_ns] += delta;
}

static void write_intervals(void) {
    fputs(",\"interval_counts\":[", output);
    for(unsigned i = 0; i < interval_count; ++i)
        fprintf(output, "%s%" PRIu64, i ? "," : "", interval_counts[i]);
    fputc(']', output);
}

static uint64_t now_ns(void) {
    struct timespec value;
    if(clock_gettime(CLOCK_MONOTONIC, &value)) abort();
    return (uint64_t)value.tv_sec * 1000000000 + (uint64_t)value.tv_nsec;
}

static void arguments(int argc, char **argv) {
    if(argc != 5) exit(2);
    port = (unsigned)strtoul(argv[1], NULL, 10);
    count = (unsigned)strtoul(argv[2], NULL, 10);
    sampling_ms = (unsigned)strtoul(argv[3], NULL, 10);
    if(!port || port > 65535 || !count || count > 1048576 || !sampling_ms || sampling_ms > 100) exit(2);
    if(strcmp(argv[4], "server") && strcmp(argv[4], "client")) exit(2);
    measure_server = !strcmp(argv[4], "server");
    output = fdopen(dup(STDOUT_FILENO), "w");
    if(!output || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) exit(2);
    setvbuf(output, NULL, _IOLBF, 0);
    setvbuf(stdin, NULL, _IONBF, 0);
}

/* Controls are short, newline-terminated coordinator writes. */
static int control(void) {
    struct pollfd input = {STDIN_FILENO, POLLIN, 0};
    if(poll(&input, 1, 0) <= 0) return 0;
    char line[128];
    if(!fgets(line, sizeof(line), stdin) || !strcmp(line, "quit\n")) return -1;
    uint64_t begin, finish, measured_begin, measured_finish, period;
    if(sscanf(line, "arm %" SCNu64 " %" SCNu64 " %" SCNu64 " %" SCNu64 " %" SCNu64,
              &begin, &finish, &measured_begin, &measured_finish, &period) != 5 ||
       start_ns || begin <= now_ns() || measured_begin < begin || measured_finish <= measured_begin ||
       finish < measured_finish || !period || (measured_finish - measured_begin) % period ||
       (measured_finish - measured_begin) / period > 1000) exit(2);
    start_ns = begin;
    end_ns = finish;
    measured_start_ns = measured_begin;
    measured_end_ns = measured_finish;
    interval_ns = period;
    interval_count = (unsigned)((measured_finish - measured_begin) / period);
    if(recording_enabled) {
        interval_counts = (uint64_t *)calloc(interval_count, sizeof(*interval_counts));
        if(!interval_counts) exit(2);
    }
    fprintf(output, "{\"event\":\"armed\",\"start_ns\":%" PRIu64 ",\"end_ns\":%" PRIu64
            ",\"measured_start_ns\":%" PRIu64 ",\"measured_end_ns\":%" PRIu64 ",\"interval_ns\":%" PRIu64 "}\n",
            start_ns, end_ns, measured_start_ns, measured_end_ns, interval_ns);
    return 1;
}

static inline void idle(void) {
    struct timespec delay = {0, 100000};
    nanosleep(&delay, NULL);
}
