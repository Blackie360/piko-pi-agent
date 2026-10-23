"""Application integration tests. Requires the existing pinned app dependencies."""
import asyncio
import json
import hashlib
import hmac
import time
import httpx
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_app  # Isolated temporary data before app import; never production settings.
import app as a
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_whatsapp import CONFIG


class WhatsAppPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_channel_forces_dashboard_approval_even_when_auto_enabled(self):
        rid = uuid.uuid4().hex
        a.DB.execute("INSERT INTO runs VALUES (?,?,'running')", (rid, 'fixture'))
        a.DB.execute("INSERT INTO run_channels VALUES (?,'whatsapp')", (rid,)); a.DB.commit()
        with patch.object(a, 'settings', return_value={'auto_commands':True, 'auto_sudo':True}), \
             patch.object(a, 'shell', new_callable=AsyncMock) as shell:
            task = asyncio.create_task(a.approved_command(a.Context(rid), 'sudo -n fixture', 'fixture', '/tmp'))
            try:
                async with asyncio.timeout(1):
                    while not a.gates: await asyncio.sleep(.001)
                shell.assert_not_awaited()
                aid = next(aid for aid, gate in a.gates.items() if gate[0] == rid)
                await a.approve(aid, a.Decision(approved=False))
                self.assertIn('declined', await task)
                shell.assert_not_awaited()
                with self.assertRaises(a.HTTPException): await a.approve(aid, a.Decision(approved=True))
            finally:
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_whatsapp_code_approval_releases_only_its_command_gate(self):
        import os
        rid, mid = uuid.uuid4().hex, uuid.uuid4().hex
        a.DB.execute("INSERT INTO runs VALUES (?,?,'running')",(rid,'fixture'))
        a.DB.execute("INSERT INTO run_channels VALUES (?,'whatsapp')",(rid,))
        a.DB.execute('INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,?,?)',
                     (CONFIG.instance,mid,'254700000001','fixture',time.time(),rid,'running')); a.DB.commit()
        with patch.dict(os.environ,{'PI_WHATSAPP_COMMAND_APPROVALS':'1'}), \
             patch.object(a.whatsapp,'config',return_value=CONFIG), \
             patch.object(a.whatsapp,'reply',new_callable=AsyncMock), \
             patch.object(a,'settings',return_value={'auto_commands':True,'auto_sudo':True}), \
             patch.object(a,'shell',new_callable=AsyncMock,return_value='fixture command result') as shell:
            task = asyncio.create_task(a.approved_command(a.Context(rid),'fixture command','fixture purpose','/tmp'))
            try:
                async with asyncio.timeout(1):
                    while not any(p['ready'] and p['rid']==rid for p in a.whatsapp.pending_commands.values()):
                        await asyncio.sleep(.001)
                shell.assert_not_awaited()
                token = next(token for token,p in a.whatsapp.pending_commands.items() if p['rid']==rid)
                await asyncio.sleep(1.05)  # Provider timestamp must follow proposal delivery.
                body = json.dumps(dict(version=1,event='message_received',instance=CONFIG.instance,
                    message_id=uuid.uuid4().hex,sender='254700000001',text='approve',sent_at=int(time.time()),
                    from_self=False,is_group=False)).encode()
                stamp = str(int(time.time()))
                headers={'x-pi-relay-timestamp':stamp,'x-pi-relay-signature':hmac.new(
                    CONFIG.relay_key.encode(),stamp.encode()+b'.'+body,hashlib.sha256).hexdigest()}
                self.assertEqual((await a.whatsapp.accept(body,headers))['status'],'accepted')
                self.assertEqual(await task,'fixture command result')
                self.assertEqual((await a.whatsapp.accept(body,headers))['status'],'duplicate')
                shell.assert_awaited_once_with('fixture command','/tmp',rid)
                self.assertNotIn(token,a.whatsapp.pending_commands)
                await asyncio.gather(*list(a.whatsapp.monitors))
            finally:
                if not task.done(): task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                a.run_status(rid,'complete')

    async def test_whatsapp_denial_never_runs_the_command(self):
        import os
        rid, mid = uuid.uuid4().hex, uuid.uuid4().hex
        a.DB.execute("INSERT INTO runs VALUES (?,?,'running')",(rid,'fixture'))
        a.DB.execute("INSERT INTO run_channels VALUES (?,'whatsapp')",(rid,))
        a.DB.execute('INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,?,?)',
                     (CONFIG.instance,mid,'254700000001','fixture',time.time(),rid,'running')); a.DB.commit()
        with patch.dict(os.environ,{'PI_WHATSAPP_COMMAND_APPROVALS':'1'}), \
             patch.object(a.whatsapp,'config',return_value=CONFIG), \
             patch.object(a.whatsapp,'reply',new_callable=AsyncMock), \
             patch.object(a,'settings',return_value={'auto_commands':True,'auto_sudo':True}), \
             patch.object(a,'shell',new_callable=AsyncMock,return_value='fixture command result') as shell:
            task = asyncio.create_task(a.approved_command(a.Context(rid),'fixture command','fixture purpose','/tmp'))
            try:
                async with asyncio.timeout(1):
                    while not any(p['ready'] and p['rid']==rid for p in a.whatsapp.pending_commands.values()):
                        await asyncio.sleep(.001)
                shell.assert_not_awaited()
                token = next(token for token,p in a.whatsapp.pending_commands.items() if p['rid']==rid)
                body = json.dumps(dict(version=1,event='message_received',instance=CONFIG.instance,
                    message_id=uuid.uuid4().hex,sender='254700000001',text='DENY '+token,
                    from_self=False,is_group=False)).encode()
                stamp = str(int(time.time()))
                headers={'x-pi-relay-timestamp':stamp,'x-pi-relay-signature':hmac.new(
                    CONFIG.relay_key.encode(),stamp.encode()+b'.'+body,hashlib.sha256).hexdigest()}
                self.assertEqual((await a.whatsapp.accept(body,headers))['status'],'accepted')
                self.assertIn('declined',await task)
                self.assertEqual((await a.whatsapp.accept(body,headers))['status'],'duplicate')
                shell.assert_not_awaited()
                self.assertNotIn(token,a.whatsapp.pending_commands)
                await asyncio.gather(*list(a.whatsapp.monitors))
            finally:
                if not task.done(): task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                a.run_status(rid,'complete')

    async def test_session_approval_cannot_skip_sudo_in_either_channel(self):
        for channel in ('whatsapp','dashboard'):
            rid=uuid.uuid4().hex
            a.DB.execute("INSERT INTO runs VALUES (?,?,'running')",(rid,'sudo-fixture'))
            a.DB.execute('INSERT INTO run_channels VALUES (?,?)',(rid,channel));a.DB.commit()
            with patch.object(a.whatsapp,'session_authorized',return_value=True), \
                 patch.object(a.whatsapp,'command_approvals_enabled',return_value=False), \
                 patch.object(a,'settings',return_value={'auto_commands':True,'auto_sudo':True}), \
                 patch.object(a,'shell',new_callable=AsyncMock,return_value='done') as shell:
                await a.approved_command(a.Context(rid),'echo fixture','purpose','/tmp')
                shell.reset_mock()
                task=asyncio.create_task(a.approved_command(a.Context(rid),'/usr/bin/sudo -n fixture','purpose','/tmp'))
                try:
                    async with asyncio.timeout(1):
                        while not any(g[0]==rid for g in a.gates.values()): await asyncio.sleep(.001)
                    shell.assert_not_awaited()
                    aid=next(k for k,v in a.gates.items() if v[0]==rid)
                    await a.approve(aid,a.Decision(approved=False))
                    self.assertIn('declined',await task)
                    shell.assert_not_awaited()
                finally:
                    if not task.done():task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
                    a.run_status(rid,'complete')
        for command in ('sudo -n true','env sudo true',"sh -c 'sudo true'",'echo $(sudo true)'):
            self.assertTrue(a.uses_sudo(command))
        self.assertFalse(a.uses_sudo('echo ordinary'))

    async def test_two_channels_and_delete_only_idle_history(self):
        c1,c2=(await a.new_chat())['id'],(await a.new_chat())['id']
        block=asyncio.Event()
        async def runner(*args):await block.wait()
        with patch.object(a,'settings',return_value={'key':'fixture','model':'fixture'}),patch.object(a,'run_agent',side_effect=runner):
            r1=await a.whatsapp_submit(c1,'WhatsApp fixture')
            r2=(await a.send(c2,a.Message(text='Dashboard fixture')))['run']
            try:
                self.assertEqual(len(a.active),2)
                with self.assertRaises(a.HTTPException):await a.send((await a.new_chat())['id'],a.Message(text='Third'))
                with self.assertRaises(a.HTTPException):await a.delete_chat(c1)
                self.assertTrue(a.DB.execute('SELECT 1 FROM chats WHERE id=?',(c1,)).fetchone())
                await a.stop(r1)
                await a.delete_chat(c1)
                self.assertIn(r2,a.active)
                with self.assertRaises(a.HTTPException):await a.chat(c1)
                self.assertFalse(a.DB.execute('SELECT 1 FROM run_channels WHERE run=?',(r1,)).fetchone())
            finally:
                for rid in (r1,r2):
                    if rid in a.active:await a.stop(rid)

    async def test_submission_keeps_history_channel_and_global_busy_guard(self):
        cid = (await a.new_chat())['id']
        a.DB.execute('UPDATE chats SET history=? WHERE id=?', (json.dumps([{'role':'assistant','content':'Prior answer'}]),cid)); a.DB.commit()
        block = asyncio.Event()
        async def runner(*args): await block.wait()
        with patch.object(a, 'settings', return_value={'key':'fixture'}), \
             patch.object(a, 'run_agent', side_effect=runner) as run:
            rid = await a.whatsapp_submit(cid, 'Check health')
            try:
                await asyncio.sleep(0)
                self.assertEqual(a.DB.execute('SELECT channel FROM run_channels WHERE run=?',(rid,)).fetchone()[0], 'whatsapp')
                self.assertEqual(run.call_args.args[3], [{'role':'assistant','content':'Prior answer'}])
                with self.assertRaises(a.AdapterError): await a.whatsapp_submit(cid, 'Second task')
                with self.assertRaises(a.HTTPException): await a.send(cid, a.Message(text='Browser task'))
                run.assert_called_once()
            finally:
                await a.stop(rid)

    async def test_provider_ingress_runs_once_and_delivers_exact_result(self):
        from public_receiver.pi_receiver import Bridge, Receiver, HEADER_LIMIT
        from test_orchestration import FakeClient, result
        enabled_app = FastAPI()
        enabled_app.middleware('http')(a.security)
        enabled_app.add_api_route('/integrations/whatsapp/relay', a.whatsapp_relay, methods=['POST'])
        body = json.dumps(dict(event='message_received', instanceId=CONFIG.instance,
            data=dict(id=uuid.uuid4().hex, **{'from': '254700000001@s.whatsapp.net'},
                      message={'conversation': 'Fixture task'}))).encode()
        bridge = Bridge(CONFIG.instance, CONFIG.owners, '254700000099', CONFIG.relay_key.encode())
        receiver = Receiver(b'synthetic-token-only', bridge)
        with patch.object(a, 'WHATSAPP_ROUTE_ENABLED', True), \
             patch.object(a.whatsapp, 'config', return_value=CONFIG), \
             patch.object(a.whatsapp, 'POLL_SECONDS', .001), \
             patch.object(a.whatsapp, 'reply', new_callable=AsyncMock) as reply, \
             patch.object(a, 'settings', return_value={'key':'fixture','model':'fixture-model'}), \
             patch.object(a, 'AsyncOpenAI', FakeClient), \
             patch.object(a.Runner, 'run', new_callable=AsyncMock, return_value=result('Verified fixture answer')) as runner:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=enabled_app),base_url='http://fixture') as client:
                loop = asyncio.get_running_loop()
                def forward(normalized, headers):
                    response = asyncio.run_coroutine_threadsafe(client.post(
                        '/integrations/whatsapp/relay', content=normalized, headers=headers), loop).result(2)
                    return response.status_code, response.json()
                async def request(payload, token=b'synthetic-token-only'):
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                    writer.write(b'POST /webhooks/flaresend HTTP/1.1\r\nAuthorization: Bearer '+token+
                        b'\r\nContent-Type: application/json\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
                    await writer.drain()
                    raw = await asyncio.wait_for(reader.read(), 2)
                    writer.close(); await writer.wait_closed()
                    head, content = raw.split(b'\r\n\r\n', 1)
                    return SimpleNamespace(status_code=int(head.split()[1]), json=lambda:json.loads(content))
                server = await asyncio.start_server(receiver.handle, '127.0.0.1', 0, limit=HEADER_LIMIT)
                async def close_server():
                    server.close(); await server.wait_closed()
                self.addAsyncCleanup(close_server)
                port = server.sockets[0].getsockname()[1]
                bridge.forward = forward
                unauthorized = await request(body, b'wrong-token')
                self.assertEqual(unauthorized.status_code, 401)
                runner.assert_not_awaited()
                response = await request(body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['status'], 'accepted')
                rid = response.json()['run']
                await asyncio.wait_for(asyncio.gather(*list(a.whatsapp.monitors)), 1)
                duplicate = await request(body)
                self.assertEqual(duplicate.json()['status'], 'duplicate')
                runner.assert_awaited_once()
                self.assertEqual(runner.await_args.args[0].name, 'Piko')
                self.assertTrue(runner.await_args.args[0].instructions.startswith('You are Piko,'))
                reply.assert_any_await('254700000001','Piko: Verified fixture answer',CONFIG)
                self.assertEqual(reply.await_count,2)
                self.assertEqual(a.DB.execute('SELECT channel FROM run_channels WHERE run=?',(rid,)).fetchone()[0], 'whatsapp')
                self.assertIn('Every sudo command requires fresh approval', runner.await_args.args[0].instructions)
                self.assertNotIn('The user has authorized automatic execution', runner.await_args.args[0].instructions)

    async def test_outbound_optional_invalid_list_still_requires_confirmed_recipient(self):
        response = SimpleNamespace(status_code=200,json=lambda:dict(success=True,
            failed_recipients=[],successful_recipients=['254700000001@s.whatsapp.net']))
        client=AsyncMock();client.post.return_value=response;client.__aenter__.return_value=client
        with patch.object(a.httpx,'AsyncClient',return_value=client):
            await a.whatsapp_reply('254700000001','Fixture',CONFIG)
            for data in [dict(success=True,failed_recipients=[],successful_recipients=[]),
                         dict(success=True,failed_recipients=[],successful_recipients=['254700000001@s.whatsapp.net'],invalid_recipients=['254700000001'])]:
                response.json=lambda:data
                with patch('builtins.print'), self.assertRaises(RuntimeError):
                    await a.whatsapp_reply('254700000001','Fixture',CONFIG)

    async def test_admission_errors_explain_busy_without_dashboard_redirect(self):
        for status in [400,404,409,503]:
            with patch.object(a,'submit_task',new_callable=AsyncMock,side_effect=a.HTTPException(status,'private detail')):
                with self.assertRaises(a.AdapterError) as caught:
                    await a.whatsapp_submit('fixture','fixture task')
                self.assertEqual(caught.exception.status,status)
                self.assertNotIn('private detail',caught.exception.message)
                self.assertNotIn('dashboard',caught.exception.message)
                if status==409:
                    self.assertIn('already handling another task',caught.exception.message)
                    self.assertIn('send your request again',caught.exception.message)

    async def test_outbound_contract_and_provider_failures(self):
        response = SimpleNamespace(status_code=200, json=lambda:dict(success=True,
            failed_recipients=[], invalid_recipients=[], successful_recipients=['254700000001@s.whatsapp.net']))
        client = AsyncMock(); client.post.return_value = response
        client.__aenter__.return_value = client
        with patch.object(a.httpx, 'AsyncClient', return_value=client) as factory:
            await a.whatsapp_reply('254700000001', 'Fixture reply', CONFIG)
            factory.assert_called_once_with(timeout=15, follow_redirects=False, trust_env=False)
            self.assertEqual(client.post.call_args.args[0], 'https://api.flaresend.com/send-message')
            self.assertEqual(client.post.call_args.kwargs['json'], {'recipients':['254700000001'],'type':'text','text':'Fixture reply'})
            response.json = lambda:dict(success=True, failed_recipients=['254700000001'], invalid_recipients=[], successful_recipients=[])
            with patch('builtins.print') as diagnostic:
                with self.assertRaises(RuntimeError): await a.whatsapp_reply('254700000001', 'Fixture', CONFIG)
                self.assertNotIn('254700000001',str(diagnostic.call_args))
                self.assertNotIn(CONFIG.api_key,str(diagnostic.call_args))
            response.status_code = 302
            with patch('builtins.print') as diagnostic:
                with self.assertRaises(RuntimeError): await a.whatsapp_reply('254700000001', 'Fixture', CONFIG)
                self.assertNotIn('254700000001',str(diagnostic.call_args))
                self.assertNotIn(CONFIG.api_key,str(diagnostic.call_args))


class WhatsAppIngress(unittest.TestCase):
    def test_disabled_or_unsigned_ingress_and_dashboard_auth(self):
        self.assertFalse(a.WHATSAPP_ROUTE_ENABLED)
        self.assertNotIn('/integrations/whatsapp/relay', {route.path for route in a.app.routes})
        enabled_app = FastAPI()
        enabled_app.middleware('http')(a.security)
        enabled_app.add_api_route('/integrations/whatsapp/relay', a.whatsapp_relay, methods=['POST'])
        client = TestClient(enabled_app)
        with patch.object(a, 'WHATSAPP_ROUTE_ENABLED', True), patch.object(a.whatsapp, 'config', side_effect=a.AdapterError(503,'Disabled')):
            self.assertEqual(client.post('/integrations/whatsapp/relay', json={}).status_code,503)
        with patch.object(a, 'WHATSAPP_ROUTE_ENABLED', True), patch.object(a.whatsapp, 'config', return_value=CONFIG), patch.object(a.whatsapp, 'submit', new_callable=AsyncMock) as submit:
            self.assertEqual(client.post('/integrations/whatsapp/relay', json={}).status_code,401)
            self.assertEqual(client.post('/api/chats', json={}).status_code,403)
            self.assertEqual(client.post('/api/chats', json={}, headers={'X-Pi-Agent':'1'}).status_code,401)
            self.assertEqual(client.post('/api/approvals/fixture', json={'approved':True}, headers={'X-Pi-Agent':'1'}).status_code,401)
            submit.assert_not_awaited()

