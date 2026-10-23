"""Conservative local media snapshotting. This is not a secret detector or OS sandbox."""
import ctypes
import os
from pathlib import Path
import re
import stat
import struct
import sys
import zlib

import httpx

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 4_000_000
SENSITIVE = re.compile(r'(^|[._-])(env|credentials?|secrets?|tokens?|passwords?|passwd|shadow|private|id_rsa|id_ed25519|id_ecdsa|authorized_keys|known_hosts)([._-]|$)', re.I)
SECRET_CONTENT = re.compile(rb'-----BEGIN |(?:api[_ -]?key|access[_ -]?key|secret|token|password|authorization)\s*[=:]|\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})', re.I)
BLOCKED = ('/proc', '/sys', '/dev', '/etc', '/run', '/var/run', '/var/lib', '/boot', '/root')

class MediaRejected(Exception):
    # Only fixed categories may reach the model; never paths, file bytes or exception text.
    REASONS = frozenset({'invalid_file', 'invalid_png', 'invalid_png_sbit',
        'unsupported_png_chunk', 'unsupported_type', 'unsafe_path', 'unsafe_file',
        'file_size_limit', 'file_changed', 'sensitive_content', 'file_unavailable'})

    def __init__(self, reason='invalid_file'):
        self.reason = reason if reason in self.REASONS else 'invalid_file'
        super().__init__(self.reason)


def sensitive(path, protected):
    path = os.path.normpath(str(path))
    if sys.platform == 'darwin' and path.startswith('/private/'):
        path = path[len('/private'):]
        protected = tuple(root[len('/private'):] if root.startswith('/private/') else root for root in protected)
    parts = Path(path).parts[1:]
    return (any(p.startswith('.') or SENSITIVE.search(p) or p.lower().endswith(
        ('.pem', '.key', '.p12', '.pfx', '.keystore', '.db', '.sqlite', '.sqlite3')) for p in parts)
        or any(path == root or path.startswith(root.rstrip('/') + '/') for root in (*BLOCKED, *protected)))


def check_filesystem(fd, path, protected):
    if sys.platform != 'linux':
        return  # Development host; Pi deployment uses the Linux checks below.
    # Permit ordinary disk/network filesystems, not procfs/sysfs/debugfs/FUSE sources.
    buf = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(fd, buf) != 0:
        raise MediaRejected()
    magic = ctypes.c_long.from_buffer(buf).value & 0xffffffff
    if magic not in {0xef53, 0x9123683e, 0x58465342, 0xf2f52010, 0x01021994,
                     0x794c7630, 0x6969, 0xff534d42, 0x4d44, 0x2011bab0}:
        raise MediaRejected()
    # A bind mount may give a sensitive tree an innocuous pathname.
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields = left.split()
        root, mount = map(unescape, (fields[3], fields[4]))
        if path == mount or path.startswith(mount.rstrip('/') + '/'):
            source = unescape(right.split()[1])
            # Root / is normal; non-root mount source paths must pass the same policy.
            if (root != '/' and sensitive(root, protected)) or SENSITIVE.search(source):
                raise MediaRejected()


def png_valid(data):
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise MediaRejected('invalid_png')
    pos, chunks, compressed, ended = 8, [], bytearray(), False
    while pos < len(data):
        if pos + 12 > len(data): raise MediaRejected('invalid_png')
        size, kind = struct.unpack('>I4s', data[pos:pos+8])
        end = pos + 12 + size
        if end > len(data): raise MediaRejected('invalid_png')
        body = data[pos+8:end-4]
        if zlib.crc32(kind + body) & 0xffffffff != struct.unpack('>I', data[end-4:end])[0]:
            raise MediaRejected('invalid_png')
        if kind == b'IHDR' and not chunks and size == 13:
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', body)
            if not width or not height or width*height > MAX_PIXELS or depth != 8 or color not in (2, 6) or compression or filtering or interlace:
                raise MediaRejected('invalid_png')
            stride = width * (3 if color == 2 else 4) + 1
        elif kind == b'sBIT':
            # PNG 11.3.2.4: once, after IHDR and before IDAT (PLTE is unsupported).
            if (chunks != [b'IHDR'] or size != (3 if color == 2 else 4)
                    or any(value < 1 or value > depth for value in body)):
                raise MediaRejected('invalid_png_sbit')
        elif kind == b'IDAT' and chunks and chunks[-1] in (b'IHDR', b'sBIT', b'IDAT'):
            compressed.extend(body)
        elif kind == b'IEND' and size == 0 and chunks and chunks[-1] == b'IDAT' and end == len(data):
            ended = True
        else:
            # Keep all other metadata, unknown chunks and animation unsupported.
            raise MediaRejected('unsupported_png_chunk')
        chunks.append(kind)
        pos = end
    if not ended: raise MediaRejected('invalid_png')
    decoder = zlib.decompressobj()
    expected = stride * height
    raw = decoder.decompress(bytes(compressed), expected + 1)
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise MediaRejected('invalid_png')
    if any(raw[i] > 4 for i in range(0, len(raw), stride)): raise MediaRejected('invalid_png')


def snapshot(file_path, media_type, protected=(), secrets=()):
    """Open every component without symlink traversal, read once, send immutable bytes."""
    try:
        if media_type not in ('image', 'document') or not isinstance(file_path, str) or len(file_path) > 4096:
            raise MediaRejected('unsupported_type')
        p = Path(file_path)
        if not p.is_absolute() or '..' in p.parts or '\x00' in file_path or sensitive(p, protected):
            raise MediaRejected('unsafe_path')
        if str(p.resolve(strict=True)) != str(p): raise MediaRejected('unsafe_path')
        extension = p.suffix.lower()
        if extension not in ({'.png'} if media_type == 'image' else {'.txt', '.csv'}): raise MediaRejected('unsupported_type')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in p.parts[1:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            source = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                before = os.fstat(source)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise MediaRejected('unsafe_file')
                if not 0 < before.st_size <= MAX_BYTES:
                    raise MediaRejected('file_size_limit')
                check_filesystem(source, str(p), protected)
                with os.fdopen(os.dup(source), 'rb') as stream:
                    data = stream.read(MAX_BYTES + 1)
                after = os.fstat(source)
                signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
                if signature(before) != signature(after) or len(data) != before.st_size:
                    raise MediaRejected('file_changed')
                if p.stat(follow_symlinks=False) != after or str(p.resolve(strict=True)) != str(p):
                    raise MediaRejected('file_changed')
            finally:
                os.close(source)
        finally:
            os.close(fd)
        if SECRET_CONTENT.search(data) or any(s and s.encode() in data for s in secrets): raise MediaRejected('sensitive_content')
        if media_type == 'image':
            png_valid(data)
            mime, name = 'image/png', 'image.png'
        else:
            text = data.decode('utf-8')
            if any(ord(c) < 32 and c not in '\r\n\t' for c in text) or '\x7f' in text: raise MediaRejected()
            mime, name = ('text/csv', 'document.csv') if extension == '.csv' else ('text/plain', 'document.txt')
        return data, mime, name
    except (OSError, ValueError, UnicodeError, zlib.error, struct.error):
        raise MediaRejected('file_unavailable') from None


TRANSPORT_CATEGORIES = frozenset({'not_completed', 'accepted', 'provider_rejected',
    'http_unconfirmed', 'malformed_response', 'acceptance_unconfirmed',
    'timeout', 'network_error', 'transport_error', 'context_changed', 'cancelled'})


def safe_transport_diagnostic(diagnostic):
    category = diagnostic.get('category')
    status = diagnostic.get('http_status')
    return (category if isinstance(category, str) and category in TRANSPORT_CATEGORIES else 'transport_error',
            status if type(status) is int and 100 <= status <= 599 else None)


async def transmit(recipient, media_type, caption, payload, cfg, diagnostics=None):
    """Only server code supplies recipient/config. Never expose provider bodies or errors."""
    def record(category, http_status=None):
        if diagnostics is not None:
            diagnostics.update(category=category, http_status=http_status)
    record('not_completed')
    data, mime, name = payload
    fields = {'recipients[]': recipient, 'type': media_type}
    if caption: fields['caption'] = caption
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.post('https://api.flaresend.com/send-message',
                headers={'Authorization': 'Bearer '+cfg.api_key}, data=fields,
                files={'file': (name, data, mime)})
        if response.status_code in (400, 401, 403, 413, 415, 422):
            record('provider_rejected', response.status_code)
            return 'provider_error'
        if response.status_code != 200:
            record('http_unconfirmed', response.status_code)
            return 'uncertain'
        try:
            body = response.json()
        except ValueError:
            record('malformed_response', response.status_code)
            return 'uncertain'
        record('acceptance_unconfirmed', response.status_code)
        if (isinstance(body, dict) and body.get('success') is True
                and body.get('failed_recipients') == [] and body.get('invalid_recipients', []) == []
                and isinstance(body.get('successful_recipients'), list)
                and recipient+'@s.whatsapp.net' in body['successful_recipients']):
            record('accepted', response.status_code)
            return 'accepted_by_provider'
    except httpx.TimeoutException:
        record('timeout')
    except httpx.RequestError:
        record('network_error')
    except Exception:
        record('transport_error')
    return 'uncertain'

