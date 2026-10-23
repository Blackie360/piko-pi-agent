"""Private authenticated relay adapter. No raw provider webhook is trusted here."""
import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
import uuid

PHONE = re.compile(r'[1-9][0-9]{7,14}\Z')
IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate field')
        result[key] = value
    return result


class AdapterError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass(frozen=True)
class Config:
    instance: str
    owners: frozenset
    relay_key: str = field(repr=False)
    api_key: str = field(repr=False)


def credential(name, legacy_variable):
    """Prefer systemd credentials; never fall back if its credential is missing."""
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if directory:
        try:
            with (Path(directory) / name).open('rb') as source:
                value = source.read(4097)
            if not value or len(value) > 4096 or any(c <= 32 or c >= 127 for c in value):
                raise ValueError()
            return value.decode('ascii')
        except (OSError, ValueError):
            raise AdapterError(503, 'WhatsApp credential unavailable') from None
    return os.environ.get(legacy_variable, '')


def load_config():
    """Read configuration and private credentials; never generate or persist secrets."""
    if os.environ.get('PI_WHATSAPP_ENABLED') != '1':
        raise AdapterError(503, 'WhatsApp adapter disabled')
    # This acknowledges an operator-verified upstream contract, not proof by itself.
    if os.environ.get('PI_WHATSAPP_VERIFIED_RELAY') != '1':
        raise AdapterError(503, 'Verified relay required')
    instance = os.environ.get('PI_WHATSAPP_INSTANCE', '')
    owners = frozenset(os.environ.get('PI_WHATSAPP_OWNERS', '').split(','))
    key = credential('piko-relay-key', 'PI_WHATSAPP_RELAY_KEY')
    api_key = credential('flaresend-token', 'PI_WHATSAPP_API_KEY')
    if (not IDENTIFIER.fullmatch(instance) or not owners or len(owners) > 10
            or not all(PHONE.fullmatch(owner) for owner in owners)
            or len(key) < 32 or len(api_key) < 16
            or any('REPLACE' in value or '<' in value for value in (key, api_key, instance))):
        raise AdapterError(503, 'WhatsApp configuration incomplete')
    return Config(instance, owners, key, api_key)


class WhatsAppAdapter:
    MAX_BODY = 16384
    MAX_REPLY = 3500
    MAX_TASK_SECONDS = 900
    POLL_SECONDS = .5
    COOLDOWN_SECONDS = 5
    BATCH_SECONDS = .65
    PROGRESS_SECONDS = 30

    def __init__(self, db, submit, stop, reply, config=load_config, decide=None, summarize=None):
        self.db, self.submit, self.stop, self.reply, self.config = db, submit, stop, reply, config
        self.monitors = set()
        self.decide = decide
        self.summarize = summarize
        self.revisions = {}
        self.revision_versions = {}
        self.pending_commands = {}
        self.session_grants = {}
        self.closed_chats = set()
        self.batches = {}
        db.executescript('''
        CREATE TABLE IF NOT EXISTS whatsapp_chats(
            instance TEXT, sender TEXT, chat TEXT NOT NULL, PRIMARY KEY(instance,sender));
        CREATE TABLE IF NOT EXISTS whatsapp_inbox(
            instance TEXT, message TEXT, sender TEXT NOT NULL, digest TEXT NOT NULL,
            created REAL NOT NULL, run TEXT, status TEXT NOT NULL,
            PRIMARY KEY(instance,message));
        CREATE TABLE IF NOT EXISTS whatsapp_deliveries(
            instance TEXT, message TEXT, kind TEXT, status TEXT NOT NULL,
            PRIMARY KEY(instance,message,kind));
        ''')
        db.execute("""CREATE TABLE IF NOT EXISTS whatsapp_media_attempts(
            instance TEXT, sender TEXT, digest TEXT, status TEXT NOT NULL,
            PRIMARY KEY(instance,sender,digest))""")
        db.execute("UPDATE whatsapp_media_attempts SET status='uncertain' WHERE status='sending'")
        db.execute("""CREATE TABLE IF NOT EXISTS whatsapp_media_diagnostics(
            id TEXT PRIMARY KEY, run TEXT NOT NULL, created REAL NOT NULL,
            category TEXT NOT NULL, http_status INTEGER, outcome TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS whatsapp_notes(
            instance TEXT, message TEXT, run TEXT, text TEXT, answer TEXT,
            PRIMARY KEY(instance,message))""")
        # Never replay a task or an uncertain send after a restart.
        db.execute("UPDATE whatsapp_inbox SET status='interrupted' WHERE status IN ('reserved','running')")
        db.execute("UPDATE whatsapp_deliveries SET status='uncertain' WHERE status='sending'")
        db.commit()

    async def accept(self, body, headers):
        cfg = self.config()
        if len(body) > self.MAX_BODY:
            raise AdapterError(413, 'Message too large')
        timestamp = headers.get('x-pi-relay-timestamp', '')
        signature = headers.get('x-pi-relay-signature', '')
        if not re.fullmatch(r'[0-9]{10}', timestamp) or abs(time.time()-int(timestamp)) > 60:
            raise AdapterError(401, 'Relay authentication failed')
        expected = hmac.new(cfg.relay_key.encode(), timestamp.encode()+b'.'+body, hashlib.sha256).hexdigest()
        if not re.fullmatch(r'[0-9a-f]{64}', signature) or not hmac.compare_digest(expected, signature):
            raise AdapterError(401, 'Relay authentication failed')
        try:
            data = json.loads(body, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise AdapterError(400, 'Invalid relay envelope') from None
        fields = {'version', 'event', 'instance', 'message_id', 'sender', 'text', 'from_self', 'is_group'}
        if (not isinstance(data, dict) or set(data) not in (fields, fields | {'sent_at'})
                or type(data['version']) is not int or data['version'] != 1
                or not all(isinstance(data[k], str) for k in ('event','instance','message_id','sender','text'))
                or type(data['from_self']) is not bool or type(data['is_group']) is not bool):
            raise AdapterError(400, 'Invalid relay envelope')
        if 'sent_at' in data and (type(data['sent_at']) is not int or data['sent_at'] <= 0):
            raise AdapterError(400, 'Invalid message timestamp')
        if data['instance'] != cfg.instance:
            raise AdapterError(403, 'Instance not allowed')
        if data['from_self'] or data['is_group'] or data['event'] != 'message_received':
            return {'status': 'ignored'}
        sender, mid, text = data['sender'], data['message_id'], data['text']
        if sender not in cfg.owners:
            raise AdapterError(403, 'Sender not allowed')
        if not IDENTIFIER.fullmatch(mid) or not text.strip() or len(text) > 8000:
            raise AdapterError(400, 'Invalid message')
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        previous = self.db.execute('SELECT * FROM whatsapp_inbox WHERE instance=? AND message=?',
                                   (cfg.instance, mid)).fetchone()
        if previous:
            if previous['digest'] != digest:
                raise AdapterError(409, 'Message ID reused with different content')
            return {'status': 'duplicate', 'run': previous['run']}
        if text.strip().lower() in ('exit', '/exit'):
            return await self.exit_session(cfg, mid, sender, digest)
        if re.match(r'(?i)^revise(?:\s|$)', text.strip()):
            return await self.confirm_revision(cfg,mid,sender,digest,text)
        if re.match(r'(?i)^(approve|deny)(?:\s|$)', text.strip()):
            return await self.command_decision(cfg, mid, sender, text, digest, data.get('sent_at'))
        active = self.db.execute("""SELECT r.id,r.status FROM runs r JOIN whatsapp_chats c ON c.chat=r.chat
            WHERE c.instance=? AND c.sender=? AND r.status IN ('running','waiting','cancelling')""",
            (cfg.instance,sender)).fetchone()
        if active:
            return await self.busy_message(cfg,mid,sender,digest,text,active)
        key = (cfg.instance,sender)
        batch = self.batches.get(key)
        if batch and (batch['cfg'] != cfg or len(batch['items']) >= 8 or sum(len(t) for _,t in batch['items'])+len(text)>8000):
            raise AdapterError(429, 'Please wait for the current message group')
        if not batch:
            if len(self.monitors) >= 4:
                raise AdapterError(429, 'Adapter busy')
            recent = self.db.execute('SELECT MAX(created) FROM whatsapp_inbox WHERE instance=? AND sender=?',
                                     (cfg.instance, sender)).fetchone()[0]
            if recent is not None and time.time()-recent < self.COOLDOWN_SECONDS:
                raise AdapterError(429, 'Please wait before sending another task')
        self.db.execute('INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,NULL,?)',
                        (cfg.instance, mid, sender, digest, time.time(), 'reserved'))
        self.db.commit()
        if not batch:
            batch = {'cfg':cfg, 'items':[], 'closed':False}
            self.batches[key] = batch
            batch['task'] = asyncio.create_task(self.flush_batch(key,batch))
            self.monitors.add(batch['task'])
            batch['task'].add_done_callback(self.monitors.discard)
        batch['items'].append((mid,text))
        return await asyncio.shield(batch['task'])

    async def flush_batch(self, key, batch):
        cfg = batch['cfg']; sender = key[1]
        try:
            await asyncio.sleep(self.BATCH_SECONDS)
            if batch['closed'] or self.config() != cfg:
                for mid,_ in batch['items']: self.status(cfg.instance,mid,'exited' if batch['closed'] else 'blocked')
                return {'status':'ignored'}
            self.batches.pop(key,None)
            mid = batch['items'][0][0]
            text = '\n'.join(t for _,t in batch['items'])
            result = await self.admit(cfg,mid,sender,text)
            for other,_ in batch['items'][1:]:
                self.db.execute('UPDATE whatsapp_inbox SET run=?,status=? WHERE instance=? AND message=?',
                    (result.get('run'), 'running' if result['status']=='accepted' else result['status'],cfg.instance,other))
            self.db.commit()
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            for mid,_ in batch['items']: self.status(cfg.instance,mid,'uncertain')
            raise AdapterError(503,'Task admission unconfirmed; check dashboard') from None
        finally:
            if self.batches.get(key) is batch: self.batches.pop(key,None)
            if batch['closed']:
                for mid,_ in batch['items']: self.status(cfg.instance,mid,'exited')

    async def busy_message(self, cfg, mid, sender, digest, text, active):
        question = text.strip().lower().rstrip('?! .').replace('’', "'")
        is_status = question in {"how's it going", 'how is it going', 'status', 'any update', 'any updates', 'are you done', 'done yet', 'progress'}
        state = active['status']
        answer = {'running': "I'm still working on your task.", 'waiting': "I'm waiting for your command approval.",
                  'cancelling': "The task is stopping; completed changes remain."}[state]
        pending = None
        if not is_status:
            # Only the latest proposal for this exact owner/chat remains confirmable.
            key=(cfg.instance,sender)
            self.revision_versions[key]=self.revision_versions.get(key,0)+1
            self.revisions.pop(key,None)
            if state in ('running','waiting') and len(text)<=6000:
                pending = dict(token=secrets.token_hex(6),rid=active['id'],cid=self.current_chat(cfg,sender),
                    text=text,cfg=cfg,deadline=time.monotonic()+300,ready=False,version=self.revision_versions[key])
                self.revisions[(cfg.instance,sender)] = pending
                answer += (" I haven't applied your correction. Reply REVISE "+pending['token']+
                    " within 5 minutes to stop this task and start a revised request in this chat. "
                    "Completed changes remain; the revised task must inspect current state, not replay earlier commands. "
                    "This confirms replacement only, not command approval. A newer correction replaces this proposal.")
            else:
                answer += " I haven't applied this message. Wait until stopping finishes, or send a shorter correction."

        self.db.execute('INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,?,?)',
            (cfg.instance,mid,sender,digest,time.time(),active['id'],'handled'))
        self.db.execute('INSERT INTO whatsapp_notes VALUES (?,?,?,?,?)',(cfg.instance,mid,active['id'],text,answer))
        self.db.commit()
        await self.deliver(cfg,mid,sender,'status' if is_status else 'correction',answer)
        if pending:
            receipt=self.db.execute("SELECT status FROM whatsapp_deliveries WHERE instance=? AND message=? AND kind='correction'",(cfg.instance,mid)).fetchone()
            pending['ready']=bool(receipt and receipt['status']=='sent')
        return {'status':'handled','run':active['id']}

    async def confirm_revision(self,cfg,mid,sender,digest,text):
        self.db.execute('INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,NULL,?)',
            (cfg.instance,mid,sender,digest,time.time(),'handled'));self.db.commit()
        pending=self.revisions.get((cfg.instance,sender))
        match=re.fullmatch(r'(?i)revise ([0-9a-f]{12})',text.strip())
        row=self.db.execute('SELECT status,chat FROM runs WHERE id=?',(pending['rid'],)).fetchone() if pending else None
        valid=bool(match and pending and pending['ready'] and pending['cfg']==cfg
            and hmac.compare_digest(match[1].lower(),pending['token'])
            and time.monotonic()<pending['deadline'] and row and row['status'] in ('running','waiting')
            and row['chat']==pending['cid'] and self.current_chat(cfg,sender)==pending['cid']
            and pending['cid'] not in self.closed_chats)
        if not valid:
            await self.deliver(cfg,mid,sender,'revision','That replacement confirmation is unavailable or stale. No task was stopped or started. Send a fresh correction if needed.')
            return {'status':'rejected'}
        self.revisions.pop((cfg.instance,sender),None)  # consume before the first await
        self.status(cfg.instance,mid,'reserved')
        try:
            await self.stop(pending['rid'])
            stopped=self.db.execute('SELECT status FROM runs WHERE id=?',(pending['rid'],)).fetchone()
            if (not stopped or stopped['status']!='cancelled' or self.config()!=cfg
                    or self.revision_versions.get((cfg.instance,sender))!=pending['version']
                    or self.current_chat(cfg,sender)!=pending['cid'] or pending['cid'] in self.closed_chats):
                raise AdapterError(409,'Replacement unavailable')
            replacement=('The user confirmed replacing the previous task after it stopped. Completed changes remain. '
                'First inspect the current state and prior outcomes; do not replay earlier commands. '
                'Apply the correction below to the previous request in this conversation. '
                'If its intent is unclear, ask before acting. This replacement is not command or sudo approval.\n'
                'User correction: '+pending['text'])
            result=await self.admit(cfg,mid,sender,replacement)
        except asyncio.CancelledError:
            self.status(cfg.instance,mid,'uncertain');raise
        except Exception:
            self.status(cfg.instance,mid,'uncertain')
            await self.deliver(cfg,mid,sender,'revision','I could not confirm the replacement. Check the task state before retrying; no automatic replay will occur.')
            return {'status':'uncertain'}
        if result['status']=='accepted':
            await self.deliver(cfg,mid,sender,'revision','The previous task stopped. Your correction is now the revised task request. Completed changes remain, and command approvals still apply.')
        return result

    async def admit(self, cfg, mid, sender, text):
        row = self.db.execute('SELECT chat FROM whatsapp_chats WHERE instance=? AND sender=?',
                              (cfg.instance, sender)).fetchone()
        if row:
            cid = row['chat']
        else:
            cid = uuid.uuid4().hex
            self.db.execute('INSERT INTO chats(id,title,created) VALUES (?,?,?)',
                            (cid, 'WhatsApp conversation', time.time()))
            self.db.execute('INSERT INTO whatsapp_chats VALUES (?,?,?)', (cfg.instance, sender, cid))
            self.db.commit()
            await self.deliver(cfg, mid, sender, 'session',
                'New conversation. The exit word is EXIT: send it alone to stop this conversation and clear its approval. '
                'Your first command approval permits ordinary commands in this conversation. Sudo commands always need separate approval. '
                'Approval also clears when Piko restarts.')
        if self.current_chat(cfg,sender) != cid:
            self.status(cfg.instance,mid,'exited')
            return {'status':'ignored'}
        try:
            rid = await self.submit(cid, text)
        except AdapterError as exc:
            # Consume even a busy request: a delayed provider retry must not execute it later.
            self.status(cfg.instance, mid, 'rejected')
            notice = asyncio.create_task(self.deliver(cfg, mid, sender, 'final', exc.message+' Send a new message when ready.'))
            self.monitors.add(notice)
            notice.add_done_callback(self.monitors.discard)
            return {'status': 'rejected', 'reason': exc.message}
        except Exception:
            self.status(cfg.instance, mid, 'uncertain')
            raise AdapterError(503, 'Task admission unconfirmed; check dashboard') from None
        self.db.execute("UPDATE whatsapp_inbox SET run=?,status='running' WHERE instance=? AND message=?",
                        (rid, cfg.instance, mid))
        self.db.commit()
        monitor = asyncio.create_task(self.watch(cfg, mid, sender, cid, rid))
        self.monitors.add(monitor)
        monitor.add_done_callback(self.monitors.discard)
        return {'status': 'accepted', 'run': rid}

    def current_chat(self, cfg, sender):
        row = self.db.execute('SELECT chat FROM whatsapp_chats WHERE instance=? AND sender=?',
                              (cfg.instance,sender)).fetchone()
        return row['chat'] if row else None

    def session_authorized(self, rid):
        if not self.command_approvals_enabled(): return False
        cfg = self.config()
        row = self.db.execute('SELECT r.chat,i.sender FROM runs r JOIN whatsapp_inbox i ON i.run=r.id WHERE r.id=? AND i.instance=?',
                              (rid,cfg.instance)).fetchone()
        return bool(row and row['sender'] in cfg.owners
                    and self.current_chat(cfg,row['sender']) == row['chat']
                    and self.session_grants.get(row['chat']) == (cfg,row['sender']))

    def forget_chat(self, cid):
        for key,pending in list(self.revisions.items()):
            if pending['cid']==cid: self.revisions.pop(key,None)
        self.session_grants.pop(cid,None)
        self.closed_chats.add(cid)
        for token,pending in list(self.pending_commands.items()):
            if pending.get('cid') == cid: self.pending_commands.pop(token,None)
        self.db.execute('DELETE FROM whatsapp_chats WHERE chat=?',(cid,))
        self.db.commit()

    async def exit_session(self, cfg, mid, sender, digest):
        self.db.execute('INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,NULL,?)',
                        (cfg.instance,mid,sender,digest,time.time(),'exited'))
        batch = self.batches.pop((cfg.instance,sender),None)
        if batch: batch['closed'] = True
        cid = self.current_chat(cfg,sender)
        self.db.commit()
        if cid:
            self.forget_chat(cid)
            rows = self.db.execute("SELECT DISTINCT r.id FROM runs r JOIN whatsapp_inbox i ON i.run=r.id WHERE r.chat=? AND i.instance=? AND i.sender=? AND r.status IN ('running','waiting','cancelling')",
                                   (cid,cfg.instance,sender)).fetchall()
            for row in rows:
                await self.stop(row['id'])
        await self.deliver(cfg,mid,sender,'exit',
                           'Conversation ended and approval cleared. Completed changes remain. Send a new message to begin another conversation.')
        return {'status':'accepted'}

    def command_approvals_enabled(self):
        return self.decide is not None and os.environ.get('PI_WHATSAPP_COMMAND_APPROVALS') == '1'

    def close_command(self, aid):
        for token, pending in list(self.pending_commands.items()):
            if pending['aid'] == aid:
                self.pending_commands.pop(token, None)

    async def request_command(self, rid, aid, command, reason, cwd, *, sudo=False):
        """Bind a one-use approval code to the originating owner and exact app gate."""
        if not self.command_approvals_enabled():
            return False
        cfg = self.config()
        origin = self.db.execute('SELECT message,sender FROM whatsapp_inbox WHERE instance=? AND run=?',
                                 (cfg.instance,rid)).fetchone()
        if not origin or origin['sender'] not in cfg.owners:
            return False
        token = secrets.token_hex(6)
        pending = dict(aid=aid,rid=rid,sender=origin['sender'],config=cfg,
                       cid=self.current_chat(cfg,origin['sender']),sudo=sudo,
                       deadline=time.time()+300,ready=False,chat=(cfg.instance,origin['sender']),not_before=None)
        self.pending_commands[token] = pending
        text = (('Sudo approval required for this command only' if sudo else 'Session approval required for ordinary commands')+' (expires in 5 minutes).\n'
                +'Command:\n'+command+'\nDirectory: '+cwd+'\nReason: '+reason
                +'\n\nReply approve to run this command, or deny to cancel it.'
                +(' Sudo always needs separate approval.' if sudo else ' Approval covers ordinary commands until EXIT or restart; sudo still needs separate approval.')
                +'\nOptional code form: APPROVE '+token+' or DENY '+token+'.')
        # Never authorize a command from a truncated preview.
        if len('Piko: '+text) > self.MAX_REPLY:
            self.pending_commands.pop(token,None)
            await self.deliver(cfg,origin['message'],origin['sender'],'command-'+aid,
                               'Command proposal is too long to approve here. It was declined; request a shorter command.')
            return False
        try:
            await self.deliver(cfg,origin['message'],origin['sender'],'command-'+aid,text)
            row = self.db.execute('SELECT status FROM whatsapp_deliveries WHERE instance=? AND message=? AND kind=?',
                (cfg.instance,origin['message'],'command-'+aid)).fetchone()
            pending['ready'] = bool(row and row['status']=='sent')
            # Provider timestamps are whole seconds. Require a later second so an
            # earlier/delayed generic reply cannot authorize this proposal.
            pending['not_before'] = int(time.time())+1
            if not pending['ready']:
                self.pending_commands.pop(token,None)
            return pending['ready']
        except BaseException:
            self.pending_commands.pop(token,None)
            raise

    async def command_decision(self, cfg, mid, sender, text, digest, sent_at=None):
        # Decisions bypass task admission/cooldown but retain authentication, owner
        # checks and durable message deduplication. They never reach the model.
        self.db.execute('INSERT INTO whatsapp_inbox VALUES (?,?,?,?,?,NULL,?)',
                        (cfg.instance,mid,sender,digest,time.time(),'approval_reserved'))
        self.db.commit()
        match = re.fullmatch(r'(approve|deny)(?: ([0-9a-f]{12}))?', text.strip(),re.I)
        token = match[2].lower() if match and match[2] else None
        plain = bool(match and token is None)
        if plain:
            candidates = [(code,item) for code,item in self.pending_commands.items()
                          if item['config']==cfg and item['sender']==sender
                          and item['chat']==(cfg.instance,sender) and time.time()<item['deadline']]
            if len(candidates)==1:
                token = candidates[0][0]
        pending = self.pending_commands.get(token) if token else None
        fresh = (not plain or (type(sent_at) is int and pending is not None
                 and pending['not_before'] is not None
                 and pending['not_before'] <= sent_at <= time.time()))
        valid = (self.command_approvals_enabled() and pending is not None
                 and pending['config']==cfg and pending['sender']==sender
                 and pending['chat']==(cfg.instance,sender)
                 and pending.get('cid') == self.current_chat(cfg,sender)
                 and pending['ready'] and time.time()<pending['deadline'] and fresh)
        approved = bool(match and match[1].lower()=='approve')
        message = ('No single current command matches that reply. Reply approve or deny after its proposal; '
                   'if several commands are pending, use the code on the intended command.')
        state = 'rejected'
        if valid:
            self.pending_commands.pop(token,None)
            try:
                await self.decide(pending['rid'],pending['aid'],approved)
                if approved and not pending.get('sudo') and pending.get('cid'):
                    self.session_grants[pending['cid']] = (cfg,sender)
                message = ('Command approved. Sudo commands still need separate approval; EXIT clears session approval.' if approved else 'Command declined.')
                state = 'accepted'
            except Exception:
                message = 'That command is no longer pending. Nothing was approved.'
        self.status(cfg.instance,mid,'approval_'+state)
        notice = asyncio.create_task(self.deliver(cfg,mid,sender,'decision',message))
        self.monitors.add(notice); notice.add_done_callback(self.monitors.discard)
        return {'status':state}

    def status(self, instance, mid, status):
        self.db.execute('UPDATE whatsapp_inbox SET status=? WHERE instance=? AND message=?',
                        (status, instance, mid))
        self.db.commit()

    def media_origin(self, rid, cfg):
        row = self.db.execute("""SELECT r.chat,i.sender,i.message FROM runs r
            JOIN run_channels c ON c.run=r.id JOIN whatsapp_inbox i ON i.run=r.id
            WHERE r.id=? AND c.channel='whatsapp' AND r.status='running'
            AND i.instance=? AND i.status='running'""", (rid,cfg.instance)).fetchone()
        if (not row or row['sender'] not in cfg.owners or row['chat'] in self.closed_chats
                or self.current_chat(cfg,row['sender']) != row['chat']):
            raise AdapterError(403, 'Media context unavailable')
        return dict(row)

    async def send_media(self, rid, file_path, media_type, caption, protected=(), secrets=()):
        from whatsapp_media import snapshot, transmit, SECRET_CONTENT, MediaRejected, safe_transport_diagnostic
        try:
            cfg = self.config()
            origin = self.media_origin(rid, cfg)
            private = (*secrets, cfg.api_key, cfg.relay_key)
            if (not isinstance(caption, str) or len(caption) > 1024
                    or (media_type == 'document' and caption)
                    or SECRET_CONTENT.search(caption.encode())
                    or any(value and value in caption for value in private)):
                return {'status': 'blocked'}
            payload = await asyncio.to_thread(snapshot, file_path, media_type, protected, private)
            # Revalidate after disk work and before reserving a single provider attempt.
            if self.config() != cfg or self.media_origin(rid, cfg) != origin:
                return {'status': 'blocked'}
        except MediaRejected as exc:
            return {'status': 'blocked', 'reason': exc.reason, 'source': 'local_validation'}
        except Exception:
            return {'status': 'blocked'}
        # Content-based, durable deduplication is intentionally independent of filename,
        # caption and inbound message. A model retry/new run cannot resend an uncertain upload.
        digest = hashlib.sha256(payload[0]).hexdigest()
        key = (cfg.instance, origin['sender'], digest)
        existing = self.db.execute('SELECT status FROM whatsapp_media_attempts WHERE instance=? AND sender=? AND digest=?', key).fetchone()
        if existing:
            state = existing['status']
            return {'status': 'uncertain' if state == 'sending' else state, 'duplicate_suppressed': True}
        reserved = self.db.execute('INSERT OR IGNORE INTO whatsapp_media_attempts VALUES (?,?,?,?)', (*key, 'sending'))
        self.db.commit()
        if not reserved.rowcount:
            return {'status': 'uncertain', 'duplicate_suppressed': True}
        status = 'uncertain'
        diagnostic_id = uuid.uuid4().hex
        diagnostics = {'category': 'not_completed', 'http_status': None}
        # Fixed-size operational metadata only: no recipients, filenames, bodies or errors.
        self.db.execute('INSERT INTO whatsapp_media_diagnostics VALUES (?,?,?,?,?,?)',
            (diagnostic_id, rid, time.time(), 'not_completed', None, status))
        self.db.commit()
        async def dispatch():
            if self.config() != cfg or self.media_origin(rid, cfg) != origin:
                diagnostics['category'] = 'context_changed'
                return 'blocked'
            return await transmit(origin['sender'], media_type, caption, payload, cfg,
                                  diagnostics=diagnostics)
        try:
            status = await asyncio.wait_for(dispatch(), 40)
        except asyncio.CancelledError:
            diagnostics['category'] = 'cancelled'
            raise
        except asyncio.TimeoutError:
            diagnostics['category'] = 'timeout'
        except Exception:
            diagnostics['category'] = 'transport_error'
        finally:
            category, http_status = safe_transport_diagnostic(diagnostics)
            self.db.execute('UPDATE whatsapp_media_attempts SET status=? WHERE instance=? AND sender=? AND digest=?', (status,*key))
            self.db.execute('UPDATE whatsapp_media_diagnostics SET category=?,http_status=?,outcome=? WHERE id=?',
                (category,http_status,status,diagnostic_id))
            self.db.commit()
        return {'status': status}

    async def deliver(self, cfg, mid, sender, kind, text):
        # At most one attempt. Network errors are ambiguous; never blindly resend.
        row = self.db.execute('SELECT 1 FROM whatsapp_deliveries WHERE instance=? AND message=? AND kind=?',
                              (cfg.instance, mid, kind)).fetchone()
        if row:
            return
        self.db.execute('INSERT INTO whatsapp_deliveries VALUES (?,?,?,?)', (cfg.instance, mid, kind, 'sending'))
        self.db.commit()
        status = 'uncertain'
        try:
            current = self.config()
            if current != cfg or sender not in current.owners:
                status = 'blocked'
                return
            text = 'Piko: '+text
            if len(text) > self.MAX_REPLY:
                text = 'Piko: This reply is too long for WhatsApp. Please open the private dashboard for the full details.'
            await asyncio.wait_for(self.reply(sender, text, cfg), 20)
            status = 'sent'
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not save provider responses, exception strings, credentials or request bodies.
            pass
        finally:
            self.db.execute('UPDATE whatsapp_deliveries SET status=? WHERE instance=? AND message=? AND kind=?',
                            (status, cfg.instance, mid, kind))
            self.db.commit()

    async def brief_result(self, rid, text):
        if len(text) <= 900: return text
        summary = None
        if self.summarize:
            try:
                summary = await asyncio.wait_for(self.summarize(rid,text),30)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        if not isinstance(summary,str) or not summary.strip() or len(summary)>900:
            summary = "I couldn't prepare a short summary of this result."
        return summary+'\nFull details are saved in the private dashboard. Ask me to send the full report if useful.'

    async def watch(self, cfg, mid, sender, cid, rid):
        try:
            started = time.monotonic()
            deadline = started+self.MAX_TASK_SECONDS
            last_progress = started
            progress_states = set()
            while True:
                row = self.db.execute('SELECT status FROM runs WHERE id=?', (rid,)).fetchone()
                state = row['status'] if row else 'failed'
                if state not in ('running', 'waiting', 'cancelling'):
                    break
                if state == 'waiting' and not self.command_approvals_enabled():
                    await self.deliver(cfg, mid, sender, 'approval',
                                       'This task needs command approval in your private Pi dashboard. WhatsApp replies cannot approve it.')
                if state not in progress_states and time.monotonic()-last_progress >= self.PROGRESS_SECONDS:
                    progress_states.add(state); last_progress = time.monotonic()
                    progress = {'running': "I'm still working on your task.", 'waiting': "I'm waiting for command approval before continuing.", 'cancelling': "The task is stopping. Completed changes remain."}[state]
                    if cid not in self.closed_chats:
                        await self.deliver(cfg,mid,sender,'progress-'+state,progress)
                if time.monotonic() >= deadline:
                    await self.stop(rid)
                    state = 'cancelled'
                    break
                await asyncio.sleep(self.POLL_SECONDS)
            if state in ('complete', 'partial'):
                # Bind the final response to this exact run, even if another task has completed.
                row = self.db.execute("SELECT m.text FROM messages m JOIN run_messages r ON r.message=m.id WHERE r.run=?",
                                      (rid,)).fetchone()
                answer = await self.brief_result(rid,row['text']) if row else 'Task finished; check the private dashboard.'
            else:
                answer = 'Task '+state+'. Check the private dashboard. Completed changes are not undone.'
            self.db.execute("UPDATE whatsapp_inbox SET status=? WHERE instance=? AND run=? AND status='running'",(state,cfg.instance,rid)); self.db.commit()
            if cid not in self.closed_chats:
                await self.deliver(cfg, mid, sender, 'final', answer)
        except asyncio.CancelledError:
            self.status(cfg.instance, mid, 'interrupted')
            raise
        except Exception:
            self.status(cfg.instance, mid, 'uncertain')

