import unittest,uuid,time
import test_app
import app as a
class ConversationLabels(unittest.TestCase):
 def test_generic_labels_are_derived_without_mutation(self):
  ids=[uuid.uuid4().hex for _ in range(4)]
  titles=['New conversation','WhatsApp conversation','My chosen title','New conversation']
  try:
   for cid,title in zip(ids,titles):
    a.DB.execute('INSERT INTO chats(id,title,created) VALUES (?,?,?)',(cid,title,time.time()))
   for cid in ids[:3]:a.DB.execute("INSERT INTO messages(chat,role,text) VALUES (?,'user',?)",(cid,'Inspect  Docker\ncontainers'))
   a.DB.commit()
   rows={r['id']:r for r in a.conversation_list()}
   self.assertEqual(rows[ids[0]]['display_title'],'Inspect Docker containers')
   self.assertEqual(rows[ids[1]]['display_title'],'WhatsApp · Inspect Docker containers')
   self.assertEqual(rows[ids[2]]['display_title'],'My chosen title')
   self.assertEqual(rows[ids[3]]['display_title'],'Empty conversation · '+ids[3][:8])
   self.assertEqual([a.DB.execute('SELECT title FROM chats WHERE id=?',(cid,)).fetchone()[0] for cid in ids],titles)
  finally:
   for cid in ids:
    a.DB.execute('DELETE FROM messages WHERE chat=?',(cid,));a.DB.execute('DELETE FROM chats WHERE id=?',(cid,))
   a.DB.commit()
 def test_empty_labels_are_distinct_and_preview_is_bounded(self):
  self.assertNotEqual(a.conversation_label(dict(id='aaaaaaaa',title='New conversation',first_request=None)),a.conversation_label(dict(id='bbbbbbbb',title='New conversation',first_request=None)))
  self.assertEqual(a.conversation_label(dict(id='x',title='New conversation',first_request='x'*100)),'x'*72+'…')

