"""Durable command outcomes and opt-in, bounded repository snapshots. Never rollback."""
import difflib
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

SECRET_LINE=re.compile(r'(?i)(?:api[_ -]?key|access[_ -]?key|secret|token|password|authorization)\s*[=:]|-----BEGIN |\bsk-[A-Za-z0-9_-]{16,}|\bAKIA[A-Z0-9]{16}')

def sanitize(text, secrets=()):
    text=str(text)
    for value in sorted((s for s in secrets if s),key=len,reverse=True): text=text.replace(value,'[redacted]')
    text=re.sub(r'-----BEGIN [\s\S]*?-----END [^-]+-----','[redacted key material]',text)
    return '\n'.join('[redacted sensitive line]' if SECRET_LINE.search(line) else line for line in text.split('\n'))

class Audit:
    def __init__(self,db):
        self.db=db
        db.execute('''CREATE TABLE IF NOT EXISTS command_audit(
            id TEXT PRIMARY KEY,run TEXT,worker TEXT,command TEXT,cwd TEXT,approval_source TEXT,
            started REAL,finished REAL,status TEXT,pid INTEGER,exit_code INTEGER,signal INTEGER,
            timed_out INTEGER,cancelled INTEGER,output_bytes INTEGER,stored_bytes INTEGER,
            truncated INTEGER,output TEXT,diff_scope TEXT,diff_coverage TEXT,diff TEXT)''')
        db.execute("UPDATE command_audit SET status='interrupted',finished=?,diff_coverage='Process outcome and file changes unconfirmed after restart' WHERE finished IS NULL",(time.time(),))
        db.commit()

    def start(self,rid,worker,command,cwd,source,secrets):
        cid=uuid.uuid4().hex
        self.db.execute('INSERT INTO command_audit(id,run,worker,command,cwd,approval_source,started,status) VALUES (?,?,?,?,?,?,?,?)',
            (cid,rid,worker,sanitize(command,secrets),sanitize(cwd,secrets),source,time.time(),'starting'))
        self.db.commit();return cid

    def running(self,cid,pid):
        self.db.execute("UPDATE command_audit SET status='running',pid=? WHERE id=?",(pid,cid));self.db.commit()

    def finish(self,cid,*,status,returncode,timed_out,cancelled,seen,output,truncated,scope,coverage,diff):
        self.db.execute('''UPDATE command_audit SET finished=?,status=?,exit_code=?,signal=?,timed_out=?,cancelled=?,
            output_bytes=?,stored_bytes=?,truncated=?,output=?,diff_scope=?,diff_coverage=?,diff=? WHERE id=?''',
            (time.time(),status,returncode if returncode is not None and returncode>=0 else None,
             -returncode if returncode is not None and returncode<0 else None,int(timed_out),int(cancelled),seen,
             len(output.encode()),int(truncated),output,scope,coverage,diff,cid));self.db.commit()

# Explicit scope only; never infer scope from arbitrary shell cwd or scan the Pi.
MAX_FILES=200
MAX_FILE_BYTES=65536
MAX_TOTAL_BYTES=1024*1024
MAX_DIFF_BYTES=32768
TEXT_EXT={'.py','.js','.ts','.tsx','.jsx','.css','.html','.md','.txt','.csv','.toml','.yaml','.yml','.json','.sh','.sql'}


def snapshot(scope,secrets=()):
    from whatsapp_media import sensitive
    result={};notes=[];total=0;visited=0;directories=0
    if not scope: return result,['No explicit audit workspace selected']
    root=Path(scope)
    if not root.is_absolute() or root.is_symlink() or str(root.resolve())!=str(root) or not root.is_dir() or sensitive(root,()):
        return result,['Audit workspace unavailable or excluded']
    # Directory fds and no-follow reads prevent following symlink swaps into secret trees.
    for directory,dirs,files,dirfd in os.fwalk(root,follow_symlinks=False):
        directories+=1
        if directories>200: return result,notes+['Directory limit reached; remaining paths excluded']
        if sys.platform=='linux':
            actual=os.readlink('/proc/self/fd/'+str(dirfd))
            if sensitive(actual,()) or not (actual==str(root) or actual.startswith(str(root)+'/')):
                dirs[:]=[];notes.append('Changed directory binding excluded');continue
        dirs[:]=sorted(d for d in dirs if not sensitive(Path(directory)/d,()) and d not in {'node_modules','venv','__pycache__','dist','build','data'})
        for name in sorted(files):
            visited+=1
            if visited>MAX_FILES: return result,notes+['File count limit reached; remaining files excluded']
            path=Path(directory)/name
            if sensitive(path,()) or path.suffix.lower() not in TEXT_EXT:
                notes.append('Sensitive or unsupported files excluded');continue
            fd=None
            try:
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=dirfd)
                before=os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_FILE_BYTES:
                    notes.append('Special, linked or oversized files excluded');continue
                if total+before.st_size>MAX_TOTAL_BYTES:
                    return result,notes+['Snapshot byte limit reached; remaining files excluded']
                with os.fdopen(os.dup(fd),'rb') as stream: raw=stream.read(MAX_FILE_BYTES+1)
                after=os.fstat(fd)
                if len(raw)!=before.st_size or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    notes.append('Changing files excluded');continue
                content=raw.decode('utf-8')
                if '\x00' in content or sanitize(content,secrets)!=content:
                    notes.append('Binary or detected sensitive content excluded');continue
                result[str(path.relative_to(root))]=content;total+=len(raw)
            except (OSError,UnicodeError): notes.append('Unreadable files excluded')
            finally:
                if fd is not None: os.close(fd)
    return result,sorted(set(notes))


def compare(before,after):
    old,old_notes=before;new,new_notes=after;parts=[];size=0
    # Never show the old content of a file newly excluded for secrets or size.
    # Additions/deletions cannot be distinguished safely from exclusions: mark them uncovered.
    for name in sorted(old.keys() & new.keys()):
        change=''.join(difflib.unified_diff(old[name].splitlines(True),new[name].splitlines(True),fromfile=name+' (before)',tofile=name+' (after)'))
        if size+len(change.encode())>MAX_DIFF_BYTES:
            parts.append('[Remaining diffs omitted: size limit]\n');break
        parts.append(change);size+=len(change.encode())
    notes=['Coverage: existing supported text files present in both snapshots only; no attribution guarantee; changes outside scope, added/deleted/excluded files and intermediate changes are not captured.']
    notes+=sorted(set(old_notes+new_notes))
    if old.keys()!=new.keys(): notes.append('File set changed or exclusions differ; added/deleted files not shown.')
    return ''.join(parts),' '.join(notes)

