"""Owner-bound command approval tests; no real messages or shell execution."""
import asyncio
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, patch
import test_whatsapp as fixtures
from test_whatsapp import CONFIG
from whatsapp import AdapterError


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.AdapterTests.asyncSetUp(self)
        self.adapter.decide = AsyncMock()
        self.mode = patch.dict(os.environ,{'PI_WHATSAPP_COMMAND_APPROVALS':'1'})
        self.mode.start()
        self.db.execute("INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,?,?)",
            (CONFIG.instance,'origin','254700000001','fixture',time.time(),'run-1','running'))
        self.db.commit()

    async def asyncTearDown(self):
        self.mode.stop()
        await fixtures.AdapterTests.asyncTearDown(self)

    async def decision(self,text,mid='decision-1',sender='254700000001',**changes):
        body = fixtures.AdapterTests.envelope(self,text=text,message_id=mid,sender=sender,**changes)
        if 'sent_at' in changes:
            # Model delivery after the provider's whole-second timestamp.
            with patch('whatsapp.time.time',return_value=time.time()+2):
                return await self.adapter.accept(body,fixtures.AdapterTests.headers(self,body))
        return await self.adapter.accept(body,fixtures.AdapterTests.headers(self,body))

    async def propose(self,aid='approval-1',command='fixture command'):
        ready = await self.adapter.request_command('run-1',aid,command,'Fixture purpose','/tmp')
        self.assertTrue(ready)
        return next(token for token,pending in self.adapter.pending_commands.items() if pending['aid']==aid)

    async def test_session_grant_exit_new_chat_and_sudo_isolation(self):
        cid = 'session-fixture'
        self.db.execute('INSERT INTO chats(id,title,created) VALUES (?,?,?)',(cid,'fixture',time.time()))
        self.db.execute('INSERT INTO runs VALUES (?,?,?)',('run-1',cid,'running'))
        self.db.execute('INSERT INTO whatsapp_chats VALUES (?,?,?)',(CONFIG.instance,'254700000001',cid))
        self.db.commit()
        token = await self.propose()
        self.assertFalse(self.adapter.session_authorized('run-1'))
        await self.decision('approve '+token)
        self.assertTrue(self.adapter.session_authorized('run-1'))
        # Exit is authenticated and deduplicated; only its conversation is stopped.
        self.db.execute('INSERT INTO runs VALUES (?,?,?)',('dashboard','other','running'));self.db.commit()
        self.assertEqual((await self.decision('EXIT',mid='exit'))['status'],'accepted')
        self.stop.assert_awaited_once_with('run-1')
        self.assertFalse(self.adapter.session_authorized('run-1'))
        self.assertIsNone(self.adapter.current_chat(CONFIG,'254700000001'))
        self.assertEqual((await self.decision('EXIT',mid='exit'))['status'],'duplicate')
        self.adapter.COOLDOWN_SECONDS=0
        body=fixtures.AdapterTests.envelope(self,text='Next task',message_id='new-chat')
        # Fixture submit counter must not collide with the manually created run.
        self.count=10
        result=await self.adapter.accept(body,fixtures.AdapterTests.headers(self,body))
        newcid=self.adapter.current_chat(CONFIG,'254700000001')
        self.assertNotEqual(newcid,cid)
        self.assertFalse(self.adapter.session_authorized(result['run']))
        self.assertTrue(any('exit word is EXIT' in call.args[1] for call in self.reply.await_args_list))
        ready=await self.adapter.request_command(result['run'],'sudo','sudo -n fixture','purpose','/tmp',sudo=True)
        self.assertTrue(ready)
        token=next(t for t,v in self.adapter.pending_commands.items() if v['aid']=='sudo')
        await self.decision('approve '+token,mid='sudo-decision')
        self.assertFalse(self.adapter.session_authorized(result['run']))

    async def test_exact_owner_code_and_duplicate_execute_once(self):
        token = await self.propose()
        self.assertIn('Command:\nfixture command',self.reply.await_args.args[1])
        self.assertIn('APPROVE '+token,self.reply.await_args.args[1])
        self.assertEqual((await self.decision('APPROVE '+token,sender='254700000002'))['status'],'rejected')
        self.adapter.decide.assert_not_awaited()
        result = await self.decision('APPROVE '+token,mid='owner-approval')
        self.assertEqual(result['status'],'accepted')
        self.adapter.decide.assert_awaited_once_with('run-1','approval-1',True)
        self.assertEqual((await self.decision('APPROVE '+token,mid='owner-approval'))['status'],'duplicate')
        self.assertEqual((await self.decision('APPROVE '+token,mid='replay'))['status'],'rejected')
        self.submit.assert_not_awaited()

    async def test_deny_and_bare_or_embedded_approval(self):
        token = await self.propose()
        for i,text in enumerate(['approve','approve '+token+' please','approve wrong-code']):
            self.assertEqual((await self.decision(text,mid='bad-'+str(i)))['status'],'rejected')
        self.adapter.decide.assert_not_awaited()
        self.assertEqual((await self.decision('DENY '+token))['status'],'accepted')
        self.adapter.decide.assert_awaited_once_with('run-1','approval-1',False)
        self.submit.assert_not_awaited()

    async def test_expired_cancelled_restarted_and_disabled_codes_fail_closed(self):
        token = await self.propose()
        self.adapter.pending_commands[token]['deadline'] = time.time()-1
        self.assertEqual((await self.decision('approve '+token))['status'],'rejected')
        self.adapter.close_command('approval-1')
        self.assertFalse(self.adapter.pending_commands)
        token = await self.propose('approval-2')
        with patch.dict(os.environ,{'PI_WHATSAPP_COMMAND_APPROVALS':'0'}):
            self.assertEqual((await self.decision('approve '+token,mid='disabled'))['status'],'rejected')
        self.adapter.pending_commands.clear()  # A fresh process never restores pending codes.
        self.assertEqual((await self.decision('approve '+token,mid='after-restart'))['status'],'rejected')
        self.adapter.decide.assert_not_awaited()

    async def test_group_self_and_unknown_sender_cannot_approve(self):
        token = await self.propose()
        for flag in ['from_self','is_group']:
            self.assertEqual((await self.decision('approve '+token,mid=flag,**{flag:True}))['status'],'ignored')
        with self.assertRaises(AdapterError):
            await self.decision('approve '+token,sender='254700000099')
        self.adapter.decide.assert_not_awaited()

    async def test_failed_delivery_and_long_preview_never_enable_code(self):
        self.reply.side_effect = RuntimeError('synthetic delivery failure')
        self.assertFalse(await self.adapter.request_command('run-1','failed','fixture','reason','/tmp'))
        self.assertFalse(self.adapter.pending_commands)
        self.reply.side_effect = None
        self.assertFalse(await self.adapter.request_command('run-1','long','x'*3500,'reason','/tmp'))
        self.assertFalse(self.adapter.pending_commands)
        self.adapter.decide.assert_not_awaited()

    async def test_stale_gate_failure_is_consumed_without_retry(self):
        token = await self.propose()
        self.adapter.decide.side_effect = AdapterError(409,'stale gate')
        self.assertEqual((await self.decision('approve '+token))['status'],'rejected')
        self.assertEqual((await self.decision('approve '+token,mid='retry'))['status'],'rejected')
        self.adapter.decide.assert_awaited_once()


    async def test_plain_approve_and_deny_are_case_insensitive(self):
        for index,word in enumerate(['ApPrOvE','DeNy']):
            aid='plain-'+str(index)
            await self.propose(aid)
            result = await self.decision('  '+word+'  ',mid=aid,sent_at=int(time.time())+1)
            self.assertEqual(result['status'],'accepted')
            self.adapter.decide.assert_awaited_with('run-1',aid,index==0)
        self.submit.assert_not_awaited()

    async def test_plain_replay_cannot_authorize_next_command(self):
        await self.propose()
        stamp=int(time.time())+1
        self.assertEqual((await self.decision('approve',sent_at=stamp))['status'],'accepted')
        next_token=await self.propose('next-command')
        self.assertEqual((await self.decision('approve',sent_at=stamp))['status'],'duplicate')
        self.adapter.decide.assert_awaited_once()
        self.assertIn(next_token,self.adapter.pending_commands)
        # A distinct delayed message from before the next proposal is also rejected.
        self.assertEqual((await self.decision('approve',mid='delayed',sent_at=int(time.time())-30))['status'],'rejected')
        self.adapter.decide.assert_awaited_once()

    async def test_plain_ambiguity_other_chat_and_stale_messages_rejected(self):
        first=await self.propose()
        second=await self.propose('second-command')
        self.assertEqual((await self.decision('approve',sent_at=int(time.time())+1))['status'],'rejected')
        self.adapter.close_command('second-command')
        for mid,changes in [('other-owner',{'sender':'254700000002','sent_at':int(time.time())+1}),
                            ('old',{'sent_at':int(time.time())-60}),('missing',{}),
                            ('future',{'sent_at':int(time.time())+60})]:
            self.assertEqual((await self.decision('approve',mid=mid,**changes))['status'],'rejected')
        self.adapter.pending_commands[first]['chat']=('other-instance','254700000001')
        self.assertEqual((await self.decision('approve',mid='other-chat',sent_at=int(time.time())+1))['status'],'rejected')
        self.adapter.decide.assert_not_awaited()

    async def test_plain_before_any_proposal_cannot_approve_future_command(self):
        stamp=int(time.time())+1
        self.assertEqual((await self.decision('approve',sent_at=stamp))['status'],'rejected')
        await self.propose()
        self.assertEqual((await self.decision('approve',sent_at=stamp))['status'],'duplicate')
        self.adapter.decide.assert_not_awaited()

