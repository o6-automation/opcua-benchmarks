# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 (c) o6 Automation GmbH (Author: Daniel Opitz)

"""Wire-contract checks. Build with --async-opcua before unittest discovery."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
import select
import socket
import subprocess
import tempfile
import unittest

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

from common.certificates import create_certificate
from common.sdk_workers import SDKS, command


@contextmanager
def server(security):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        args = command("async-opcua") + ["--port", str(port), "--array-sizes", "1001", "--security", security]
        if security != "None":
            for role in ("client", "server"):
                create_certificate(f"urn:o6:benchmark:{role}", role, root / f"{role}.key", root / f"{role}.der")
            args += ["--certificate", str(root / "server.der"), "--private-key", str(root / "server.key"),
                     "--trust-certificate", str(root / "client.der")]
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=stderr, text=True)
            try:
                if not select.select([process.stdout], [], [], 20)[0]:
                    raise RuntimeError("Server did not become ready")
                if "benchmark server ready" not in process.stdout.readline():
                    stderr.seek(0)
                    raise RuntimeError(stderr.read().decode())
                yield f"opc.tcp://127.0.0.1:{port}", root
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()


@unittest.skipUnless((SDKS["async-opcua"].output / "server").is_file(), "Build the async-opcua worker first")
class WireContract(unittest.TestCase):
    def test_scalar_array_and_write_types(self):
        for security in ("None", "Basic256Sha256"):
            with self.subTest(security=security), server(security) as (endpoint, material):
                asyncio.run(self.check_values(endpoint, material, security))

    async def check_values(self, endpoint, material, security):
        client = Client(endpoint)
        client.application_uri = "urn:o6:benchmark:client"
        if security != "None":
            await client.set_security(
                SecurityPolicyBasic256Sha256,
                material / "client.der",
                material / "client.key",
                server_certificate=material / "server.der",
            )
        async with client:
            first, last, array = [client.get_node(f"ns=1;i={i}") for i in (1001, 1100, 2001)]
            self.assertEqual(await first.read_value(), 0)
            self.assertEqual(await last.read_value(), 99)
            self.assertEqual(await array.read_value(), [i % 1000 for i in range(1001)])
            await first.write_value(ua.Variant(123, ua.VariantType.Int32))
            self.assertEqual(await first.read_value(), 123)
            values = [7] * 1001
            await array.write_value(ua.Variant(values, ua.VariantType.Int32))
            self.assertEqual(await array.read_value(), values)
            with self.assertRaises(ua.UaStatusCodeError):
                await array.write_value(ua.Variant(["wrong type"], ua.VariantType.String))
            with self.assertRaises(ua.UaStatusCodeError):
                await client.get_node("ns=1;i=999").read_value()


if __name__ == "__main__":
    unittest.main()
