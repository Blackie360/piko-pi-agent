"""Local fixtures only: no provider calls, real credentials, or Pi commands."""
import asyncio
import json
import unittest
import uuid
import os
import subprocess
import sys
import tempfile
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_app  # Uses a private temporary data directory before importing app.
import app as a
from agents.tool_context import ToolContext
from agents.models.interface import Model
from agents.items import ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText


class FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.closed = False
        self.instances.append(self)

    async def close(self):
        self.closed = True


def result(text, history=None):
    return SimpleNamespace(
        final_output=text,
        context_wrapper=SimpleNamespace(usage=SimpleNamespace(
            input_tokens=2, output_tokens=1, total_tokens=3, requests=1)),
        to_input_list=lambda: (history or []) + [{'role': 'assistant', 'content': text}],
    )


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.001)


class Orchestration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = patch.object(a, 'settings', return_value={'key': 'fixture', 'model': 'fixture-model'})
        self.client = patch.object(a, 'AsyncOpenAI', FakeClient)
        self.settings.start(); self.client.start()
        self.addCleanup(self.settings.stop); self.addCleanup(self.client.stop)
        a.command_lock = asyncio.Lock()
        FakeClient.instances.clear()
        self.cid = (await a.new_chat())['id']

    async def asyncTearDown(self):
        for rid in list(a.active):
            await a.stop(rid)
        self.assertFalse(a.gates)
        self.assertFalse(a.runtimes)

    def run_row(self):
        rid = uuid.uuid4().hex
        a.DB.execute('INSERT INTO runs VALUES (?,?,?)', (rid, self.cid, 'running'))
        a.DB.commit()
        return rid

    def status(self, rid):
        return a.DB.execute('SELECT status FROM runs WHERE id=?', (rid,)).fetchone()[0]

    async def delegate(self, rid):
        return await a.delegate_tasks.on_invoke_tool(
            ToolContext(context=a.Context(rid),tool_name='delegate_tasks',tool_call_id='fixture-call',tool_arguments='{}'),
            json.dumps({'first_task': 'Inspect health', 'second_task': 'Research docs', 'shared_context': 'Synthetic task'}))

    async def test_two_workers_overlap_combine_and_persist(self):
        both_started = asyncio.Event()
        seen = []
        clients = []

        async def runner(agent, history, *, context, max_turns):
            clients.append(agent.model._client)
            self.assertFalse(agent.model_settings.parallel_tool_calls)
            if not context.worker_id:
                combined = json.loads(await self.delegate(context.rid))
                self.assertEqual(len(combined), 2)
                return result('Combined: ' + ' / '.join(w['result'] for w in combined), history)
            self.assertEqual({t.name for t in agent.tools}, {'system_overview', 'web_search'})
            seen.append(context.worker_id)
            if len(seen) == 2: both_started.set()
            await asyncio.wait_for(both_started.wait(), 2)
            return result(json.loads(history)['assignment'] + ' verified')

        with patch.object(a.Runner, 'run', side_effect=runner):
            rid = (await a.send(self.cid, a.Message(text='Inspect and research')))['run']
            await a.active[rid]
        d = await a.chat(self.cid)
        self.assertEqual(self.status(rid), 'complete')
        self.assertEqual([w['status'] for w in d['runs'][0]['workers']], ['complete', 'complete'])
        self.assertIn('Inspect health verified / Research docs verified', d['messages'][-1]['text'])
        self.assertEqual(len({id(c) for c in clients}), 1)
        self.assertTrue(clients[0].closed)
        self.assertEqual(a.DB.execute('SELECT SUM(total_tokens) FROM usage WHERE run=?', (rid,)).fetchone()[0], 9)
        saved = json.loads(a.DB.execute('SELECT history FROM chats WHERE id=?', (self.cid,)).fetchone()[0])
        self.assertIn('Combined:', saved[-1]['content'])

    async def test_worker_failure_keeps_other_result_and_marks_partial(self):
        async def runner(agent, history, *, context, max_turns):
            if not context.worker_id:
                rows = json.loads(await self.delegate(context.rid))
                self.assertEqual([w['status'] for w in rows], ['failed', 'complete'])
                return result('Available evidence: ' + rows[1]['result'], history)
            if agent.name.endswith('1'): raise ValueError('PRIVATE_PROVIDER_DETAIL')
            return result('Useful findings')

        with patch.object(a.Runner, 'run', side_effect=runner):
            rid = (await a.send(self.cid, a.Message(text='Inspect and research')))['run']
            await a.active[rid]
        d = await a.chat(self.cid)
        self.assertEqual(self.status(rid), 'partial')
        self.assertIn('Useful findings', d['messages'][-1]['text'])
        self.assertIn('partially complete', d['messages'][-1]['text'])
        self.assertNotIn('PRIVATE_PROVIDER_DETAIL', json.dumps(d))

    async def test_cancel_cascades_and_closes_client_after_workers(self):
        seen = set(); stopped = set()

        async def runner(agent, history, *, context, max_turns):
            if not context.worker_id:
                await self.delegate(context.rid)
                self.fail('Cancelled coordinator should not synthesize a result')
            seen.add(context.worker_id)
            try:
                await asyncio.Future()
            finally:
                self.assertFalse(agent.model._client.closed)
                stopped.add(context.worker_id)

        with patch.object(a.Runner, 'run', side_effect=runner):
            rid = (await a.send(self.cid, a.Message(text='Parallel task')))['run']
            await until(lambda: len(seen) == 2)
            await asyncio.gather(a.stop(rid), a.stop(rid))
        self.assertEqual(stopped, seen)
        self.assertEqual(self.status(rid), 'cancelled')
        self.assertFalse(a.active)
        self.assertTrue(FakeClient.instances[0].closed)
        d = await a.chat(self.cid)
        self.assertEqual([w['status'] for w in d['runs'][0]['workers']], ['cancelled', 'cancelled'])
        self.assertEqual([m['role'] for m in d['messages']], ['user','assistant'])
        self.assertIn('stopped',d['messages'][-1]['text'])

    async def test_overlapping_missions_rejected_without_extra_message(self):
        started = asyncio.Event()
        async def runner(*args, **kwargs):
            started.set(); await asyncio.Future()
        other = (await a.new_chat())['id']
        with patch.object(a.Runner, 'run', side_effect=runner):
            rid = (await a.send(self.cid, a.Message(text='First')))['run']
            await started.wait()
            for cid in (self.cid, other):
                with self.assertRaises(a.HTTPException) as exc:
                    await a.send(cid, a.Message(text='Overlap'))
                self.assertEqual(exc.exception.status_code, 409)
            self.assertFalse((await a.chat(other))['messages'])
            await a.stop(rid)

    async def test_cancel_before_first_turn_releases_active_slot(self):
        with patch.object(a.Runner, 'run', new_callable=AsyncMock) as runner:
            rid = (await a.send(self.cid, a.Message(text='Stop immediately')))['run']
            await a.stop(rid)
            runner.assert_not_called()
        self.assertEqual(self.status(rid), 'cancelled')
        self.assertFalse(a.active)

    async def test_disconnected_stop_request_keeps_slot_until_cleanup_finishes(self):
        started=asyncio.Event(); closing=asyncio.Event(); release=asyncio.Event()
        async def runner(*args, **kwargs):
            started.set(); await asyncio.Future()
        async def close(client):
            closing.set(); await release.wait(); client.closed=True
        with patch.object(a.Runner,'run',side_effect=runner),patch.object(FakeClient,'close',close):
            rid=(await a.send(self.cid,a.Message(text='Cancel during cleanup')))['run']
            mission=a.active[rid]
            await started.wait()
            request=asyncio.create_task(a.stop(rid))
            await closing.wait()
            request.cancel(); await request
            self.assertIs(a.active[rid],mission)
            self.assertFalse(mission.done())
            release.set(); await mission
        self.assertFalse(a.active)

    async def test_two_pending_approvals_remain_independent_and_one_shot(self):
        rid = self.run_row()
        shell = AsyncMock(return_value='fixture output')
        with patch.object(a, 'shell', shell):
            commands = [asyncio.create_task(a.approved_command(a.Context(rid), command, 'fixture', '/tmp'))
                        for command in ('first', 'second')]
            await until(lambda: len(a.gates) == 2)
            ids = list(a.gates)
            self.assertEqual(self.status(rid), 'waiting')
            await a.approve(ids[0], a.Decision(approved=True))
            await commands[0]
            self.assertEqual(self.status(rid), 'waiting')
            with self.assertRaises(a.HTTPException):
                await a.approve(ids[0], a.Decision(approved=True))
            await a.approve(ids[1], a.Decision(approved=False))
            self.assertIn('declined', await commands[1])
            shell.assert_awaited_once_with('first', '/tmp', rid)
        self.assertEqual(self.status(rid), 'running')

    async def test_stopping_clears_pending_approval_and_rejects_late_decision(self):
        async def runner(agent, history, *, context, max_turns):
            await a.approved_command(context, 'fixture', 'fixture', '/tmp')
            return result('should not run')
        with patch.object(a.Runner, 'run', side_effect=runner), patch.object(a, 'shell', new_callable=AsyncMock) as shell:
            rid = (await a.send(self.cid, a.Message(text='Await approval')))['run']
            await until(lambda: bool(a.gates))
            aid = next(iter(a.gates))
            await a.stop(rid)
            with self.assertRaises(a.HTTPException):
                await a.approve(aid, a.Decision(approved=True))
            shell.assert_not_called()
        self.assertEqual(self.status(rid), 'cancelled')

    async def test_all_shell_operations_are_serial_and_queue_is_cancellable(self):
        entered = []; release = asyncio.Event()
        async def command(command, cwd, ctx, timeout):
            entered.append(command)
            await release.wait()
            return command
        with patch.object(a, 'shell_unlocked', side_effect=command):
            first = asyncio.create_task(a.shell('first', '/tmp', self.run_row()))
            await until(lambda: entered == ['first'])
            second = asyncio.create_task(a.shell('second', '/tmp', self.run_row()))
            await asyncio.sleep(.02)
            self.assertEqual(entered, ['first'])
            second.cancel()
            with self.assertRaises(asyncio.CancelledError): await second
            release.set(); await first
            self.assertEqual(await a.shell('third', '/tmp', self.run_row()), 'third')
        self.assertEqual(entered, ['first', 'third'])

    async def test_workers_cannot_use_automatic_shell_policy(self):
        rid = self.run_row()
        with patch.object(a, 'settings', return_value={'auto_commands': True}), patch.object(a, 'shell', new_callable=AsyncMock) as shell:
            response = await a.approved_command(a.Context(rid, 'worker'), 'fixture', 'fixture', '/tmp')
            self.assertIn('cannot execute', response)
            shell.assert_not_called()

    async def test_existing_automatic_policies_are_preserved(self):
        for policy, command in (({'auto_commands': True}, 'fixture'),):
            rid = self.run_row()
            with patch.object(a, 'settings', return_value=policy), patch.object(a, 'shell', new_callable=AsyncMock, return_value='fixture') as shell:
                await a.approved_command(a.Context(rid), command, 'fixture', '/tmp')
                shell.assert_awaited_once_with(command, '/tmp', rid)
        rid = self.run_row()
        with patch.object(a, 'settings', return_value={'auto_sudo': True}), patch.object(a, 'shell', new_callable=AsyncMock) as shell:
            task = asyncio.create_task(a.approved_command(a.Context(rid), 'ordinary', 'fixture', '/tmp'))
            await until(lambda: bool(a.gates))
            await a.approve(next(iter(a.gates)), a.Decision(approved=False))
            await task; shell.assert_not_called()

    async def test_second_delegation_cannot_spawn_more_workers(self):
        calls = 0
        async def runner(agent, history, *, context, max_turns):
            nonlocal calls
            if context.worker_id:
                calls += 1
                return result('Done')
            await self.delegate(context.rid)
            self.assertIn('already used', await self.delegate(context.rid))
            return result('Combined', history)
        with patch.object(a.Runner, 'run', side_effect=runner):
            rid = (await a.send(self.cid, a.Message(text='Bounded delegation')))['run']
            await a.active[rid]
        self.assertEqual(calls, 2)

    async def test_coordinator_error_releases_slot_without_leaking_details(self):
        with patch.object(a.Runner, 'run', side_effect=RuntimeError('PRIVATE_DETAIL')):
            rid = (await a.send(self.cid, a.Message(text='Fail safely')))['run']
            await a.active[rid]
        self.assertEqual(self.status(rid), 'failed')
        self.assertFalse(a.active)
        self.assertNotIn('PRIVATE_DETAIL', json.dumps(await a.chat(self.cid)))

    async def test_real_sdk_executes_delegation_and_returns_combined_history(self):
        """Exercise the installed SDK's tool dispatch, not a mocked Runner."""
        owner = self
        models = []
        worker_started = set()
        both_started = asyncio.Event()

        class FixtureModel(Model):
            def __init__(self, **kwargs):
                self.number = len(models); self.calls = 0
                models.append(self)

            async def get_response(self, *args, **kwargs):
                self.calls += 1
                if self.number == 0 and self.calls == 1:
                    output = [ResponseFunctionToolCall(type='function_call', id='fixture-tool', call_id='fixture-delegation', name='delegate_tasks', arguments=json.dumps({'first_task':'Inspect health','second_task':'Research docs','shared_context':'Fixture'}))]
                else:
                    if self.number:
                        worker_started.add(self.number)
                        if len(worker_started) == 2: both_started.set()
                        await asyncio.wait_for(both_started.wait(), 2)
                        text = f'Worker {self.number} evidence'
                    else:
                        input_items = kwargs.get('input', args[1] if len(args)>1 else [])
                        serialized = json.dumps(input_items)
                        owner.assertIn('Worker 1 evidence', serialized)
                        owner.assertIn('Worker 2 evidence', serialized)
                        text = 'Combined worker evidence'
                    output = [ResponseOutputMessage(type='message', id=f'msg-{self.number}', role='assistant', status='completed', content=[ResponseOutputText(type='output_text',text=text,annotations=[],logprobs=[])])]
                return ModelResponse(output=output,usage=Usage(requests=1,input_tokens=2,output_tokens=1,total_tokens=3),response_id=f'fixture-{self.number}-{self.calls}')

            async def stream_response(self, *args, **kwargs):
                raise AssertionError('Streaming is not used')
                yield

        with patch.object(a, 'OpenAIResponsesModel', FixtureModel):
            rid = (await a.send(self.cid, a.Message(text='Inspect and research')))['run']
            await a.active[rid]
        self.assertEqual(self.status(rid), 'complete', json.dumps(await a.chat(self.cid)))
        self.assertEqual(len(models), 3)
        self.assertEqual([m.calls for m in models], [2,1,1])
        self.assertEqual((await a.chat(self.cid))['messages'][-1]['text'], 'Combined worker evidence')
        saved = json.loads(a.DB.execute('SELECT history FROM chats WHERE id=?',(self.cid,)).fetchone()[0])
        self.assertTrue(any(item.get('type')=='function_call_output' for item in saved))


class RestartRecovery(unittest.TestCase):
    def test_restart_marks_active_work_interrupted_and_preserves_finished_results(self):
        with tempfile.TemporaryDirectory(prefix='pi-agent-restart-test-') as directory:
            db = sqlite3.connect(os.path.join(directory, 'agent.db'))
            db.executescript('''CREATE TABLE runs(id TEXT PRIMARY KEY,chat TEXT,status TEXT);
                INSERT INTO runs VALUES ('fixture-run','fixture-chat','cancelling');
                CREATE TABLE workers(id TEXT PRIMARY KEY,run TEXT NOT NULL,slot INTEGER NOT NULL,task TEXT NOT NULL,status TEXT NOT NULL,result TEXT,error TEXT,started REAL,finished REAL);
                INSERT INTO workers VALUES ('one','fixture-run',1,'fixture','running',NULL,NULL,0,NULL);
                INSERT INTO workers VALUES ('two','fixture-run',2,'fixture','complete','saved findings',NULL,0,1);''')
            db.commit(); db.close()
            run = subprocess.run([sys.executable,'-c','import app'], env={**os.environ,'PI_AGENT_DATA':directory},capture_output=True,text=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stderr)
            db=sqlite3.connect(os.path.join(directory,'agent.db'))
            self.assertEqual(db.execute('SELECT status FROM runs').fetchone()[0],'interrupted')
            self.assertEqual(db.execute('SELECT status,result FROM workers ORDER BY slot').fetchall(),[('interrupted',None),('complete','saved findings')])
            db.close()


if __name__ == '__main__':
    unittest.main()

