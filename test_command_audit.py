import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import test_app
import app as a
from command_audit import Audit,snapshot,compare,sanitize

class Commands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();a.command_lock=asyncio.Lock()

    def latest(self,rid):
        return dict(a.DB.execute('SELECT * FROM command_audit WHERE run=? ORDER BY started DESC',(rid,)).fetchone())

    async def test_success_failure_and_approval_source(self):
        result=await a.authorized_shell('printf fixture; exit 7',str(self.root),'audit-failure','dashboard_explicit')
        row=self.latest('audit-failure');self.assertEqual(row['exit_code'],7);self.assertEqual(row['status'],'failed')
        self.assertEqual(row['output_bytes'],7);self.assertEqual(row['approval_source'],'dashboard_explicit')
        self.assertIn('Exit code: 7',result);self.assertIsNotNone(row['finished'])
        await a.shell('true',str(self.root),'audit-success',worker_id='worker-fixture')
        self.assertEqual(self.latest('audit-success')['worker'],'worker-fixture')

    async def test_timeout_cancellation_and_launch_failure_are_durable(self):
        await a.shell('sleep 3',str(self.root),'audit-timeout',timeout=.02)
        self.assertEqual(self.latest('audit-timeout')['timed_out'],1)
        self.assertEqual(self.latest('audit-timeout')['signal'],9)
        task=asyncio.create_task(a.shell('sleep 3',str(self.root),'audit-cancel'))
        await asyncio.sleep(.02);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.latest('audit-cancel')['cancelled'],1)
        with patch.object(a.asyncio,'create_subprocess_exec',side_effect=OSError('private failure')):
            await a.shell('fixture',str(self.root),'audit-launch')
        self.assertEqual(self.latest('audit-launch')['status'],'failed')
        self.assertIsNone(self.latest('audit-launch')['exit_code'])

    async def test_output_bytes_truncation_and_split_secret_redaction(self):
        with patch.object(a,'private_values',return_value=('credential-value',)):
            result=await a.shell("printf credential-; printf value; printf '\\n'; head -c 30000 /dev/zero | tr '\\000' x",str(self.root),'audit-output')
        row=self.latest('audit-output')
        self.assertNotIn('credential-value',result);self.assertNotIn('credential-value',row['output'])
        self.assertEqual(row['output_bytes'],30017);self.assertEqual(row['truncated'],1)
        self.assertLessEqual(row['stored_bytes'],24000)

    async def test_scoped_diff_and_no_scope_no_scan(self):
        path=self.root/'file.txt';path.write_text('before\n')
        a.DB.execute('INSERT OR REPLACE INTO run_workspaces VALUES (?,?)',('audit-diff',str(self.root)));a.DB.commit()
        await a.shell("printf 'after\\n' > file.txt",str(self.root),'audit-diff')
        row=self.latest('audit-diff');self.assertIn('-before',row['diff']);self.assertIn('+after',row['diff'])
        with patch.object(a,'audit_snapshot') as snap:
            await a.shell('true',str(self.root),'audit-no-scope');snap.assert_not_called()

    async def test_completion_is_committed_before_post_snapshot(self):
        a.DB.execute('INSERT OR REPLACE INTO run_workspaces VALUES (?,?)',('audit-order',str(self.root)));a.DB.commit()
        calls=[]
        def snap(*args):
            calls.append(1)
            if len(calls)==2:
                row=self.latest('audit-order')
                self.assertEqual(row['status'],'complete');self.assertIsNotNone(row['finished'])
                raise OSError('private snapshot error')
            return {},[]
        with patch.object(a,'audit_snapshot',side_effect=snap):await a.shell('true',str(self.root),'audit-order')
        self.assertEqual(self.latest('audit-order')['status'],'complete')
        self.assertIn('snapshot failed',self.latest('audit-order')['diff_coverage'])

class Snapshots(unittest.TestCase):
    def test_sensitive_symlink_limits_and_newly_sensitive_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'safe.txt').write_text('before')
            (root/'.env').write_text('private');(root/'key.txt').write_text('password=private')
            (root/'link.txt').symlink_to(root/'safe.txt')
            before=snapshot(str(root));self.assertEqual(set(before[0]),{'safe.txt'})
            (root/'safe.txt').write_text('secret=private')
            diff,coverage=compare(before,snapshot(str(root)))
            self.assertNotIn('private',diff);self.assertNotIn('before',diff)
            self.assertIn('excluded',coverage)
            with patch('command_audit.MAX_FILES',0): self.assertFalse(snapshot(str(root))[0])
    def test_restart_recovery_and_redaction(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        audit=Audit(db);cid=audit.start('r',None,'echo password=secret','/tmp','fixture',())
        Audit(db);row=db.execute('SELECT * FROM command_audit WHERE id=?',(cid,)).fetchone()
        self.assertEqual(row['status'],'interrupted');self.assertIsNone(row['exit_code'])
        self.assertNotIn('secret',row['command']);db.close()

