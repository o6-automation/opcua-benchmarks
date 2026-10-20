# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
#    Copyright 2026 (c) o6 Automation GmbH (Author: Daniel Opitz)

"""Counter node setup for the asynchronous o6 API and configurable limits."""

import asyncio


async def prepare(args, counters):
    import o6

    server = o6.Server(port=args.port, loop=asyncio.get_running_loop())
    server.config.samplingIntervalLimits = [1, 86400e3]
    server.config.publishingIntervalLimits = [1, 86400e3]
    server.config.queueSizeLimits = [1, 1]
    server.config.maxMonitoredItems = args.items
    server.config.maxMonitoredItemsPerSubscription = args.items
    server.config.maxNotificationsPerPublish = 2**32 - 1
    nodes = []
    for i in range(args.items):
        node = await server.addVariable("Counter", server.objectsNode, o6.UInt64(0), nodeId=f"ns=1;i={i + 1}")
        await node(value=o6.Double(1), attr="MinimumSamplingInterval")
        nodes.append(node)
        if counters is not None:

            def read(node, index=i, **kwargs):
                return o6.StatusCode.GOOD, o6.UInt64(counters.read(index))

            server.implement(node, read=read)

    async def write(node, value):
        await node(value=o6.UInt64(value))

    return server, nodes, write
