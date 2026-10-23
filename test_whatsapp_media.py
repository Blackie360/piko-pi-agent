"""Local fixtures and mocked HTTP only; never send real media."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zlib

import httpx
import whatsapp_media as m
from whatsapp import WhatsAppAdapter
from test_whatsapp import CONFIG


def png():
    def chunk(kind, body):
        return struct.pack('>I', len(body))+kind+body+struct.pack('>I', zlib.crc32(kind+body)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', 1,1,8,2,0,0,0))
            +chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00'))+chunk(b'IEND', b''))


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.file = self.root/'sample.png'
        self.file.write_bytes(png())

    def test_images_and_documents_from_arbitrary_folders(self):
        self.assertEqual(m.snapshot(str(self.file),'image')[1:], ('image/png','image.png'))
        other = self.root/'another-folder'
        other.mkdir()
        doc=other/'report.txt'; doc.write_text('Hello, world!\n')
        self.assertEqual(m.snapshot(str(doc),'document')[0], b'Hello, world!\n')

    def test_sensitive_and_unsupported_paths(self):
        for name in ['.env.png','credentials.png','id_rsa.png','private.key','a.jpg','a.pdf']:
            path=self.root/name; path.write_bytes(png())
            with self.subTest(name=name), self.assertRaises(m.MediaRejected): m.snapshot(str(path),'image')
        for path in ['/proc/self/environ', '/dev/zero', '/etc/passwd', 'relative.png']:
            with self.subTest(path=path), self.assertRaises(m.MediaRejected): m.snapshot(path,'image')
        with self.assertRaises(m.MediaRejected): m.snapshot(str(self.file),'image', (str(self.root),))
        with self.assertRaises(m.MediaRejected): m.snapshot(str(self.file),'audio')

    def test_symlinks_hardlinks_and_fifo(self):
        link=self.root/'link.png'; link.symlink_to(self.file)
        directory=self.root/'alias'; directory.symlink_to(self.root, target_is_directory=True)
        fifo=self.root/'pipe.png'; os.mkfifo(fifo)
        for path in (link,directory/'sample.png',fifo):
            with self.assertRaises(m.MediaRejected): m.snapshot(str(path),'image')
        hard=self.root/'hard.png'; os.link(self.file,hard)
        with self.assertRaises(m.MediaRejected): m.snapshot(str(self.file),'image')

    def test_invalid_content_and_secrets(self):
        for data in (b'', b'not a PNG', png()+b'extra', png()[:-1], b'\x89PNG\r\n\x1a\n'):
            self.file.write_bytes(data)
            with self.assertRaises(m.MediaRejected): m.snapshot(str(self.file),'image')
        doc=self.root/'report.txt'
        for data in (b'API_KEY=abc', b'-----BEGIN PRIVATE KEY-----', b'hello\x00world', b'\xff', b'knowncredential'):
            doc.write_bytes(data)
            with self.assertRaises(m.MediaRejected): m.snapshot(str(doc),'document',secrets=('knowncredential',))
        with patch.object(m,'MAX_BYTES',3):
            with self.assertRaises(m.MediaRejected): m.snapshot(str(doc),'document')

    def test_replaced_file_during_open(self):
        real_open=os.open
        def raced(path, flags, *args, **kwargs):
            if path == self.file.name:
                self.file.unlink(); self.file.symlink_to('/dev/zero')
            return real_open(path,flags,*args,**kwargs)
        with patch.object(m.os,'open',side_effect=raced), self.assertRaises(m.MediaRejected):
            m.snapshot(str(self.file),'image')

    def test_mutation_after_read(self):
        real_fstat=os.fstat
        calls=0
        def raced(fd):
            nonlocal calls
            calls+=1
            if calls==2: self.file.write_bytes(b'changed')
            return real_fstat(fd)
        with patch.object(m.os,'fstat',side_effect=raced), self.assertRaises(m.MediaRejected):
            m.snapshot(str(self.file),'image')


class Delivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''CREATE TABLE runs(id TEXT,chat TEXT,status TEXT);
            CREATE TABLE run_channels(run TEXT,channel TEXT);''')
        self.adapter=WhatsAppAdapter(self.db,AsyncMock(),AsyncMock(),AsyncMock(),lambda:CONFIG)
        self.db.execute("INSERT INTO runs VALUES ('run','chat','running')")
        self.db.execute("INSERT INTO run_channels VALUES ('run','whatsapp')")
        self.sender=next(iter(CONFIG.owners))
        self.db.execute('INSERT INTO whatsapp_chats VALUES (?,?,?)',(CONFIG.instance,self.sender,'chat'))
        self.db.execute('INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,?,?)',
            (CONFIG.instance,'message',self.sender,'digest',0,'run','running'))
        self.db.commit()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.file=Path(self.tmp.name).resolve()/'image.png';self.file.write_bytes(png())

    async def send(self,caption=''):
        return await self.adapter.send_media('run',str(self.file),'image',caption)

    async def test_origin_and_dedup(self):
        with patch.object(m,'transmit',new_callable=AsyncMock,return_value='accepted_by_provider') as send:
            self.assertEqual(await self.send(),{'status':'accepted_by_provider'})
            self.assertTrue((await self.send('different caption'))['duplicate_suppressed'])
            send.assert_awaited_once()
            self.assertEqual(send.call_args.args[0],self.sender)
            self.db.execute("UPDATE whatsapp_inbox SET message='new-message'")
            self.assertTrue((await self.send())['duplicate_suppressed'])

    async def test_timeout_restart_and_concurrent_retry(self):
        started=asyncio.Event(); release=asyncio.Event()
        async def timeout(*args, **kwargs):
            started.set(); await release.wait(); raise httpx.ReadTimeout('SECRET provider error')
        with patch.object(m,'transmit',side_effect=timeout) as send:
            task=asyncio.create_task(self.send());await asyncio.wait_for(started.wait(), 2)
            self.assertEqual(await self.send(),{'status':'uncertain','duplicate_suppressed':True})
            release.set(); self.assertEqual(await task,{'status':'uncertain'})
            self.adapter=WhatsAppAdapter(self.db,AsyncMock(),AsyncMock(),AsyncMock(),lambda:CONFIG)
            self.db.execute("UPDATE whatsapp_inbox SET status='running'")
            self.assertTrue((await self.send())['duplicate_suppressed'])
            self.assertEqual(send.call_count,1)

    async def test_cancel_is_uncertain(self):
        with patch.object(m,'transmit',side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError): await self.send()
        self.assertEqual(self.db.execute('SELECT status FROM whatsapp_media_attempts').fetchone()[0],'uncertain')

    async def test_context_fail_closed(self):
        with patch.object(m,'transmit',new_callable=AsyncMock) as send:
            for sql in ["UPDATE run_channels SET channel='dashboard'", "UPDATE runs SET status='complete'",
                        "UPDATE whatsapp_chats SET chat='different'", "UPDATE whatsapp_inbox SET sender='123'"]:
                self.db.execute('SAVEPOINT context')
                self.db.execute(sql)
                self.assertEqual(await self.send(),{'status':'blocked'})
                self.db.execute('ROLLBACK TO context');self.db.execute('RELEASE context')
            self.adapter.closed_chats.add('chat')
            self.assertEqual(await self.send(),{'status':'blocked'})
            send.assert_not_awaited()

    async def test_revocation_while_reading(self):
        original=m.snapshot
        def changed(*args):
            self.adapter.closed_chats.add('chat')
            return original(*args)
        with patch.object(m,'snapshot',side_effect=changed),patch.object(m,'transmit',new_callable=AsyncMock) as send:
            self.assertEqual(await self.send(),{'status':'blocked'});send.assert_not_awaited()

    async def test_caption_secrets_blocked(self):
        self.assertEqual(await self.send(CONFIG.api_key),{'status':'blocked'})
        self.assertEqual(await self.send('token=abc'),{'status':'blocked'})


class Provider(unittest.IsolatedAsyncioTestCase):
    async def test_multipart_and_sanitized_results(self):
        recipient=next(iter(CONFIG.owners));payload=(png(),'image/png','image.png')
        responses=[httpx.Response(200,json={'success':True,'failed_recipients':[],
            'successful_recipients':[recipient+'@s.whatsapp.net']}),
            httpx.Response(403,text='SECRET'),httpx.Response(500,text='SECRET'),
            httpx.Response(200,text='bad json'),httpx.Response(200,json={'success':True})]
        for response, expected in zip(responses,['accepted_by_provider','provider_error','uncertain','uncertain','uncertain']):
            client=AsyncMock();client.post.return_value=response
            client.__aenter__.return_value=client
            with patch.object(m.httpx,'AsyncClient',return_value=client) as factory:
                self.assertEqual(await m.transmit(recipient,'image','hello',payload,CONFIG),expected)
                factory.assert_called_once_with(timeout=30,follow_redirects=False,trust_env=False)
                args=client.post.call_args
                self.assertEqual(args.args,('https://api.flaresend.com/send-message',))
                self.assertEqual(args.kwargs['data'],{'recipients[]':recipient,'type':'image','caption':'hello'})
                self.assertEqual(args.kwargs['files']['file'],('image.png',png(),'image/png'))


class ToolExposure(unittest.IsolatedAsyncioTestCase):
    async def test_tool_only_on_whatsapp_coordinator(self):
        import test_app
        import app as a
        import uuid
        from agents.tool_context import ToolContext
        from test_orchestration import FakeClient, result
        for channel in ('dashboard', 'whatsapp'):
            cid, rid = uuid.uuid4().hex, uuid.uuid4().hex
            a.DB.execute('INSERT INTO chats(id,title,created) VALUES (?,?,?)',(cid,'fixture',0))
            a.DB.execute("INSERT INTO runs VALUES (?,?,'running')",(rid,cid))
            a.DB.execute('INSERT INTO run_channels VALUES (?,?)',(rid,channel));a.DB.commit()
            with patch.object(a,'settings',return_value={'key':'fixture','model':'fixture-model'}), patch.object(a,'AsyncOpenAI',FakeClient), patch.object(a.Runner,'run',new_callable=AsyncMock,return_value=result('fixture')) as runner:
                await a.run_agent(cid,rid,'fixture',[])
                names={tool.name for tool in runner.call_args.args[0].tools}
                self.assertEqual('send_whatsapp_media' in names, channel == 'whatsapp')
        self.assertEqual(set(a.send_whatsapp_media.params_json_schema['properties']), {'file_path','media_type','caption'})
        with patch.object(a.whatsapp,'send_media',new_callable=AsyncMock) as send:
            output=await a.send_whatsapp_media.on_invoke_tool(ToolContext(context=a.Context('run','worker'),tool_name='send_whatsapp_media',tool_call_id='fixture',tool_arguments='{}'), json.dumps({'file_path':'/fixture.png','media_type':'image','caption':''}))
            self.assertEqual(output, {'status':'blocked'})
            send.assert_not_awaited()


class LinuxSources(unittest.TestCase):
    def test_filesystem_and_credential_bind_mount_checks(self):
        import ctypes
        from types import SimpleNamespace
        def fs(magic):
            def fstatfs(fd, buf):
                ctypes.c_long.from_buffer(buf).value=magic
                return 0
            return SimpleNamespace(fstatfs=fstatfs)
        with patch.object(m.sys,'platform','linux'):
            with patch.object(m.ctypes,'CDLL',return_value=fs(0x9fa0)):
                with self.assertRaises(m.MediaRejected): m.check_filesystem(0,'/home/user/image.png',())
            with patch.object(m.ctypes,'CDLL',return_value=fs(0xef53)):
                for line in ('1 0 8:1 /run/credentials/service /home/user rw - ext4 /dev/sda rw',
                             '1 0 8:1 /etc /home/user rw - ext4 /dev/sda rw'):
                    with patch.object(m.Path,'read_text',return_value=line), self.assertRaises(m.MediaRejected):
                        m.check_filesystem(0,'/home/user/image.png',())
                with patch.object(m.Path,'read_text',return_value='1 0 8:1 / / rw - ext4 /dev/sda rw'):
                    m.check_filesystem(0,'/home/user/image.png',())


def precision_png(color=2, precision=b'\x08\x08\x08', placement='before'):
    def chunk(kind, body):
        return struct.pack('>I',len(body))+kind+body+struct.pack('>I',zlib.crc32(kind+body)&0xffffffff)
    ihdr=chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,color,0,0,0))
    sbit=chunk(b'sBIT',precision)
    image=chunk(b'IDAT',zlib.compress(b'\0'+b'\xff'*(3 if color==2 else 4)))
    sequences={'before':ihdr+sbit+image,'duplicate':ihdr+sbit+sbit+image,
               'after':ihdr+image+sbit,'first':sbit+ihdr+image,
               'between':ihdr+image+sbit+image,
               'unknown':ihdr+chunk(b'tEXt',b'fixture')+image,
               'bad_crc':ihdr+sbit[:-1]+bytes([sbit[-1]^1])+image}
    return b'\x89PNG\r\n\x1a\n'+sequences[placement]+chunk(b'IEND',b'')


class PrecisionMetadata(unittest.TestCase):
    def test_rgb_rgba_and_precision_boundaries(self):
        for color,count in ((2,3),(6,4)):
            for values in (bytes([1])*count,bytes([8])*count,bytes(range(1,count+1))):
                with self.subTest(color=color,values=values):m.png_valid(precision_png(color,values))

    def test_reject_invalid_precision_lengths_values_and_order(self):
        for color,count in ((2,3),(6,4)):
            for values in (b'',bytes([8])*(count-1),bytes([8])*(count+1),
                           bytes([0])+bytes([8])*(count-1),bytes([9])*count,bytes([255])*count):
                with self.subTest(color=color,values=values),self.assertRaises(m.MediaRejected) as error:
                    m.png_valid(precision_png(color,values))
                self.assertEqual(error.exception.reason,'invalid_png_sbit')
        for placement in ('duplicate','after','first','between'):
            with self.subTest(placement=placement),self.assertRaises(m.MediaRejected) as error:
                m.png_valid(precision_png(placement=placement))
            self.assertEqual(error.exception.reason,'invalid_png_sbit')

    def test_crc_and_other_metadata_still_blocked(self):
        for placement,reason in (('unknown','unsupported_png_chunk'),('bad_crc','invalid_png')):
            with self.assertRaises(m.MediaRejected) as error:m.png_valid(precision_png(placement=placement))
            self.assertEqual(error.exception.reason,reason)
        self.assertEqual(m.MediaRejected('/private/credential-value').reason,'invalid_file')


class PrecisionDelivery(Delivery):
    async def test_valid_sbit_uses_existing_owner_binding_and_dedup(self):
        self.file.write_bytes(precision_png())
        with patch.object(m,'transmit',new_callable=AsyncMock,return_value='accepted_by_provider') as send:
            self.assertEqual(await self.send(),{'status':'accepted_by_provider'})
            self.assertTrue((await self.send())['duplicate_suppressed'])
            send.assert_awaited_once();self.assertEqual(send.call_args.args[0],self.sender)

    async def test_invalid_sbit_is_local_and_never_reserved_or_sent(self):
        self.file.write_bytes(precision_png(precision=b'\0\x08\x08'))
        with patch.object(m,'transmit',new_callable=AsyncMock) as send:
            self.assertEqual(await self.send(),{'status':'blocked','source':'local_validation','reason':'invalid_png_sbit'})
            send.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM whatsapp_media_attempts').fetchone()[0],0)

    async def test_untrusted_exception_text_never_returned(self):
        with patch.object(m,'snapshot',side_effect=m.MediaRejected('PRIVATE-CONTENT')):
            self.assertEqual(await self.send(),{'status':'blocked','source':'local_validation','reason':'invalid_file'})


class TransportDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_categories_and_no_private_response_content(self):
        recipient=next(iter(CONFIG.owners))
        cases=[
            (httpx.ReadTimeout('PRIVATE'), 'uncertain','timeout',None),
            (httpx.ConnectError('PRIVATE'), 'uncertain','network_error',None),
            (RuntimeError('PRIVATE'), 'uncertain','transport_error',None),
            (httpx.Response(403,text='PRIVATE'), 'provider_error','provider_rejected',403),
            (httpx.Response(500,text='PRIVATE'), 'uncertain','http_unconfirmed',500),
            (httpx.Response(302,headers={'Location':'https://private.invalid'}), 'uncertain','http_unconfirmed',302),
            (httpx.Response(200,text='PRIVATE'), 'uncertain','malformed_response',200),
            (httpx.Response(200,json={'success':False,'message':'PRIVATE'}), 'uncertain','acceptance_unconfirmed',200),
            (httpx.Response(200,json={'success':True,'failed_recipients':[],
              'successful_recipients':[recipient+'@s.whatsapp.net']}), 'accepted_by_provider','accepted',200)]
        for response,outcome,category,code in cases:
            with self.subTest(category=category,code=code):
                client=AsyncMock();client.__aenter__.return_value=client
                if isinstance(response,Exception):client.post.side_effect=response
                else:client.post.return_value=response
                diagnostic={}
                with patch.object(m.httpx,'AsyncClient',return_value=client):
                    result=await m.transmit(recipient,'image','',(png(),'image/png','image.png'),CONFIG,diagnostic)
                self.assertEqual(result,outcome)
                self.assertEqual(diagnostic,{'category':category,'http_status':code})
                client.post.assert_awaited_once()
                self.assertNotIn('PRIVATE',json.dumps(diagnostic))
                self.assertNotIn(recipient,json.dumps(diagnostic))

    def test_diagnostic_allowlist(self):
        self.assertEqual(m.safe_transport_diagnostic({'category':'PRIVATE','http_status':'PRIVATE'}),('transport_error',None))
        for code in (True,99,600):
            self.assertEqual(m.safe_transport_diagnostic({'category':'timeout','http_status':code}),('timeout',None))


class PersistedDiagnostics(Delivery):
    async def test_diagnostic_persists_and_duplicate_does_not_add_attempt(self):
        async def transport(*args,diagnostics):
            diagnostics.update(category='timeout',http_status=None)
            return 'uncertain'
        with patch.object(m,'transmit',side_effect=transport) as send:
            self.assertEqual(await self.send(),{'status':'uncertain'})
            self.assertEqual(await self.send(),{'status':'uncertain','duplicate_suppressed':True})
            send.assert_awaited_once()
        rows=self.db.execute('SELECT run,category,http_status,outcome FROM whatsapp_media_diagnostics').fetchall()
        self.assertEqual([tuple(row) for row in rows],[('run','timeout',None,'uncertain')])
        self.assertNotIn(self.sender,str([tuple(row) for row in rows]))
        WhatsAppAdapter(self.db,AsyncMock(),AsyncMock(),AsyncMock(),lambda:CONFIG)
        self.assertEqual(self.db.execute('SELECT outcome FROM whatsapp_media_diagnostics').fetchone()[0],'uncertain')

    async def test_cancel_diagnostic_does_not_change_uncertain_status(self):
        with patch.object(m,'transmit',side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):await self.send()
        row=self.db.execute('SELECT category,outcome FROM whatsapp_media_diagnostics').fetchone()
        self.assertEqual(tuple(row),('cancelled','uncertain'))

    async def test_sensitive_diagnostic_is_filtered_before_storage(self):
        async def transport(*args,diagnostics):
            diagnostics.update(category='PRIVATE',http_status='PRIVATE')
            return 'uncertain'
        with patch.object(m,'transmit',side_effect=transport):await self.send()
        self.assertEqual(tuple(self.db.execute('SELECT category,http_status FROM whatsapp_media_diagnostics').fetchone()),('transport_error',None))

