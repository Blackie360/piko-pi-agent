import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import test_app
import app as a
from whatsapp_summary import validated_summary
from agents.tool_context import ToolContext

class Summary(unittest.IsolatedAsyncioTestCase):
    async def test_full_source_and_evidence_validation(self):
        source='Routine details. '*100+'The backup failed. Replace the disk.'
        raw=json.dumps({'sentences':[{'text':'The backup failed; replace the disk.','evidence':['The backup failed. Replace the disk.']}]})
        client=AsyncMock();client.__aenter__.return_value=client
        client.responses.create.return_value=SimpleNamespace(output_text=raw,usage=None)
        with patch.object(a,'settings',return_value={'key':'fixture','model':'fixture'}),patch.object(a,'AsyncOpenAI',return_value=client):
            self.assertEqual(await a.whatsapp_summary('r',source),'The backup failed; replace the disk.')
        self.assertEqual(json.loads(client.responses.create.call_args.kwargs['input'])['source'],source)
        self.assertNotIn('tools',client.responses.create.call_args.kwargs)
        self.assertIsNone(validated_summary(raw,'Unrelated source'))
        self.assertIsNone(validated_summary('bad json',source))

    async def test_report_selects_bound_chat_and_uses_media_checks(self):
        import uuid,time
        from test_whatsapp import CONFIG
        cid,rid,old=[uuid.uuid4().hex for _ in range(3)]
        sender=next(iter(CONFIG.owners))
        a.DB.execute('INSERT INTO chats(id,title,created) VALUES (?,?,0)',(cid,'fixture'))
        a.DB.execute("INSERT INTO runs VALUES (?,?,'complete')",(old,cid))
        message=a.DB.execute("INSERT INTO messages(chat,role,text) VALUES (?,'assistant','Entire saved report')",(cid,))
        a.DB.execute('INSERT INTO run_messages VALUES (?,?)',(old,message.lastrowid))
        a.DB.execute("INSERT INTO runs VALUES (?,?,'running')",(rid,cid))
        a.DB.execute("INSERT INTO run_channels VALUES (?,'whatsapp')",(rid,))
        a.DB.execute('INSERT OR REPLACE INTO whatsapp_chats VALUES (?,?,?)',(CONFIG.instance,sender,cid))
        a.DB.execute('INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,?,?)',(CONFIG.instance,rid,sender,'fixture',time.time(),rid,'running'));a.DB.commit()
        ctx=ToolContext(context=a.Context(rid),tool_name='send_whatsapp_report',tool_call_id='fixture',tool_arguments='{}')
        async def send(bound,path,kind,caption,*args):
            from pathlib import Path
            self.assertEqual(bound,rid);self.assertEqual(Path(path).read_text(),'Entire saved report')
            self.assertEqual((kind,caption),('document',''));return {'status':'uncertain'}
        with patch.object(a.whatsapp,'config',return_value=CONFIG),patch.object(a.whatsapp,'send_media',side_effect=send):
            self.assertEqual(await a.send_whatsapp_report.on_invoke_tool(ctx,'{}'),{'status':'uncertain'})
        with patch.object(a.whatsapp,'config',side_effect=RuntimeError('disabled')):
            self.assertEqual(await a.send_whatsapp_report.on_invoke_tool(ctx,'{}'),{'status':'blocked'})

