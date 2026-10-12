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

// The OPCUAServerOptions of a node_fronts_server.mjs front, built in that thread, and the subscription
// limits of every thread: the static minimums are per module instance, so the fronts and the session
// worker (which hosts the monitored items) each lower them as node_server.mjs does for its one thread.
// Each front gets its own temporary PKI folder from the stock worker's security module.
import { MonitoredItem, Subscription, RegisterServerMethod } from '../common/node/sdk.mjs';
import { security } from '../common/node/security.mjs';

MonitoredItem.minimumSamplingInterval = 1;
Subscription.minimumPublishingInterval = 1;
Subscription.maxNotificationPerPublishHighLimit = 0xffffffff;

export default async function frontServerOptions({ port }, thread) {
  if (thread?.front === -1) return {};
  const identity = await security({ security: 'None' }, 'server');
  return {
    port, host: '127.0.0.1', hostname: '127.0.0.1', resourcePath: '', allowAnonymous: true,
    serverInfo: { applicationUri: 'urn:o6:benchmark:server', productUri: 'urn:o6:benchmark',
      applicationName: { text: 'Counter benchmark' } },
    registerServerMethod: RegisterServerMethod.HIDDEN,
    securityModes: [identity.mode], securityPolicies: [identity.policy],
    serverCertificateManager: identity.manager, userCertificateManager: identity.manager,
    certificateFile: identity.certificateFile, privateKeyFile: identity.privateKeyFile,
  };
}
