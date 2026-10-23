"""HTTP application assembly, authentication and dashboard routes."""
import asyncio, hashlib, hmac, json, os, secrets, time, uuid
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
from . import config
from . import state
from . import commands
from . import agent
from . import voice

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.mount('/static', StaticFiles(directory=config.BASE / 'frontend' / 'assets'), name='static')

@app.middleware('http')
async def security(request: Request, call_next):
    if request.method != 'GET' and (not (config.WHATSAPP_ROUTE_ENABLED and request.url.path == '/integrations/whatsapp/relay')):
        if request.headers.get('x-pi-agent') != '1':
            return JSONResponse({'detail': 'Request rejected'}, 403)
        origin = request.headers.get('origin')
        if origin and origin not in config.ALLOWED_ORIGINS:
            return JSONResponse({'detail': 'Origin rejected'}, 403)
    if request.url.path.startswith('/api/') and request.url.path != '/api/login':
        token = request.cookies.get('pi_session', '')
        row = state.DB.execute('SELECT expires FROM sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['expires'] < time.time():
            return JSONResponse({'detail': 'Please sign in'}, 401)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response

@app.get('/')
async def index():
    return FileResponse(config.BASE / 'frontend' / 'index.html')

@app.get('/api/metrics')
async def metrics():
    now = time.time()
    since = now - 86400
    totals = dict(state.DB.execute('SELECT COALESCE(SUM(input_tokens),0) input,COALESCE(SUM(output_tokens),0) output,COALESCE(SUM(total_tokens),0) total,COALESCE(SUM(requests),0) requests FROM usage').fetchone())
    today = dict(state.DB.execute('SELECT COALESCE(SUM(total_tokens),0) tokens,COALESCE(SUM(requests),0) requests FROM usage WHERE created>=?', (since,)).fetchone())
    models = [dict(r) for r in state.DB.execute('SELECT model,SUM(input_tokens) input,SUM(output_tokens) output,SUM(total_tokens) total,SUM(requests) requests FROM usage GROUP BY model ORDER BY total DESC')]
    tasks = [dict(r) for r in state.DB.execute('SELECT status,COUNT(*) count FROM runs GROUP BY status')]
    duration = state.DB.execute("SELECT AVG(duration) FROM task_metrics WHERE status='complete'").fetchone()[0]
    recent = [dict(r) for r in state.DB.execute('SELECT created,model,duration,status FROM task_metrics ORDER BY created DESC LIMIT 8')]
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        k, v = line.split(':', 1)
        memory[k] = int(v.strip().split()[0]) * 1024
    disk = os.statvfs('/')
    total = disk.f_blocks * disk.f_frsize
    available = disk.f_bavail * disk.f_frsize
    return {'totals': totals, 'last24h': today, 'models': models, 'tasks': tasks, 'average_seconds': duration, 'recent': recent, 'pi': {'load': list(os.getloadavg()), 'cpus': os.cpu_count(), 'memory_total': memory['MemTotal'], 'memory_used': memory['MemTotal'] - memory['MemAvailable'], 'disk_total': total, 'disk_used': total - disk.f_bfree * disk.f_frsize, 'uptime_seconds': float(Path('/proc/uptime').read_text().split()[0])}, 'active': len(state.active), 'tracking_since': state.DB.execute('SELECT MIN(created) FROM usage').fetchone()[0]}

class Login(BaseModel):
    password: str

@app.post('/api/login')
async def login(body: Login, request: Request):
    ip = request.client.host
    recent = [t for t in state.attempts.get(ip, []) if t > time.time() - 300]
    if len(recent) >= 10:
        raise HTTPException(429, 'Too many attempts. Try again in five minutes.')
    if not hmac.compare_digest(body.password, state.PASSWORD):
        state.attempts[ip] = recent + [time.time()]
        raise HTTPException(401, 'Incorrect access code')
    token = secrets.token_urlsafe(32)
    state.DB.execute('INSERT INTO sessions VALUES (?,?)', (hashlib.sha256(token.encode()).hexdigest(), time.time() + 86400))
    state.DB.commit()
    response = JSONResponse({'ok': True})
    response.set_cookie('pi_session', token, secure=True, httponly=True, samesite='strict', max_age=86400)
    return response

@app.post('/api/logout')
async def logout(request: Request):
    state.DB.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(request.cookies.get('pi_session', '').encode()).hexdigest(),))
    state.DB.commit()
    r = JSONResponse({'ok': True})
    r.delete_cookie('pi_session')
    return r

def conversation_label(row):
    """Presentation only: never rename stored conversations or meaningful titles."""
    title = row['title'] or 'New conversation'
    if title not in ('New conversation', 'WhatsApp conversation'):
        return title
    first = ' '.join((row['first_request'] or '').split())
    if first:
        return ('WhatsApp · ' if title == 'WhatsApp conversation' else '') + first[:72] + ('…' if len(first) > 72 else '')
    return ('WhatsApp' if title == 'WhatsApp conversation' else 'Empty conversation') + ' · ' + row['id'][:8]

def conversation_list():
    rows = state.DB.execute("SELECT c.id,c.title,(SELECT m.text FROM messages m WHERE m.chat=c.id AND m.role='user' ORDER BY m.id LIMIT 1) AS first_request FROM chats c ORDER BY c.created DESC")
    return [dict(id=r['id'], title=r['title'], display_title=conversation_label(r)) for r in rows]

class PersonaChoice(BaseModel):
    persona: str

@app.post('/api/persona')
async def choose_persona(body: PersonaChoice):
    if body.persona not in config.PERSONAS:
        raise HTTPException(400, 'Choose a listed tone.')
    if state.active:
        raise HTTPException(409, 'Wait for the current task before changing tone.')
    cfg = config.settings()
    cfg['persona'] = body.persona
    temp = config.DATA / 'settings.tmp'
    temp.write_text(json.dumps(cfg))
    temp.replace(config.DATA / 'settings.json')
    return {'ok': True, 'persona': body.persona}

@app.get('/api/state')
async def get_state():
    s = config.settings()
    return {'persona': s.get('persona', 'warm'), 'personas': [{'id': key, 'label': value[0]} for key, value in config.PERSONAS.items()], 'configured': bool(s.get('key')), 'model': s['model'], 'auto_sudo': bool(s.get('auto_sudo')), 'auto_commands': bool(s.get('auto_commands')), 'models': config.MODEL_OPTIONS, 'chats': conversation_list(), 'active': list(state.active.keys()), 'active_sessions': [dict(r) for r in state.DB.execute("SELECT r.chat,COALESCE(c.channel,'dashboard') AS channel FROM runs r LEFT JOIN run_channels c ON c.run=r.id WHERE r.status IN ('running','waiting','cancelling')")]}

class Settings(BaseModel):
    key: str = Field(min_length=1, max_length=1000)
    model: str = Field(default='gpt-6.1-sol', min_length=1, max_length=100)

@app.post('/api/settings')
async def configure(body: Settings):
    if state.active:
        raise HTTPException(409, 'Wait for the current task to finish')
    body.key = body.key.strip()
    body.model = body.model.strip()
    try:
        async with AsyncOpenAI(api_key=body.key, timeout=15, max_retries=0) as c:
            await c.models.retrieve(body.model)
    except Exception as exc:
        raise HTTPException(400, config.api_error(exc))
    if state.active:
        raise HTTPException(409, 'Wait for the current task to finish')
    p = config.DATA / 'settings.json'
    temp = config.DATA / 'settings.tmp'
    temp.write_text(json.dumps({**config.settings(), 'key': body.key, 'model': body.model}))
    temp.replace(p)
    return {'ok': True}

class ModelChoice(BaseModel):
    model: str = Field(min_length=1, max_length=100)

@app.post('/api/model')
async def choose_model(body: ModelChoice):
    if state.active:
        raise HTTPException(409, 'Wait for the current task to finish before switching models.')
    if body.model not in {m['id'] for m in config.MODEL_OPTIONS}:
        raise HTTPException(400, 'Choose a model from the list.')
    cfg = config.settings()
    if not cfg.get('key'):
        raise HTTPException(400, 'Add your API key in Settings first.')
    try:
        async with AsyncOpenAI(api_key=cfg['key'], timeout=20, max_retries=0) as c:
            await c.models.retrieve(body.model)
    except Exception as exc:
        raise HTTPException(400, config.api_error(exc))
    if state.active:
        raise HTTPException(409, 'A task started. Wait for it to finish before switching models.')
    cfg['model'] = body.model
    temp = config.DATA / 'settings.tmp'
    temp.write_text(json.dumps(cfg))
    temp.replace(config.DATA / 'settings.json')
    return {'ok': True, 'model': cfg['model']}

@app.post('/api/chats')
async def new_chat():
    cid = uuid.uuid4().hex
    state.DB.execute('INSERT INTO chats(id,title,created) VALUES (?,?,?)', (cid, 'New conversation', time.time()))
    state.DB.commit()
    return {'id': cid}

@app.delete('/api/chats/{cid}')
async def delete_chat(cid: str):
    if not state.DB.execute('SELECT id FROM chats WHERE id=?', (cid,)).fetchone():
        raise HTTPException(404)
    if state.DB.execute("SELECT 1 FROM runs WHERE chat=? AND status IN ('running','waiting','cancelling')", (cid,)).fetchone():
        raise HTTPException(409, 'Stop this conversation before deleting it.')
    state.whatsapp.forget_chat(cid)
    with state.DB:
        for table in ('events', 'workers', 'usage', 'task_metrics', 'run_channels', 'run_messages', 'whatsapp_notes'):
            state.DB.execute(f'DELETE FROM {table} WHERE run IN (SELECT id FROM runs WHERE chat=?)', (cid,))
        state.DB.execute('DELETE FROM runs WHERE chat=?', (cid,))
        state.DB.execute('DELETE FROM messages WHERE chat=?', (cid,))
        state.DB.execute('DELETE FROM chats WHERE id=?', (cid,))
    return {'ok': True}

@app.get('/api/chats/{cid}')
async def chat(cid: str):
    if not state.DB.execute('SELECT id FROM chats WHERE id=?', (cid,)).fetchone():
        raise HTTPException(404)
    runs = [dict(r) for r in state.DB.execute('SELECT id,status FROM runs WHERE chat=? ORDER BY rowid', (cid,))]
    for r in runs:
        answer = state.DB.execute('SELECT m.text FROM messages m JOIN run_messages rm ON rm.message=m.id WHERE rm.run=?', (r['id'],)).fetchone()
        r['answer'] = answer['text'] if answer else None
        r['events'] = [{'id': x['id'], 'kind': x['kind'], **json.loads(x['payload'])} for x in state.DB.execute('SELECT * FROM events WHERE run=? ORDER BY id', (r['id'],))]
        r['commands'] = [dict(x) for x in state.DB.execute('SELECT * FROM command_audit WHERE run=? ORDER BY started', (r['id'],))]
        for item in r['events']:
            detail = state.pending_command_details.get(item.get('approval_id'))
            if detail and item.get('kind') == 'approval' and (detail['rid'] == r['id']):
                item.update({k: detail[k] for k in ('command', 'reason', 'cwd')})
        r['workers'] = [dict(w) for w in state.DB.execute('SELECT * FROM workers WHERE run=? ORDER BY slot', (r['id'],))]
    return {'messages': [dict(r) for r in state.DB.execute('SELECT id,role,text FROM messages WHERE chat=? ORDER BY id', (cid,))], 'runs': runs}

@app.get('/api/devices')
async def devices():
    proc = await asyncio.create_subprocess_exec('tailscale', 'status', '--json', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(503, 'Tailscale took too long to respond. Try again.')
    if proc.returncode:
        raise HTTPException(503, 'Could not read the Tailscale device list.')
    try:
        status = json.loads(stdout)
    except ValueError:
        raise HTTPException(503, 'Tailscale returned an unreadable device list.')
    rows = []
    for own, peer in [(True, status.get('Self', {}))] + [(False, p) for p in status.get('Peer', {}).values()]:
        if not peer:
            continue
        online = status.get('BackendState') == 'Running' if own else bool(peer.get('Online'))
        seen = peer.get('LastSeen', '')
        rows.append({'name': peer.get('HostName') or peer.get('DNSName', '').split('.')[0], 'hostname': peer.get('DNSName', '').rstrip('.'), 'os': peer.get('OS', 'Unknown'), 'ip': next((ip for ip in peer.get('TailscaleIPs', []) if ':' not in ip), ''), 'online': online, 'last_seen': seen if seen and (not seen.startswith('0001-')) else None, 'is_pi': own})
    rows.sort(key=lambda d: (not d['is_pi'], not d['online'], d['name'].lower()))
    return {'devices': rows, 'checked_at': time.time(), 'connected': status.get('BackendState') == 'Running'}

if config.WHATSAPP_ROUTE_ENABLED:
    app.add_api_route('/integrations/whatsapp/relay', agent.whatsapp_relay, methods=['POST'])

app.include_router(commands.router)
app.include_router(agent.router)
app.include_router(voice.router)
