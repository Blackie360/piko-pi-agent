"""Loopback-only receiver tests. No real provider messages or credentials."""
import asyncio
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from public_receiver import pi_receiver as r


class BoundaryTests(unittest.TestCase):
    def test_exact_target_only(self):
        self.assertEqual(r.response_for('POST', r.PATH)[0], 401)
        for path in ['/', '/api/state', '/webhooks', r.PATH+'/', r.PATH+'/child',
                     r.PATH+'-other', r.PATH+'?a=b', '/webhooks//flaresend',
                     '/webhooks/%66laresend', '/webhooks/flaresend%2f',
                     '/WEBHOOKS/flaresend', '/webhooks/./flaresend',
                     '/webhooks/flaresend/../api/state']:
            with self.subTest(path=path):
                self.assertEqual(r.response_for('POST', path)[0], 404)

    def test_credential_loading_fails_closed(self):
        with patch.dict(r.os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                r.load_token()
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(r.os.environ, {'CREDENTIALS_DIRECTORY': folder}):
                with self.assertRaises(RuntimeError):
                    r.load_token()
                path = Path(folder) / 'flaresend-token'
                for value in [b'', b'short', b'x'*4097, b'valid-length-but-newline\n']:
                    path.write_bytes(value)
                    with self.assertRaises(RuntimeError):
                        r.load_token()
                path.write_bytes(b'synthetic-test-token-only')
                self.assertEqual(r.load_token(), b'synthetic-test-token-only')

    def test_never_reports_ready(self):
        for method in ['GET', 'HEAD']:
            status, body = r.response_for(method, r.PATH)
            self.assertEqual(status, 503)
            self.assertIs(body['active'], False)
        self.assertEqual(r.response_for('PUT', r.PATH)[0], 405)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.receiver = r.Receiver(b'synthetic-test-token-only')
        self.server = await asyncio.start_server(self.receiver.handle, '127.0.0.1', 0, limit=r.HEADER_LIMIT)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def request(self, data, framed=False):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(data); await writer.drain()
        if framed:
            headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
            length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                          if line.lower().startswith(b'content-length:'))
            body = b'' if data.startswith(b'HEAD ') else await asyncio.wait_for(reader.readexactly(length), 2)
            result = headers + body
        else:
            result = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            # A rejected request may reset after its complete framed response.
            pass
        return result

    async def test_rejects_without_waiting_for_or_echoing_body(self):
        data = b'POST /webhooks/flaresend HTTP/1.1\r\nHost: fixture\r\nContent-Length: 900000\r\nAuthorization: synthetic-fixture\r\n\r\n'
        result = await self.request(data)
        self.assertTrue(result.startswith(b'HTTP/1.1 401'))
        self.assertIn(b'Connection: close', result)
        self.assertNotIn(b'synthetic-fixture', result)

    async def test_oversized_headers_and_malformed_requests(self):
        result = await self.request(b'POST /webhooks/flaresend HTTP/1.1\r\nX: '+b'x'*9000+b'\r\n\r\n')
        self.assertTrue(result.startswith(b'HTTP/1.1 431'))
        result = await self.request(b'garbage\r\n\r\n')
        self.assertTrue(result.startswith(b'HTTP/1.1 400'))

    async def test_pipeline_does_not_reach_second_route(self):
        result = await self.request(b'POST /webhooks/flaresend HTTP/1.1\r\nHost: fixture\r\n\r\nGET /api/state HTTP/1.1\r\nHost: fixture\r\n\r\n')
        self.assertEqual(result.count(b'HTTP/1.1'), 1)
        self.assertTrue(result.startswith(b'HTTP/1.1 401'))

    async def test_authentication_required_and_no_processing(self):
        cases = [
            (b'', 401),
            (b'Authorization: Bearer incorrect-token\r\n', 401),
            (b'Authorization: Basic synthetic-test-token-only\r\n', 401),
            (b'Authorization: Bearer synthetic-test-token-only\r\nAuthorization: Bearer synthetic-test-token-only\r\n', 401),
            (b'Authorization: Bearer synthetic-test-token-only\r\n', 503),
            (b'authorization: bearer synthetic-test-token-only\r\n', 503),
        ]
        for header, expected in cases:
            with self.subTest(header_type=expected):
                result = await self.request(b'POST /webhooks/flaresend HTTP/1.1\r\nHost: fixture\r\n' + header + b'Content-Length: 900000\r\n\r\n')
                self.assertTrue(result.startswith(f'HTTP/1.1 {expected}'.encode()))
                self.assertNotIn(b'synthetic-test-token-only', result)
                if expected == 503:
                    self.assertIn(b'message_processing_not_enabled', result)

    async def test_head_and_capacity(self):
        result = await self.request(b'HEAD /webhooks/flaresend HTTP/1.1\r\nHost: fixture\r\n\r\n')
        self.assertTrue(result.startswith(b'HTTP/1.1 503'))
        self.assertEqual(result.split(b'\r\n\r\n', 1)[1], b'')
        with patch.object(r, 'MAX_CONNECTIONS', 0):
            result = await self.request(b'POST /webhooks/flaresend HTTP/1.1\r\n\r\n', framed=True)
        self.assertTrue(result.startswith(b'HTTP/1.1 503'))
        self.assertEqual(result.split(b'\r\n\r\n', 1)[1], b'{"accepted":false,"error":"busy"}')
        self.assertEqual(self.receiver.connections, 0)


if __name__ == '__main__':
    unittest.main()

