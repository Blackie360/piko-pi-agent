import test_app
import app as a
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from fastapi.testclient import TestClient
class FakeClient:
 def __init__(self,**kwargs):self.audio=SimpleNamespace(transcriptions=SimpleNamespace(create=self.transcribe),speech=SimpleNamespace(create=self.speak))
 async def __aenter__(self):return self
 async def __aexit__(self,*args):pass
 async def transcribe(self,**kwargs):
  assert kwargs['file'][1]==b'fakeaudio'
  return SimpleNamespace(text='Check my Pi')
 async def speak(self,**kwargs):return SimpleNamespace(content=b'fake-mp3')
class VoiceTests(unittest.TestCase):
 def setUp(self):
  self.c=TestClient(a.app,base_url='https://piko.example.test');self.h={'X-Pi-Agent':'1'}
 def test_audio_requires_auth(self):
  self.assertEqual(self.c.post('/api/voice/transcribe',content=b'fakeaudio',headers={**self.h,'Content-Type':'audio/webm'}).status_code,401)
 def test_limits_and_review_only(self):
  self.c.post('/api/login',json={'password':a.PASSWORD},headers=self.h)
  with patch.object(a,'settings',return_value={'key':'fake','model':'gpt-5-mini'}),patch.object(a,'AsyncOpenAI',FakeClient):
   count=a.DB.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
   h={**self.h,'Content-Type':'audio/webm'}
   self.assertEqual(self.c.post('/api/voice/transcribe',content=b'',headers=h).status_code,400)
   self.assertEqual(self.c.post('/api/voice/transcribe',content=b'x',headers=self.h).status_code,415)
   with patch.object(a,'AUDIO_LIMIT',4):self.assertEqual(self.c.post('/api/voice/transcribe',content=b'12345',headers=h).status_code,413)
   self.assertEqual(self.c.post('/api/voice/transcribe',content=b'fakeaudio',headers=h).json()['text'],'Check my Pi')
   self.assertEqual(a.DB.execute('SELECT COUNT(*) FROM messages').fetchone()[0],count)
   self.assertEqual(self.c.post('/api/voice/speak/999999',headers=self.h).status_code,404)
   a.DB.execute("INSERT INTO messages(chat,role,text) VALUES ('voice','assistant','Hello')");mid=a.DB.execute('SELECT last_insert_rowid()').fetchone()[0];a.DB.commit()
   r=self.c.post('/api/voice/speak/'+str(mid),headers=self.h)
   self.assertEqual(r.content,b'fake-mp3');self.assertEqual(r.headers['content-type'],'audio/mpeg')
if __name__=='__main__':unittest.main()

