import json
import unittest
import uuid
from unittest.mock import AsyncMock, patch
import httpx
import test_app
import app as a
from test_orchestration import FakeClient


class ConversationApp(unittest.IsolatedAsyncioTestCase):
    async def test_failed_run_keeps_truthful_history_and_busy_correction(self):
        cid,rid=uuid.uuid4().hex,uuid.uuid4().hex
        a.DB.execute('INSERT INTO chats(id,title,created) VALUES (?,?,0)',(cid,'fixture'))
        a.DB.execute("INSERT INTO messages(chat,role,text) VALUES (?,'user','Inspect disk')",(cid,))
        a.DB.execute("INSERT INTO runs VALUES (?,?,'running')",(rid,cid))
        a.DB.execute("INSERT INTO run_channels VALUES (?,'whatsapp')",(rid,))
        a.DB.execute('INSERT INTO whatsapp_notes VALUES (?,?,?,?,?)',('fixture',rid,rid,'Only inspect, do not edit',"I haven't applied the correction."));a.DB.commit()
        with patch.object(a,'settings',return_value={'key':'fixture','model':'fixture'}),patch.object(a,'AsyncOpenAI',FakeClient),patch.object(a.Runner,'run',side_effect=RuntimeError('private details')):
            await a.run_agent(cid,rid,'Inspect disk',[])
        history=json.loads(a.DB.execute('SELECT history FROM chats WHERE id=?',(cid,)).fetchone()[0])
        self.assertIn('Only inspect, do not edit',json.dumps(history))
        self.assertIn('task failed',history[-1]['content'])
        self.assertNotIn('private details',json.dumps(history))
        details=await a.chat(cid)
        self.assertIn('task failed',details['runs'][0]['answer'])
        a.persist_terminal_outcome(rid)
        self.assertEqual(a.DB.execute('SELECT COUNT(*) FROM run_messages WHERE run=?',(rid,)).fetchone()[0],1)

    async def test_live_voice_uses_client_confirmation_and_no_sudo_preauthorization(self):
        client=AsyncMock();client.__aenter__.return_value=client
        client.post.return_value=httpx.Response(200,text='fixture answer sdp')
        with patch.object(a,'settings',return_value={'key':'fixture'}),patch.object(a.httpx,'AsyncClient',return_value=client):
            await a.live_voice(a.LiveOffer(sdp='v=0\r\n'+'a=fixture\r\n'*10))
        session=json.loads(client.post.call_args.kwargs['files']['session'][1])
        self.assertFalse(session['audio']['input']['turn_detection']['create_response'])
        self.assertTrue(session['audio']['input']['turn_detection']['interrupt_response'])
        self.assertIn('fresh approval for sudo',session['instructions'])
        self.assertNotIn('authorizes requested tasks including sudo',session['instructions'])
        self.assertEqual([t['name'] for t in session['tools']],['pi_task'])

