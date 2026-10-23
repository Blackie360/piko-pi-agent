import os,tempfile,unittest,asyncio
from pathlib import Path
os.environ['PI_AGENT_DATA']=tempfile.mkdtemp(prefix='pi-agent-test-')
os.environ['PI_AGENT_WORKSPACE']=str(Path(os.environ['PI_AGENT_DATA'])/'workspace')
os.environ['PI_AGENT_ALLOWED_ORIGINS']='https://piko.example.test'
import app as a
from fastapi.testclient import TestClient
class WebSecurity(unittest.TestCase):
 def setUp(self):self.client=TestClient(a.app,base_url='https://piko.example.test')
 def test_auth_csrf_and_setup(self):
  self.assertEqual(self.client.get('/api/state').status_code,401)
  self.assertEqual(self.client.post('/api/login',json={'password':a.PASSWORD}).status_code,403)
  h={'X-Pi-Agent':'1','Origin':'https://evil.example'}
  self.assertEqual(self.client.post('/api/login',json={'password':a.PASSWORD},headers=h).status_code,403)
  h={'X-Pi-Agent':'1'}
  self.assertEqual(self.client.post('/api/login',json={'password':'wrong'},headers=h).status_code,401)
  response=self.client.post('/api/login',json={'password':a.PASSWORD},headers=h)
  self.assertEqual(response.status_code,200)
  self.assertIn('Secure',response.headers['set-cookie']);self.assertIn('HttpOnly',response.headers['set-cookie'])
  self.assertFalse(self.client.get('/api/state').json()['configured'])
  cid=self.client.post('/api/chats',json={},headers=h).json()['id']
  response=self.client.post('/api/chats/'+cid+'/send',json={'text':'do something'},headers=h)
  self.assertEqual(response.status_code,400)
  self.client.post('/api/logout',json={},headers=h)
  self.assertEqual(self.client.get('/api/state').status_code,401)
 def test_configured_origin_is_accepted(self):
  response=self.client.post('/api/login',json={'password':a.PASSWORD},headers={'X-Pi-Agent':'1','Origin':'https://piko.example.test'})
  self.assertEqual(response.status_code,200)
 def test_unconfigured_origin_fails_closed(self):
  from unittest.mock import patch
  with patch.object(a,'ALLOWED_ORIGINS',frozenset()):
   response=self.client.post('/api/login',json={'password':a.PASSWORD},headers={'X-Pi-Agent':'1','Origin':'https://piko.example.test'})
   self.assertEqual(response.status_code,403)
class Commands(unittest.IsolatedAsyncioTestCase):
 async def test_approval_is_required_and_one_shot(self):
  rid='testapproval';a.DB.execute('INSERT INTO runs VALUES (?,?,?)',(rid,'test','running'));a.DB.commit()
  marker=Path(os.environ['PI_AGENT_DATA'])/'marker'
  task=asyncio.create_task(a.approved_command(a.Context(rid),'touch '+str(marker),'test',str(marker.parent)))
  await asyncio.sleep(.02)
  self.assertFalse(marker.exists())
  aid=next(iter(a.gates))
  await a.approve(aid,a.Decision(approved=False));self.assertIn('declined',await task);self.assertFalse(marker.exists())
  with self.assertRaises(a.HTTPException):await a.approve(aid,a.Decision(approved=True))
  task=asyncio.create_task(a.approved_command(a.Context(rid),'touch '+str(marker)+'; printf verified','test',str(marker.parent)))
  await asyncio.sleep(.02);aid=next(iter(a.gates))
  await a.approve(aid,a.Decision(approved=True));self.assertIn('verified',await task);self.assertTrue(marker.exists())
 async def test_cancel_pending_task_never_executes(self):
  rid='testcancel';a.DB.execute('INSERT INTO runs VALUES (?,?,?)',(rid,'test','running'));a.DB.commit()
  marker=Path(os.environ['PI_AGENT_DATA'])/'cancelled'
  task=asyncio.create_task(a.approved_command(a.Context(rid),'touch '+str(marker),'test',str(marker.parent)))
  await asyncio.sleep(.02);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  self.assertFalse(marker.exists());self.assertFalse(a.gates)
 async def test_shell_timeout(self):
  result=await a.shell('sleep 20',os.environ['PI_AGENT_DATA'],'timeout',timeout=.1)
  self.assertIn('stopped',result)
if __name__=='__main__':unittest.main()

