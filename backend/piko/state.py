"""Single-process persistent state and task/event bookkeeping."""
import contextvars
from .command_audit import Audit, sanitize
import asyncio, json, secrets, sqlite3, time
from dataclasses import dataclass, field
from openai import AsyncOpenAI
from . import config

SECRET = config.DATA / 'login-token'

if not SECRET.exists():
    SECRET.write_text(secrets.token_urlsafe(24))

PASSWORD = SECRET.read_text().strip()

DB = sqlite3.connect(config.DATA / 'agent.db', check_same_thread=False)

DB.row_factory = sqlite3.Row

DB.executescript("CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,title TEXT,created REAL,history TEXT DEFAULT '[]');\nCREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,chat TEXT,role TEXT,text TEXT);\nCREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,chat TEXT,status TEXT);\nCREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,run TEXT,kind TEXT,payload TEXT);\nCREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,expires REAL);")

DB.execute("UPDATE runs SET status='interrupted' WHERE status IN ('running','waiting','cancelling')")

DB.execute('CREATE TABLE IF NOT EXISTS workers(\n    id TEXT PRIMARY KEY, run TEXT NOT NULL, slot INTEGER NOT NULL, task TEXT NOT NULL,\n    status TEXT NOT NULL, result TEXT, error TEXT, started REAL, finished REAL)')

DB.execute("UPDATE workers SET status='interrupted',finished=? WHERE status IN ('queued','running')", (time.time(),))

DB.execute('CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY,created REAL,run TEXT,kind TEXT,model TEXT,input_tokens INTEGER,output_tokens INTEGER,total_tokens INTEGER,requests INTEGER,duration REAL)')

DB.execute('CREATE TABLE IF NOT EXISTS task_metrics(run TEXT PRIMARY KEY,model TEXT,duration REAL,status TEXT,created REAL)')

DB.execute('CREATE TABLE IF NOT EXISTS run_channels(run TEXT PRIMARY KEY,channel TEXT NOT NULL)')

DB.execute('CREATE TABLE IF NOT EXISTS run_workspaces(run TEXT PRIMARY KEY,workspace TEXT NOT NULL)')

DB.execute('CREATE TABLE IF NOT EXISTS run_messages(run TEXT PRIMARY KEY,message INTEGER NOT NULL)')

DB.commit()

COMMAND_AUDIT = Audit(DB)

approval_origin = contextvars.ContextVar('approval_origin', default='internal')

active = {}

gates = {}

pending_command_details = {}

runtimes = {}

command_lock = asyncio.Lock()

RUNNING_STATUSES = ('running', 'waiting', 'cancelling')

attempts = {}

def track_usage(rid, kind, model, usage, duration):
    DB.execute('INSERT INTO usage(created,run,kind,model,input_tokens,output_tokens,total_tokens,requests,duration) VALUES (?,?,?,?,?,?,?,?,?)', (time.time(), rid, kind, model, int(getattr(usage, 'input_tokens', 0)), int(getattr(usage, 'output_tokens', 0)), int(getattr(usage, 'total_tokens', 0)), int(getattr(usage, 'requests', 1)), duration))
    DB.commit()

def private_values():
    values = [PASSWORD, config.settings().get('key', '')]
    try:
        cfg = whatsapp.config()
        values.extend([cfg.api_key, cfg.relay_key])
    except Exception:
        pass
    return tuple(values)

def event(rid, kind, **payload):
    values = private_values()
    payload = {key: sanitize(value, values) if isinstance(value, str) else value for key, value in payload.items()}
    DB.execute('INSERT INTO events(run,kind,payload) VALUES (?,?,?)', (rid, kind, json.dumps(payload)))
    DB.commit()

def run_status(rid, status):
    DB.execute('UPDATE runs SET status=? WHERE id=?', (status, rid))
    DB.commit()

@dataclass
class Context:
    rid: str
    worker_id: str | None = None

@dataclass
class Runtime:
    client: AsyncOpenAI
    model: str
    delegated: bool = False
    children: set = field(default_factory=set)

def emit(ctx, kind, **payload):
    if ctx.worker_id:
        payload['worker_id'] = ctx.worker_id
    event(ctx.rid, kind, **payload)

def run_is_open(rid):
    row = DB.execute('SELECT status FROM runs WHERE id=?', (rid,)).fetchone()
    return bool(row and row['status'] in ('running', 'waiting'))

def sync_approval_status(rid):
    if run_is_open(rid):
        waiting = any((owner == rid and (not future.done()) for owner, future in gates.values()))
        run_status(rid, 'waiting' if waiting else 'running')

# Adapter is wired once by agent.py after its callback functions are defined.
whatsapp = None
