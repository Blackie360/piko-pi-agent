"""Serialized command execution, audit and approval gates."""
import re
from .command_audit import sanitize, snapshot as audit_snapshot, compare as audit_compare
import asyncio, contextlib, os, signal, time, uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from agents import RunContextWrapper, function_tool
from . import config
from . import state

router = APIRouter()

async def shell(command, cwd, rid, timeout=120, worker_id=None):
    ctx = state.Context(rid, worker_id)
    if state.command_lock.locked():
        state.emit(ctx, 'info', text='Waiting for the current Pi command to finish…')
    async with state.command_lock:
        return await shell_unlocked(command, cwd, ctx, timeout)

async def shell_unlocked(command, cwd, ctx, timeout):
    private = state.private_values()
    command_id = state.COMMAND_AUDIT.start(ctx.rid, ctx.worker_id, command, cwd, state.approval_origin.get(), private)
    proc = None
    raw = bytearray()
    seen = 0
    timed_out = False
    cancelled = False
    status = 'failed'
    scope = None
    before = ({}, ['No explicit audit workspace selected'])
    diff = ''
    coverage = 'No explicit audit workspace selected'
    output = ''
    truncated = False
    try:
        path = Path(cwd).expanduser().resolve()
        if not path.is_dir():
            return 'Directory does not exist.'
        selected = state.DB.execute('SELECT workspace FROM run_workspaces WHERE run=?', (ctx.rid,)).fetchone()
        if selected:
            scope = selected['workspace']
            try:
                before = await asyncio.wait_for(asyncio.to_thread(audit_snapshot, scope, private), 2)
            except asyncio.TimeoutError:
                before = ({}, ['Pre-command snapshot timed out; coverage unavailable'])
        proc = await asyncio.create_subprocess_exec('/bin/bash', '-c', command, cwd=path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        state.COMMAND_AUDIT.running(command_id, proc.pid)
        deadline = time.monotonic() + timeout
        while True:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), max(0.01, deadline - time.monotonic()))
            if not chunk:
                break
            seen += len(chunk)
            raw.extend(chunk)
            if seen > 2000000:
                raise asyncio.TimeoutError()
        await asyncio.wait_for(proc.wait(), max(0.01, deadline - time.monotonic()))
        status = 'complete' if proc.returncode == 0 else 'failed'
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        timed_out = not cancelled and seen <= 2000000
        status = 'cancelled' if cancelled else 'timeout' if timed_out else 'output_limit'
        if proc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        if cancelled:
            raise
    except Exception:
        status = 'failed'
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        return 'Command could not complete. See its audit record.'
    finally:
        clean = sanitize(raw.decode(errors='replace'), private)
        encoded = clean.encode()
        truncated = len(encoded) > 24000 or seen > 2000000
        output = encoded[:24000].decode(errors='ignore')
        if scope:
            coverage = 'Post-command snapshot pending; changes unconfirmed'
        state.COMMAND_AUDIT.finish(command_id, status=status, returncode=proc.returncode if proc else None, timed_out=timed_out, cancelled=cancelled, seen=seen, output=output, truncated=truncated, scope=sanitize(scope, private) if scope else None, coverage=coverage, diff=sanitize(diff, private))
        if output:
            state.emit(ctx, 'output', text=output)
        state.emit(ctx, 'command_complete', command_id=command_id, status=status, exit_code=proc.returncode if proc else None, output_bytes=seen, truncated=truncated)
        if scope:
            try:
                if cancelled:
                    coverage = 'Post-command snapshot skipped after cancellation; changes unconfirmed'
                else:
                    after = await asyncio.wait_for(asyncio.to_thread(audit_snapshot, scope, private), 2)
                    diff, coverage = audit_compare(before, after)
            except asyncio.CancelledError:
                state.DB.execute('UPDATE command_audit SET diff_coverage=? WHERE id=?', ('Snapshot interrupted; command outcome already recorded', command_id))
                state.DB.commit()
                raise
            except Exception:
                coverage = 'Workspace snapshot failed or timed out; changes unconfirmed'
            state.DB.execute('UPDATE command_audit SET diff=?,diff_coverage=? WHERE id=?', (sanitize(diff, private), coverage, command_id))
            state.DB.commit()
    return ('Command stopped: time or output limit exceeded.' if status in ('timeout', 'output_limit') else 'Exit code: ' + str(proc.returncode)) + '\n' + output + ('\n[Output truncated; audit records observed byte count]' if truncated else '')

async def authorized_shell(command, cwd, rid, source):
    token = state.approval_origin.set(source)
    try:
        return await shell(command, cwd, rid)
    finally:
        state.approval_origin.reset(token)

def uses_sudo(command):
    return bool(re.search('(?<![A-Za-z0-9_])sudo(?![A-Za-z0-9_])', command))

async def approved_command(ctx, command, reason, cwd):
    rid = ctx.rid
    if ctx.worker_id:
        return 'Workers cannot execute shell commands. Return a proposal to the coordinator.'
    if not state.run_is_open(rid):
        return 'Task is no longer active. Command was not executed.'
    policy = config.settings()
    channel = state.DB.execute('SELECT channel FROM run_channels WHERE run=?', (rid,)).fetchone()
    human_approval = bool(channel and channel['channel'] == 'whatsapp')
    sudo = uses_sudo(command)
    if human_approval and (not sudo) and state.whatsapp.session_authorized(rid):
        state.event(rid, 'info', text='Session approved command: ' + command + '\nPurpose: ' + reason)
        return await authorized_shell(command, cwd, rid, 'whatsapp_session')
    if not sudo and (not human_approval) and policy.get('auto_commands'):
        state.event(rid, 'info', text='Automatically approved command: ' + command + '\nPurpose: ' + reason)
        return await authorized_shell(command, cwd, rid, 'dashboard_auto')
    aid = uuid.uuid4().hex
    future = asyncio.get_running_loop().create_future()
    state.gates[aid] = (rid, future)
    state.pending_command_details[aid] = {'rid': rid, 'command': command, 'reason': reason, 'cwd': cwd}
    state.event(rid, 'approval', approval_id=aid, command=command, reason=reason, cwd=cwd)
    state.sync_approval_status(rid)
    try:
        whatsapp_approval = human_approval and state.whatsapp.command_approvals_enabled()
        if whatsapp_approval:
            ready = await state.whatsapp.request_command(rid, aid, command, reason, cwd, sudo=sudo)
            if not ready and (not future.done()):
                state.event(rid, 'decision', approval_id=aid, approved=False)
                future.set_result(False)
        allowed = await asyncio.wait_for(future, 300 if whatsapp_approval else 3600)
    except asyncio.TimeoutError:
        state.event(rid, 'decision', approval_id=aid, approved=False)
        return 'Approval expired. Command was not executed.'
    except asyncio.CancelledError:
        state.event(rid, 'decision', approval_id=aid, approved=False, cancelled=True)
        raise
    finally:
        state.whatsapp.close_command(aid)
        state.gates.pop(aid, None)
        state.pending_command_details.pop(aid, None)
        state.sync_approval_status(rid)
    if not allowed:
        return 'User declined. Do not retry this command or equivalent actions.'
    if not state.run_is_open(rid):
        return 'Task is no longer active. Command was not executed.'
    return await authorized_shell(command, cwd, rid, ('whatsapp_explicit' if whatsapp_approval else 'dashboard_explicit') + ('_sudo' if sudo else ''))

@function_tool
async def execute_command(wrapper: RunContextWrapper[state.Context], command: str, reason: str, cwd: str=str(config.WORKSPACE)) -> str:
    """Propose and execute a shell command with the configured approval policy: commands are automatically approved when automatic execution is enabled; otherwise the configured sudo or human approval policy applies. Can edit files, install software, or manage Docker. Use sudo -n if authorized administration is needed. Time limit 120 seconds."""
    return await approved_command(wrapper.context, command, reason, cwd)

@function_tool
async def system_overview(wrapper: RunContextWrapper[state.Context]) -> str:
    """Read uptime, memory, disk space, container status and failed services without changing anything."""
    ctx = wrapper.context
    if not state.run_is_open(ctx.rid):
        return 'Task is no longer active.'
    state.emit(ctx, 'info', text='Checking the Pi’s health and running services…')
    token = state.approval_origin.set('fixed_health_check')
    try:
        return await shell('uptime; free -h; df -h /; docker ps --format "{{.Names}} {{.Status}}"; systemctl --failed --no-pager', str(config.WORKSPACE), ctx.rid, 30, ctx.worker_id)
    finally:
        state.approval_origin.reset(token)

class Decision(BaseModel):
    approved: bool

@router.post('/api/approvals/{aid}')
async def approve(aid: str, body: Decision):
    gate = state.gates.get(aid)
    if not gate or gate[1].done() or (not state.run_is_open(gate[0])):
        raise HTTPException(409, 'This approval is no longer pending')
    state.event(gate[0], 'decision', approval_id=aid, approved=body.approved)
    gate[1].set_result(body.approved)
    state.sync_approval_status(gate[0])
    return {'ok': True}
