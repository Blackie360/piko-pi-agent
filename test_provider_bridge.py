"""Synthetic provider normalization, HTTP framing and credential tests."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from public_receiver import pi_receiver as r
from whatsapp import credential, AdapterError


def event(**changes):
    data = {'from':'254700000001@s.whatsapp.net', 'id':'synthetic-1',
            'fromMe':False, 'message':{'conversation':'Check health'}}
    data.update(changes)
    return json.dumps({'event':'message_received', 'instanceId':'test-instance', 'data':data}).encode()


def bridge():
    return r.Bridge('test-instance', ['254700000001'], '254700000099', b'x'*32)


class Normalization(unittest.TestCase):
    def test_flags_sender_and_instance_fail_closed(self):
        b = bridge()
        self.assertEqual(b.normalize(event())['sender'], '254700000001')
        for changes in [dict(fromMe=None), dict(fromMe='false'), dict(isGroup='false'),
                        dict(key={'fromMe':True}), dict(key={'remoteJid':'other'}),
                        dict(key={'id':'other'}), {'from':'123@lid'}, {'message':{}}, {'id':[]},
                        {'message':{'conversation':'x'*8001}}]:
            with self.subTest(changes=changes), self.assertRaises(r.BridgeError):
                b.normalize(event(**changes))
        data = json.loads(event()); del data['data']['fromMe']
        self.assertEqual(b.normalize(json.dumps(data).encode())['sender'], '254700000001')
        data['data']['key'] = {'fromMe':False}
        self.assertEqual(b.normalize(json.dumps(data).encode())['from_self'], False)
        for body in [event(**{'from':'254700000002@s.whatsapp.net'}),
                     event().replace(b'test-instance', b'other-instance')]:
            with self.assertRaises(r.BridgeError) as error: b.normalize(body)
            self.assertEqual(error.exception.status, 403)
        for body in [b'[]', b'{', b'{"event":1,"event":2}', b'x'*16385]:
            with self.assertRaises(r.BridgeError): b.normalize(body)

    def test_ignored_events_never_become_tasks(self):
        b = bridge()
        for body in [event(fromMe=True), event(isGroup=True), event(**{'from':'123@g.us'}),
                     event(**{'from':'254700000099@s.whatsapp.net'}),
                     event().replace(b'message_received', b'message_sent')]:
            self.assertIsNone(b.normalize(body))

    def test_disabled_and_invalid_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(r.Bridge.from_environment())
        with patch.dict(os.environ, {'PI_WHATSAPP_PROCESSING_ENABLED':'1'}, clear=True):
            with self.assertRaises(RuntimeError): r.Bridge.from_environment()
        with self.assertRaises(RuntimeError):
            r.Bridge('instance', ['254700000001'], '254700000001', b'x'*32)

    def test_systemd_credentials_do_not_fall_back(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY':folder, 'LEGACY':'fallback'}, clear=True):
                with self.assertRaises(AdapterError): credential('piko-relay-key', 'LEGACY')
                path = Path(folder)/'piko-relay-key'; path.write_bytes(b'x'*32)
                self.assertEqual(credential('piko-relay-key','LEGACY'), 'x'*32)
                self.assertEqual(r.load_token('piko-relay-key',32), b'x'*32)
                path.write_bytes(b'x'*32+b'\n')
                with self.assertRaises(AdapterError): credential('piko-relay-key','LEGACY')

    def test_transport_fixed_destination_sanitized_errors_and_no_retry(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200; response.read.return_value = b'{"status":"accepted","run":"private"}'
        with patch.object(r.http.client,'HTTPConnection',return_value=connection) as factory:
            self.assertEqual(bridge().forward(b'{}',{}), (200,{'status':'accepted'}))
            factory.assert_called_once_with('127.0.0.1',8765,timeout=5)
            connection.request.assert_called_once_with('POST','/integrations/whatsapp/relay',b'{}',{})
            connection.close.assert_called_once()
            response.status = 302
            self.assertEqual(bridge().forward(b'{}',{})[0],502)
            response.status = 200; response.read.return_value = b'[]'
            self.assertEqual(bridge().forward(b'{}',{})[0],502)
            response.read.side_effect = TimeoutError('secret-fixture')
            self.assertEqual(bridge().forward(b'{}',{}),(502,{'accepted':False,'error':'adapter_unavailable'}))


class EnabledProtocol(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = bridge()
        self.bridge.accept = AsyncMock(return_value=(200,{'status':'accepted'}))
        self.receiver = r.Receiver(b'fixture-token-only',self.bridge)
        self.server = await asyncio.start_server(self.receiver.handle,'127.0.0.1',0,limit=r.HEADER_LIMIT)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close(); await self.server.wait_closed()

    async def request(self, headers, body=b'', token=b'fixture-token-only'):
        reader,writer = await asyncio.open_connection('127.0.0.1',self.port)
        writer.write(b'POST /webhooks/flaresend HTTP/1.1\r\nAuthorization: Bearer '+token+b'\r\n'+headers+b'\r\n'+body)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(),2)
        writer.close(); await writer.wait_closed()
        return int(raw.split()[1])

    async def test_body_framing_and_authentication_before_dispatch(self):
        for headers, status in [
            (b'Content-Type: application/json\r\n',400),
            (b'Content-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n',400),
            (b'Content-Type: application/json\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n',400),
            (b'Content-Type: text/plain\r\nContent-Length: 2\r\n',400),
            (b'Content-Type: application/json\r\nContent-Length: 16385\r\n',413),
            (b'Content-Type: application/json\r\nContent-Length: 2\r\nContent-Encoding: gzip\r\n',400)]:
            self.assertEqual(await self.request(headers),status)
        self.assertEqual(await self.request(b'Content-Length: 99999\r\n',token=b'wrong'),401)
        self.bridge.accept.assert_not_awaited()
        headers = b'Content-Type: application/json\r\nContent-Length: 2\r\n'
        self.assertEqual(await self.request(headers,b'{}'),200)
        self.bridge.accept.assert_awaited_once_with(b'{}')

    async def test_incomplete_body_times_out_without_dispatch(self):
        with patch.object(r,'HEADER_TIMEOUT',.05):
            self.assertEqual(await self.request(b'Content-Type: application/json\r\nContent-Length: 2\r\n'),408)
        self.bridge.accept.assert_not_awaited()


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_verification_never_forwards_and_logs_no_content(self):
        import time
        verifier = r.Verification('test-instance','254700000001','254700000099',time.time()+60)
        with patch.object(r.Bridge,'forward',side_effect=AssertionError('must not forward')), patch('builtins.print') as log:
            self.assertEqual((await verifier.accept(event()))[0],200)
            report = json.loads(log.call_args.args[0].split(' ',1)[1])
            self.assertTrue(report['valid_incoming_task'])
            self.assertTrue(all(type(value) is bool for value in report.values()))
            self.assertNotIn('Check health',str(log.call_args))
            self.assertNotIn('254700000001',str(log.call_args))
            self.assertEqual((await verifier.accept(event()))[0],503)

    async def test_missing_direction_expiry_and_unrelated_sender(self):
        import time
        verifier = r.Verification('test-instance','254700000001','254700000099',time.time()+60)
        with patch('builtins.print') as log:
            await verifier.accept(event(**{'from':'254700000002@s.whatsapp.net'}))
            log.assert_not_called()
            data = json.loads(event()); del data['data']['fromMe']
            await verifier.accept(json.dumps(data).encode())
            self.assertTrue(json.loads(log.call_args.args[0].split(' ',1)[1])['valid_incoming_task'])
        verifier.finished = False; verifier.deadline = time.time()-1
        self.assertEqual((await verifier.accept(event()))[0],503)
        with patch.dict(os.environ, {'PI_WHATSAPP_VERIFY_ONLY':'1','PI_WHATSAPP_PROCESSING_ENABLED':'1'},clear=True):
            with self.assertRaises(RuntimeError): r.configured_bridge()


class URLAuthentication(unittest.IsolatedAsyncioTestCase):
    async def test_exact_secret_path_only_no_redirects_or_credential_echo(self):
        handler = AsyncMock(return_value=(200,{'status':'verified_without_processing'}))
        receiver = r.Receiver(b'',type('Fixture',(),{'accept':staticmethod(handler)})(),b'a'*64)
        server = await asyncio.start_server(receiver.handle,'127.0.0.1',0,limit=r.HEADER_LIMIT)
        port = server.sockets[0].getsockname()[1]
        secret_path = r.PATH+'/ '+'a'*64
        secret_path = secret_path.replace('/ ', '/')
        try:
            for target, expected in [(r.PATH,404),(secret_path+'/',404),
                                     (secret_path+'?x=1',404),(secret_path.replace('a'*64,'b'*64),404),
                                     (secret_path,200)]:
                reader,writer = await asyncio.open_connection('127.0.0.1',port)
                writer.write(('POST '+target+' HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}').encode())
                await writer.drain()
                response = await asyncio.wait_for(reader.read(),2)
                writer.close(); await writer.wait_closed()
                self.assertEqual(int(response.split()[1]),expected)
                self.assertNotIn(b'a'*64,response)
                self.assertNotIn(b'Location:',response)
            handler.assert_awaited_once_with(b'{}')
        finally:
            server.close(); await server.wait_closed()
        with self.assertRaises(RuntimeError): r.Receiver(b'',url_token=b'short')


class ReceivedEventDirection(unittest.TestCase):
    def test_absent_flags_do_not_allow_self_groups_or_other_events(self):
        b = bridge()
        for changes in [{'from':'254700000099@s.whatsapp.net'}, {'from':'123@g.us'}, {'isGroup':True}]:
            data = json.loads(event(**changes)); del data['data']['fromMe']
            self.assertIsNone(b.normalize(json.dumps(data).encode()))
        data = json.loads(event()); del data['data']['fromMe']
        data['event'] = 'message_sent'
        self.assertIsNone(b.normalize(json.dumps(data).encode()))
        data['event'] = 'message_received'; data['data']['from'] = '254700000002@s.whatsapp.net'
        with self.assertRaises(r.BridgeError) as error: b.normalize(json.dumps(data).encode())
        self.assertEqual(error.exception.status,403)


class ProviderTimestamp(unittest.TestCase):
    def test_timestamp_is_preserved_and_invalid_values_fail_closed(self):
        self.assertEqual(bridge().normalize(event(timestamp=1700000000))['sent_at'],1700000000)
        for value in [True,0,-1,'1700000000',None]:
            with self.assertRaises(r.BridgeError): bridge().normalize(event(timestamp=value))

