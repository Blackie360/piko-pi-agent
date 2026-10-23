import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
import test_whatsapp as fixtures
from test_whatsapp import CONFIG


class Conversation(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.AdapterTests.asyncSetUp
    asyncTearDown=fixtures.AdapterTests.asyncTearDown
    envelope=fixtures.AdapterTests.envelope
    headers=fixtures.AdapterTests.headers
    accept=fixtures.AdapterTests.accept
    finish=fixtures.AdapterTests.finish
    settle=fixtures.AdapterTests.settle

    async def test_burst_keeps_all_ids_and_one_run(self):
        self.adapter.BATCH_SECONDS=.02
        first=asyncio.create_task(self.accept(text='Check disk'))
        await asyncio.sleep(.001)
        second=asyncio.create_task(self.accept(message_id='fragment-2',text='and memory'))
        results=await asyncio.gather(first,second)
        self.assertEqual(results[0]['run'],results[1]['run'])
        self.assertEqual(self.submit.call_args.args[1],'Check disk\nand memory')
        for mid in ('message-1','fragment-2'):
            text='Check disk' if mid=='message-1' else 'and memory'
            self.assertEqual((await self.accept(message_id=mid,text=text))['status'],'duplicate')
        self.submit.assert_awaited_once()
        self.finish(results[0]['run']);await self.settle()
        self.assertEqual({r[0] for r in self.db.execute('SELECT status FROM whatsapp_inbox')},{'complete'})

    async def test_exit_during_buffer_never_submits(self):
        self.adapter.BATCH_SECONDS=.02
        first=asyncio.create_task(self.accept())
        await asyncio.sleep(.001)
        self.assertEqual((await self.accept(message_id='exit-now',text='EXIT'))['status'],'accepted')
        self.assertEqual((await first)['status'],'ignored')
        self.submit.assert_not_awaited()
        self.assertEqual(self.db.execute("SELECT status FROM whatsapp_inbox WHERE message='message-1'").fetchone()[0],'exited')

    async def test_approval_and_denial_bypass_buffer(self):
        self.adapter.BATCH_SECONDS=.02
        task=asyncio.create_task(self.accept())
        await asyncio.sleep(.001)
        with patch.object(self.adapter,'command_decision',new_callable=AsyncMock,return_value={'status':'decision'}) as decision:
            for word in ('approve','deny'):
                self.assertEqual((await self.accept(message_id=word,text=word))['status'],'decision')
            self.assertEqual(decision.await_count,2)
            self.assertFalse(task.done())
        await task

    async def test_status_and_correction_are_truthful_and_deduped(self):
        result=await self.accept()
        self.assertEqual((await self.accept(message_id='status',text="how’s it going?"))['status'],'handled')
        self.assertIn('still working',self.reply.call_args.args[1])
        self.assertEqual((await self.accept(message_id='change',text='Actually only check disk'))['status'],'handled')
        self.assertIn("haven't applied",self.reply.call_args.args[1])
        self.submit.assert_awaited_once()
        count=self.reply.await_count
        self.assertEqual((await self.accept(message_id='change',text='Actually only check disk'))['status'],'duplicate')
        self.assertEqual(self.reply.await_count,count)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM whatsapp_notes').fetchone()[0],2)
        self.db.execute("UPDATE runs SET status='waiting'");self.db.commit()
        await self.accept(message_id='waiting',text='status')
        self.assertIn('waiting for your command approval',self.reply.call_args.args[1])

    async def test_sparse_progress_and_failed_send_not_retried(self):
        self.adapter.PROGRESS_SECONDS=.005
        result=await self.accept()
        self.reply.side_effect=TimeoutError('private details')
        await asyncio.sleep(.025)
        count=self.reply.await_count
        await asyncio.sleep(.02)
        self.assertEqual(self.reply.await_count,count)
        row=self.db.execute("SELECT status FROM whatsapp_deliveries WHERE kind='progress-running'").fetchone()
        self.assertEqual(row[0],'uncertain')
        self.finish(result['run'],'Short answer.');await self.settle()

    async def test_long_answer_is_explicit_excerpt_no_attachment_claim(self):
        result=await self.accept()
        self.finish(result['run'],'Disk is healthy. '+('Technical detail. '*300));await self.settle()
        answer=self.reply.call_args.args[1]
        self.assertIn("couldn't prepare a short summary",answer)
        self.assertIn('Full details are saved in the private dashboard',answer)
        self.assertNotIn('attachment sent',answer)
        self.assertLess(len(answer),900)

    async def test_batch_owner_revocation_is_not_submitted(self):
        self.adapter.BATCH_SECONDS=.02
        task=asyncio.create_task(self.accept())
        await asyncio.sleep(.001)
        from dataclasses import replace
        self.adapter.config=lambda:replace(CONFIG,owners=frozenset())
        self.assertEqual((await task)['status'],'ignored')
        self.submit.assert_not_awaited()

    async def test_simultaneous_sender_buffers_stay_separate(self):
        self.adapter.BATCH_SECONDS=.01
        one,two=await asyncio.gather(self.accept(text='Disk'),self.accept(sender='254700000002',message_id='other',text='Memory'))
        self.assertNotEqual(one['run'],two['run'])
        self.assertEqual({c.args[1] for c in self.submit.call_args_list},{'Disk','Memory'})
        self.assertNotEqual(self.submit.call_args_list[0].args[0],self.submit.call_args_list[1].args[0])

    async def test_failed_batch_consumes_all_fragment_ids(self):
        self.adapter.BATCH_SECONDS=.01
        self.submit.side_effect=RuntimeError('private failure')
        results=await asyncio.gather(self.accept(),self.accept(message_id='fragment',text='More context'),return_exceptions=True)
        self.assertTrue(all(isinstance(result,Exception) for result in results))
        self.assertEqual({r[0] for r in self.db.execute('SELECT status FROM whatsapp_inbox')},{'uncertain'})
        self.assertEqual((await self.accept(message_id='fragment',text='More context'))['status'],'duplicate')
        self.submit.assert_awaited_once()

    async def test_revision_requires_confirmation_then_restarts_once(self):
        old=await self.accept()
        await self.accept(message_id='correction',text='Only check memory')
        pending=self.adapter.revisions[(CONFIG.instance,'254700000001')]
        self.stop.assert_not_awaited();self.assertEqual(self.submit.await_count,1)
        confirmation='REVISE '+pending['token']
        new=await self.accept(message_id='confirm',text=confirmation)
        self.assertEqual(new['status'],'accepted')
        self.assertNotEqual(new['run'],old['run'])
        self.stop.assert_awaited_once_with(old['run'])
        self.assertIn('Only check memory',self.submit.call_args.args[1])
        self.assertIn('not replay earlier commands',self.submit.call_args.args[1])
        self.assertEqual((await self.accept(message_id='confirm',text=confirmation))['status'],'duplicate')
        self.assertEqual((await self.accept(message_id='confirm-again',text=confirmation))['status'],'rejected')
        self.assertEqual(self.submit.await_count,2)

    async def test_stale_completed_wrong_owner_and_failed_notice_never_restart(self):
        old=await self.accept()
        await self.accept(message_id='c1',text='Correction one')
        token=self.adapter.revisions[(CONFIG.instance,'254700000001')]['token']
        await self.accept(message_id='c2',text='Correction two')
        self.assertEqual((await self.accept(message_id='old',text='REVISE '+token))['status'],'rejected')
        latest=self.adapter.revisions[(CONFIG.instance,'254700000001')]['token']
        self.assertEqual((await self.accept(message_id='other',sender='254700000002',text='REVISE '+latest))['status'],'rejected')
        self.finish(old['run'])
        self.assertEqual((await self.accept(message_id='finished',text='REVISE '+latest))['status'],'rejected')
        self.stop.assert_not_awaited();self.submit.assert_awaited_once()

    async def test_failed_cancellation_and_expired_confirmation_are_not_replayed(self):
        await self.accept();await self.accept(message_id='c',text='Correction')
        pending=self.adapter.revisions[(CONFIG.instance,'254700000001')]
        self.stop.side_effect=RuntimeError('private error')
        self.assertEqual((await self.accept(message_id='go',text='REVISE '+pending['token']))['status'],'uncertain')
        self.assertEqual((await self.accept(message_id='again',text='REVISE '+pending['token']))['status'],'rejected')
        self.submit.assert_awaited_once()
        await self.accept(message_id='new-c',text='Another correction')
        pending=self.adapter.revisions[(CONFIG.instance,'254700000001')];pending['deadline']=0
        self.assertEqual((await self.accept(message_id='expired',text='REVISE '+pending['token']))['status'],'rejected')

    async def test_summary_uses_whole_answer_and_reports_failure_honestly(self):
        full='Routine detail. '*100+'The backup failed. Retry after replacing the disk.'
        self.adapter.summarize=AsyncMock(return_value='The backup failed; replace the disk before retrying.')
        result=await self.accept();self.finish(result['run'],full);await self.settle()
        self.adapter.summarize.assert_awaited_once_with(result['run'],full)
        self.assertIn('backup failed',self.reply.call_args.args[1])
        self.adapter.summarize.side_effect=RuntimeError('private error')
        self.assertIn("couldn't prepare",await self.adapter.brief_result('x',full))

