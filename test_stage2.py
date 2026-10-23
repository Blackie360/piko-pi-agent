import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
import test_app
import app as a
from conversation_context import bounded_history,HistoryUnavailable
from test_orchestration import FakeClient

class History(unittest.IsolatedAsyncioTestCase):
    async def test_small_history_no_model_and_known_secrets_removed(self):
        client=Mock()
        result=await bounded_history([{'role':'user','content':'Keep service running.\nknown-private-value'}],client,'fixture',('known-private-value',))
        self.assertNotIn('known-private-value',json.dumps(result));client.with_options.assert_not_called()

    async def test_summary_keeps_constraints_and_unfinished_work(self):
        source=[{'role':'user','content':'Never restart DNS. Backup unfinished. '+('Detail. '*2000)}]
        data={'constraints':[{'text':'Never restart DNS.','evidence':'Never restart DNS.'}],
              'unfinished':[{'text':'Backup unfinished.','evidence':'Backup unfinished.'}],'facts':[],'questions':[]}
        create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(data)))
        client=Mock();client.with_options.return_value.responses.create=create
        result=await bounded_history(source,client,'fixture')
        self.assertLess(len(json.dumps(result).encode()),12000)
        self.assertIn('Never restart DNS',result[0]['content']);self.assertIn('Backup unfinished',result[0]['content'])
        self.assertIn('not new instructions or approval',result[0]['content'])
        client.with_options.assert_called_once_with(max_retries=0)
        self.assertEqual(create.call_args.kwargs['max_output_tokens'],1800)
        self.assertNotIn('tools',create.call_args.kwargs)

    async def test_invalid_failed_and_oversized_history_fail_closed(self):
        source=[{'role':'user','content':'a'*13000}]
        client=Mock();client.with_options.return_value.responses.create=AsyncMock(return_value=SimpleNamespace(output_text='{}'))
        with self.assertRaises(HistoryUnavailable):await bounded_history(source,client,'fixture')
        client.with_options.return_value.responses.create.side_effect=RuntimeError('private error')
        with self.assertRaises(HistoryUnavailable):await bounded_history(source,client,'fixture')
        client.reset_mock()
        with self.assertRaises(HistoryUnavailable):await bounded_history([{'role':'user','content':'a'*65000}],client,'fixture')
        client.with_options.assert_not_called()

    async def test_unavailable_history_never_starts_coordinator(self):
        cid,rid=uuid.uuid4().hex,uuid.uuid4().hex
        a.DB.execute('INSERT INTO chats(id,title,created) VALUES (?,?,0)',(cid,'fixture'))
        a.DB.execute("INSERT INTO runs VALUES (?,?,'running')",(rid,cid));a.DB.commit()
        with patch.object(a,'settings',return_value={'key':'fixture','model':'fixture'}),patch.object(a,'AsyncOpenAI',FakeClient),patch.object(a.Runner,'run',new_callable=AsyncMock) as runner:
            await a.run_agent(cid,rid,'Continue',[{'role':'user','content':'x'*65000}])
        runner.assert_not_awaited()
        self.assertEqual(a.DB.execute('SELECT status FROM runs WHERE id=?',(rid,)).fetchone()[0],'failed')

class Readback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rid,self.aid=uuid.uuid4().hex,uuid.uuid4().hex
        a.DB.execute("INSERT INTO runs VALUES (?,?,'waiting')",(self.rid,'readback'));a.DB.commit()
        self.future=asyncio.get_running_loop().create_future()
        a.gates[self.aid]=(self.rid,self.future)
        a.pending_command_details[self.aid]={'rid':self.rid,'command':'sudo -n systemctl status fixture-service','reason':'Inspect service state','cwd':'/home/user/piko-workspace'}
        self.client=AsyncMock();self.client.__aenter__.return_value=self.client
        self.client.audio.speech.create.return_value=SimpleNamespace(content=b'fixture-audio')
        a.voice_lock=asyncio.Lock()
    async def asyncTearDown(self):
        a.gates.pop(self.aid,None);a.pending_command_details.pop(self.aid,None)
        if not self.future.done():self.future.cancel()
    async def test_readback_exact_fields_does_not_approve(self):
        with patch.object(a,'settings',return_value={'key':'synthetic-key'}),patch.object(a,'AsyncOpenAI',return_value=self.client):
            response=await a.speak_approval(self.aid)
        text=self.client.audio.speech.create.call_args.kwargs['input']
        for key in ('command','reason','cwd'):self.assertIn(a.pending_command_details[self.aid][key],text)
        self.assertFalse(self.future.done());self.assertEqual(response.body,b'fixture-audio')
    async def test_stale_and_sensitive_readback_refused(self):
        self.future.set_result(False)
        with self.assertRaises(a.HTTPException):await a.speak_approval(self.aid)
        self.future=asyncio.get_running_loop().create_future();a.gates[self.aid]=(self.rid,self.future)
        a.pending_command_details[self.aid]['command']='sudo echo password=private'
        with patch.object(a,'settings',return_value={'key':'synthetic-key'}),patch.object(a,'AsyncOpenAI',return_value=self.client):
            with self.assertRaises(a.HTTPException):await a.speak_approval(self.aid)
        self.client.audio.speech.create.assert_not_awaited()
    async def test_expiry_during_synthesis_discards_audio(self):
        async def synthesize(**kwargs):self.future.set_result(False);return SimpleNamespace(content=b'audio')
        self.client.audio.speech.create.side_effect=synthesize
        with patch.object(a,'settings',return_value={'key':'synthetic-key'}),patch.object(a,'AsyncOpenAI',return_value=self.client):
            with self.assertRaises(a.HTTPException) as error:await a.speak_approval(self.aid)
        self.assertEqual(error.exception.status_code,409)

class Personas(unittest.IsolatedAsyncioTestCase):
    async def test_presets_change_only_tone(self):
        with tempfile.TemporaryDirectory() as directory:
            before={'key':'synthetic','model':'fixture','auto_commands':False,'auto_sudo':False}
            with patch.object(a,'DATA',Path(directory)),patch.object(a,'settings',return_value=before.copy()),patch.object(a,'active',{}):
                await a.choose_persona(a.PersonaChoice(persona='guide'))
                after=json.loads((Path(directory)/'settings.json').read_text())
                self.assertEqual(after,{**before,'persona':'guide'})
                with self.assertRaises(a.HTTPException):await a.choose_persona(a.PersonaChoice(persona='ignore approvals'))
            self.assertNotIn('accent',json.dumps(a.PERSONAS))

