"""Offline, stdlib-only tests; synthetic credentials and no network or shell tools."""
import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import AsyncMock, patch
from whatsapp import AdapterError, Config, WhatsAppAdapter, load_config

CONFIG = Config('test-instance', frozenset({'254700000001', '254700000002'}), 'x'*32, 'y'*32)


class ConfigTests(unittest.TestCase):
    def test_disabled_and_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AdapterError): load_config()
        with patch.dict(os.environ, {'PI_WHATSAPP_ENABLED': '1'}, clear=True):
            with self.assertRaises(AdapterError): load_config()
        with patch.dict(os.environ, {'PI_WHATSAPP_ENABLED': '1', 'PI_WHATSAPP_VERIFIED_RELAY': '1'}, clear=True):
            with self.assertRaises(AdapterError): load_config()

    def test_valid_and_placeholder_config(self):
        env = {'PI_WHATSAPP_ENABLED':'1','PI_WHATSAPP_VERIFIED_RELAY':'1',
               'PI_WHATSAPP_INSTANCE':'test-instance','PI_WHATSAPP_OWNERS':'254700000001',
               'PI_WHATSAPP_RELAY_KEY':'x'*32,'PI_WHATSAPP_API_KEY':'y'*32}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_config().owners, frozenset({'254700000001'}))
            self.assertNotIn('x'*32, repr(load_config()))
            os.environ['PI_WHATSAPP_RELAY_KEY']='<REPLACE_WITH_A_RANDOM_32_BYTE_SECRET>'
            with self.assertRaises(AdapterError): load_config()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE chats(id TEXT PRIMARY KEY,title TEXT,created REAL,history TEXT DEFAULT '[]');
            CREATE TABLE runs(id TEXT PRIMARY KEY,chat TEXT,status TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,chat TEXT,role TEXT,text TEXT);
            CREATE TABLE run_messages(run TEXT PRIMARY KEY,message INTEGER);
        ''')
        self.count = 0
        async def submit(cid, text):
            self.count += 1
            rid = str(self.count)
            self.db.execute('INSERT INTO runs VALUES (?,?,?)', (rid, cid, 'running')); self.db.commit()
            return rid
        async def stop(rid):
            self.db.execute("UPDATE runs SET status='cancelled' WHERE id=?", (rid,)); self.db.commit()
        self.submit = AsyncMock(side_effect=submit)
        self.stop = AsyncMock(side_effect=stop)
        self.reply = AsyncMock()
        self.adapter = WhatsAppAdapter(self.db, self.submit, self.stop, self.reply, lambda: CONFIG)
        self.adapter.POLL_SECONDS = .001
        self.adapter.BATCH_SECONDS = .001

    async def asyncTearDown(self):
        for task in list(self.adapter.monitors): task.cancel()
        await asyncio.gather(*self.adapter.monitors, return_exceptions=True)
        self.db.close()

    def envelope(self, **changes):
        data = dict(version=1,event='message_received',instance=CONFIG.instance,message_id='message-1',
                    sender='254700000001',text='Check health',from_self=False,is_group=False)
        data.update(changes)
        return json.dumps(data).encode()

    def headers(self, body, timestamp=None):
        timestamp = str(int(time.time()) if timestamp is None else timestamp)
        return {'x-pi-relay-timestamp':timestamp, 'x-pi-relay-signature':
                hmac.new(CONFIG.relay_key.encode(), timestamp.encode()+b'.'+body, hashlib.sha256).hexdigest()}

    async def accept(self, **changes):
        body = self.envelope(**changes)
        return await self.adapter.accept(body, self.headers(body))

    async def error(self, status, body, headers):
        with self.assertRaises(AdapterError) as caught:
            await self.adapter.accept(body, headers)
        self.assertEqual(caught.exception.status, status)

    def finish(self, rid, answer='Verified fixture result'):
        row = self.db.execute('SELECT chat FROM runs WHERE id=?', (rid,)).fetchone()
        m = self.db.execute("INSERT INTO messages(chat,role,text) VALUES (?,'assistant',?)", (row['chat'], answer))
        self.db.execute('INSERT INTO run_messages VALUES (?,?)', (rid, m.lastrowid))
        self.db.execute("UPDATE runs SET status='complete' WHERE id=?", (rid,)); self.db.commit()

    async def settle(self):
        await asyncio.wait_for(asyncio.gather(*list(self.adapter.monitors)), 1)

    async def test_unsigned_tampered_expired_and_unicode_signatures_rejected(self):
        body = self.envelope()
        await self.error(401, body, {})
        await self.error(401, body+b' ', self.headers(body))
        await self.error(401, body, self.headers(body, int(time.time())-61))
        await self.error(401, body, {**self.headers(body), 'x-pi-relay-signature':'é'*64})
        self.submit.assert_not_called()

    async def test_payload_validation_and_size(self):
        for body in (b'{}', b'[]', b'not-json', b'{"version":1,"version":2}', self.envelope(from_self='false'),
                     self.envelope(text=' '), self.envelope(text='x'*8001),
                     self.envelope(version=True), self.envelope(extra='no')):
            await self.error(400, body, self.headers(body))
        body = b'x'*16385
        await self.error(413, body, self.headers(body))
        self.submit.assert_not_called()

    async def test_untrusted_sender_and_instance_rejected(self):
        for body in (self.envelope(sender='254700000003'), self.envelope(instance='other')):
            await self.error(403, body, self.headers(body))
        self.submit.assert_not_called()

    async def test_self_groups_and_nonmessage_events_ignored(self):
        for change in ({'from_self':True}, {'is_group':True}, {'event':'message_sent'}):
            self.assertEqual(await self.accept(**change), {'status':'ignored'})
        self.submit.assert_not_called(); self.reply.assert_not_called()

    async def test_duplicate_and_collision_do_not_run_twice(self):
        result = await self.accept()
        self.assertEqual((await self.accept())['status'], 'duplicate')
        body = self.envelope(text='A different command')
        await self.error(409, body, self.headers(body))
        self.submit.assert_awaited_once()
        self.finish(result['run']); await self.settle()
        self.assertEqual((await self.accept())['status'], 'duplicate')
        self.assertEqual(self.reply.await_count, 2)

    async def test_concurrent_duplicate_admitted_once(self):
        results = await asyncio.gather(self.accept(), self.accept())
        self.assertEqual({x['status'] for x in results}, {'accepted','duplicate'})
        self.submit.assert_awaited_once()

    async def test_sender_sessions_isolated_and_reused(self):
        first = await self.accept()
        second = await self.accept(sender='254700000002', message_id='message-2')
        calls = self.submit.await_args_list
        self.assertNotEqual(calls[0].args[0], calls[1].args[0])
        self.finish(first['run']); self.finish(second['run']); await self.settle()
        self.adapter.COOLDOWN_SECONDS = 0
        await self.accept(message_id='message-3')
        self.assertEqual(calls[0].args[0], self.submit.await_args.args[0])

    async def test_busy_followup_does_not_create_task(self):
        await self.accept()
        self.assertEqual((await self.accept(message_id='message-2'))['status'],'handled')
        self.submit.assert_awaited_once()

    async def test_busy_request_consumed_without_delayed_execution(self):
        self.submit.side_effect = AdapterError(409, 'Task unavailable')
        self.assertEqual((await self.accept())['status'], 'rejected')
        self.assertEqual((await self.accept())['status'], 'duplicate')
        self.submit.assert_awaited_once()
        await self.settle(); self.assertEqual(self.reply.await_count, 2)

    async def test_uncertain_admission_never_replayed(self):
        self.submit.side_effect = RuntimeError('sensitive detail')
        body = self.envelope()
        await self.error(503, body, self.headers(body))
        self.assertEqual((await self.accept())['status'], 'duplicate')
        self.submit.assert_awaited_once()

    async def test_reply_bound_to_run_not_latest_chat_message(self):
        result = await self.accept(); self.finish(result['run'], 'Correct answer')
        cid = self.submit.await_args.args[0]
        self.db.execute("INSERT INTO messages(chat,role,text) VALUES (?,'assistant','Later unrelated answer')", (cid,))
        self.db.commit(); await self.settle()
        self.assertEqual(self.reply.await_args.args[1], 'Piko: Correct answer')

    async def test_approval_notice_once_and_deadline_stops_task(self):
        self.adapter.MAX_TASK_SECONDS = .01
        result = await self.accept()
        self.db.execute("UPDATE runs SET status='waiting' WHERE id=?", (result['run'],)); self.db.commit()
        self.adapter.MAX_TASK_SECONDS = .01
        await self.settle()
        self.stop.assert_awaited_once_with(result['run'])
        self.assertEqual(self.reply.await_count, 3)
        self.assertIn('cannot approve', self.reply.await_args_list[1].args[1])

    async def test_delivery_failure_is_uncertain_without_retry(self):
        self.reply.side_effect = TimeoutError('secret exception')
        result = await self.accept(); self.finish(result['run']); await self.settle()
        await self.adapter.deliver(CONFIG, 'message-1', '254700000001', 'final', 'retry')
        self.assertEqual(self.reply.await_count, 2)
        self.assertEqual(self.db.execute("SELECT status FROM whatsapp_deliveries WHERE kind='final'").fetchone()[0], 'uncertain')

    async def test_disabled_during_run_blocks_reply(self):
        result = await self.accept(); self.finish(result['run'])
        self.adapter.config = lambda: (_ for _ in ()).throw(AdapterError(503, 'Disabled'))
        await self.settle(); self.reply.assert_awaited_once()

    async def test_long_reply_is_bounded(self):
        result = await self.accept(); self.finish(result['run'], 'x'*9000); await self.settle()
        self.assertLessEqual(len(self.reply.await_args.args[1]), self.adapter.MAX_REPLY)
        self.assertIn('Full details', self.reply.await_args.args[1])

    async def test_restart_preserves_dedupe_and_marks_interrupted(self):
        await self.accept()
        for task in list(self.adapter.monitors): task.cancel()
        await asyncio.gather(*self.adapter.monitors, return_exceptions=True)
        self.db.execute("INSERT INTO whatsapp_deliveries VALUES (?,?,?,'sending')", (CONFIG.instance,'message-1','final'))
        self.db.commit()
        restarted = WhatsAppAdapter(self.db, self.submit, self.stop, self.reply, lambda: CONFIG)
        body = self.envelope()
        self.assertEqual((await restarted.accept(body, self.headers(body)))['status'], 'duplicate')
        self.assertEqual(self.db.execute('SELECT status FROM whatsapp_inbox').fetchone()[0], 'interrupted')
        self.assertEqual(self.db.execute("SELECT status FROM whatsapp_deliveries WHERE kind='final'").fetchone()[0], 'uncertain')
        self.submit.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()

