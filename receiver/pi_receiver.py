"""Authenticated, opt-in FlareSend bridge to the private Piko adapter."""
import asyncio
import json
import hmac
import hashlib
import http.client
import re
import time
import os
from pathlib import Path

HOST = '127.0.0.1'
PORT = 8766
PATH = '/webhooks/flaresend'
HEADER_LIMIT = 8192
MAX_CONNECTIONS = 16
HEADER_TIMEOUT = 5
BODY_LIMIT = 16384


def response_for(method, target, authenticated=False, enabled=False):
    # Compare the raw request target: do not normalize slashes, escapes or dot segments.
    if target != PATH:
        return 404, {'accepted': False, 'error': 'not_found'}
    if method == 'POST' and authenticated:
        return 503, {'accepted': False, 'error': 'message_processing_not_enabled'}
    if method == 'POST':
        return 401, {'accepted': False, 'error': 'unauthorized'}
    if method in ('GET', 'HEAD') and enabled:
        return 200, {'service': 'Piko WhatsApp receiver', 'active': True}
    if method in ('GET', 'HEAD'):
        return 503, {'service': 'Piko WhatsApp receiver', 'active': False,
                     'status': 'message_processing_not_enabled'}
    return 405, {'accepted': False, 'error': 'method_not_allowed'}


def wire_response(status, payload, head=False):
    reasons = {200:'OK',403:'Forbidden',409:'Conflict',413:'Content Too Large',429:'Too Many Requests',502:'Bad Gateway',400:'Bad Request',401:'Unauthorized',404:'Not Found',405:'Method Not Allowed',
               408:'Request Timeout',431:'Request Header Fields Too Large',503:'Service Unavailable'}
    body = json.dumps(payload, separators=(',', ':')).encode()
    headers = (f'HTTP/1.1 {status} {reasons[status]}\r\n'
               'Content-Type: application/json\r\n'
               f'Content-Length: {len(body)}\r\n'
               'Cache-Control: no-store\r\n'
               'X-Content-Type-Options: nosniff\r\n'
               'Connection: close\r\n')
    if status == 405:
        headers += 'Allow: GET, HEAD, POST\r\n'
    return (headers+'\r\n').encode()+(b'' if head else body)


def load_token(name="flaresend-token", minimum=16):
    """Read only systemd's private credential copy; never log its value."""
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:
        raise RuntimeError('Receiver credential unavailable')
    try:
        with (Path(directory) / name).open('rb') as source:
            token = source.read(4097)
    except OSError:
        raise RuntimeError('Receiver credential unavailable') from None
    if not minimum <= len(token) <= 4096 or any(c <= 32 or c >= 127 for c in token):
        raise RuntimeError('Receiver credential invalid')
    return token


def authenticated_headers(raw, token):
    # Reject duplicates and malformed/folded headers; compare secrets in constant time.
    authorization = []
    for line in raw.split(b'\r\n')[1:-2]:
        if not line or line[:1] in (b' ', b'\t') or b':' not in line:
            return False
        name, value = line.split(b':', 1)
        if not name or name.strip() != name:
            return False
        if name.lower() == b'authorization':
            authorization.append(value.strip(b' \t'))
    if len(authorization) != 1:
        return False
    scheme, separator, supplied = authorization[0].partition(b' ')
    return (scheme.lower() == b'bearer' and bool(separator)
            and hmac.compare_digest(supplied, token))


class BridgeError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


class Bridge:
    """A narrow received-event contract with independent sender and self checks."""
    def __init__(self, instance, owners, connected_number, relay_key):
        phone = r'[1-9][0-9]{7,14}'
        if (not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', instance)
                or not owners or len(owners) > 10
                or not all(re.fullmatch(phone, owner) for owner in owners)
                or not re.fullmatch(phone, connected_number)
                or connected_number in owners or len(relay_key) < 32):
            raise RuntimeError('Bridge configuration incomplete or self sender allowed')
        self.instance, self.owners = instance, frozenset(owners)
        self.connected_number, self._key = connected_number, relay_key

    @classmethod
    def from_environment(cls):
        if os.environ.get('PI_WHATSAPP_PROCESSING_ENABLED') != '1':
            return None
        if os.environ.get('PI_WHATSAPP_VERIFIED_RELAY') != '1':
            raise RuntimeError('Verified provider contract required')
        return cls(os.environ.get('PI_WHATSAPP_INSTANCE', ''),
                   os.environ.get('PI_WHATSAPP_OWNERS', '').split(','),
                   os.environ.get('PI_WHATSAPP_CONNECTED_NUMBER', ''),
                   load_token('piko-relay-key', minimum=32))

    def normalize(self, body):
        if len(body) > BODY_LIMIT:
            raise BridgeError(413, 'message_too_large')
        try:
            event = json.loads(body, object_pairs_hook=unique_object)
            if not isinstance(event, dict):
                raise ValueError()
            if event.get('instanceId') != self.instance:
                raise BridgeError(403, 'instance_not_allowed')
            if not isinstance(event.get('event'), str):
                raise ValueError()
            if event['event'] != 'message_received':
                return None
            data = event['data']
            if not isinstance(data, dict):
                raise ValueError()
            sender = data['from']
            if not isinstance(sender, str):
                raise ValueError()
            if 'isGroup' in data and type(data['isGroup']) is not bool:
                raise ValueError()
            if sender.endswith('@g.us') or data.get('isGroup') is True:
                return None
            # message_received plus data.from identifies incoming direction in FlareSend.
            # If optional direction flags exist, validate them and reject contradictions.
            flags = [data['fromMe']] if 'fromMe' in data else []
            if 'key' in data:
                key = data['key']
                if not isinstance(key, dict):
                    raise ValueError()
                if 'fromMe' in key:
                    flags.append(key['fromMe'])
                if 'remoteJid' in key and key['remoteJid'] != sender:
                    raise ValueError()
                if 'id' in key and key['id'] != data.get('id'):
                    raise ValueError()
            if any(type(flag) is not bool for flag in flags) or len(set(flags)) > 1:
                raise ValueError()
            if flags and flags[0]:
                return None
            match = re.fullmatch(r'([1-9][0-9]{7,14})@s\.whatsapp\.net', sender)
            if not match:
                raise ValueError()
            sender = match[1]
            if sender == self.connected_number:
                return None
            if sender not in self.owners:
                raise BridgeError(403, 'sender_not_allowed')
            mid = data['id']
            text = data['message']['conversation']
            if (not isinstance(mid, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', mid)
                    or not isinstance(text, str) or not text.strip() or len(text) > 8000):
                raise ValueError()
            envelope = dict(version=1, event='message_received', instance=self.instance,
                            message_id=mid, sender=sender, text=text, from_self=False, is_group=False)
            if 'timestamp' in data:
                if type(data['timestamp']) is not int or data['timestamp'] <= 0:
                    raise ValueError()
                envelope['sent_at'] = data['timestamp']
            return envelope
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            raise BridgeError(400, 'unsupported_provider_payload') from None

    def forward(self, body, headers):
        # Fixed loopback destination, no proxy, redirects or automatic retries.
        connection = http.client.HTTPConnection('127.0.0.1', 8765, timeout=5)
        try:
            connection.request('POST', '/integrations/whatsapp/relay', body, headers)
            response = connection.getresponse()
            raw = response.read(BODY_LIMIT + 1)
            if len(raw) > BODY_LIMIT:
                raise ValueError()
            if response.status != 200:
                status = response.status if response.status in (400,401,403,409,413,429,503) else 502
                return status, {'accepted': False, 'error': 'adapter_rejected'}
            result = json.loads(raw)
            # Do not expose task output, internal run IDs or raw adapter errors publicly.
            state = result.get('status')
            if state not in ('accepted', 'duplicate', 'ignored', 'rejected'):
                raise ValueError()
            return 200, {'status': state}
        except (OSError, http.client.HTTPException, ValueError, AttributeError, RecursionError):
            return 502, {'accepted': False, 'error': 'adapter_unavailable'}
        finally:
            connection.close()

    async def accept(self, body):
        data = self.normalize(body)
        if data is None:
            return 200, {'status': 'ignored'}
        normalized = json.dumps(data, separators=(',', ':')).encode()
        stamp = str(int(time.time()))
        signature = hmac.new(self._key, stamp.encode()+b'.'+normalized, hashlib.sha256).hexdigest()
        headers = {'Content-Type': 'application/json', 'X-Pi-Relay-Timestamp': stamp,
                   'X-Pi-Relay-Signature': signature}
        return await asyncio.to_thread(self.forward, normalized, headers)


class Verification:
    """Time-limited inspection with no signing key, forwarding or reply capability."""
    def __init__(self, instance, owner, connected, deadline):
        if (not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', instance)
                or not re.fullmatch(r'[1-9][0-9]{7,14}', owner)
                or not re.fullmatch(r'[1-9][0-9]{7,14}', connected)
                or owner == connected or not 0 < deadline <= time.time()+1800):
            raise RuntimeError('Invalid verification configuration')
        self.instance, self.owners, self.connected_number = instance, frozenset([owner]), connected
        self.deadline, self.finished, self.auth_reported = deadline, False, False

    def unauthorized(self):
        if not self.auth_reported and time.time() < self.deadline:
            print('PIKO_VERIFY ' + json.dumps({'authenticated':False}), flush=True)
            self.auth_reported = True

    async def accept(self, body):
        if self.finished or time.time() >= self.deadline:
            return 503, {'accepted':False,'error':'verification_closed'}
        try:
            event = json.loads(body, object_pairs_hook=unique_object)
            data = event.get('data', {})
            # Inspect only the expressly authorized incoming sender and instance.
            if (event.get('instanceId') != self.instance
                    or event.get('event') != 'message_received'
                    or not isinstance(data, dict)
                    or data.get('from') not in {owner+'@s.whatsapp.net' for owner in self.owners}):
                return 200, {'status':'ignored'}
            key = data.get('key', {})
            message = data.get('message', {})
            report = {'authenticated':True, 'instance_matches':True, 'sender_matches':True,
                      'data_direction_present':'fromMe' in data,
                      'key_direction_present':isinstance(key,dict) and 'fromMe' in key,
                      'data_direction_boolean':type(data.get('fromMe')) is bool,
                      'key_direction_boolean':isinstance(key,dict) and type(key.get('fromMe')) is bool,
                      'conversation_text':isinstance(message,dict) and isinstance(message.get('conversation'),str),
                      'message_id_string':isinstance(data.get('id'),str)}
            try:
                normalized = Bridge.normalize(self, body)
                report['valid_incoming_task'] = normalized is not None
            except BridgeError:
                report['valid_incoming_task'] = False
            self.finished = True
            print('PIKO_VERIFY ' + json.dumps(report, sort_keys=True), flush=True)
            return 200, {'status':'verified_without_processing'}
        except (ValueError, AttributeError, TypeError, UnicodeError, RecursionError):
            return 400, {'accepted':False,'error':'invalid_verification_payload'}


def configured_bridge():
    if os.environ.get('PI_WHATSAPP_VERIFY_ONLY') == '1':
        if os.environ.get('PI_WHATSAPP_PROCESSING_ENABLED') == '1':
            raise RuntimeError('Verification cannot enable processing')
        return Verification(os.environ.get('PI_WHATSAPP_INSTANCE',''),
                            os.environ.get('PI_WHATSAPP_OWNERS',''),
                            os.environ.get('PI_WHATSAPP_CONNECTED_NUMBER',''),
                            float(os.environ.get('PI_WHATSAPP_VERIFY_UNTIL','0')))
    return Bridge.from_environment()


def body_length(raw):
    fields = {}
    for line in raw.split(b'\r\n')[1:-2]:
        name, value = line.split(b':', 1)
        name = name.lower()
        fields.setdefault(name, []).append(value.strip())
    lengths = fields.get(b'content-length', [])
    types = fields.get(b'content-type', [])
    if (b'transfer-encoding' in fields or b'content-encoding' in fields
            or len(lengths) != 1 or not re.fullmatch(rb'[0-9]{1,6}', lengths[0])
            or len(types) != 1 or types[0].lower() not in
            (b'application/json', b'application/json; charset=utf-8')):
        raise BridgeError(400, 'invalid_body_framing')
    length = int(lengths[0])
    if length > BODY_LIMIT:
        raise BridgeError(413, 'message_too_large')
    return length


class Receiver:
    def __init__(self, token, bridge=None, url_token=None):
        self.connections = 0
        self._token = token
        self.bridge = bridge
        if url_token is not None and not re.fullmatch(rb"[0-9a-f]{64}", url_token):
            raise RuntimeError("Invalid webhook URL credential")
        self._url_token = url_token

    async def handle(self, reader, writer):
        admitted = self.connections < MAX_CONNECTIONS
        if admitted:
            self.connections += 1
        try:
            if not admitted:
                writer.write(wire_response(503, {'accepted': False, 'error': 'busy'}))
            else:
                head = False
                try:
                    raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), HEADER_TIMEOUT)
                    if len(raw) > HEADER_LIMIT:
                        raise asyncio.LimitOverrunError('Header too large', len(raw))
                    line = raw.split(b'\r\n', 1)[0].decode('ascii')
                    method, target, version = line.split(' ')
                    if version not in ('HTTP/1.0', 'HTTP/1.1') or not target.startswith('/'):
                        raise ValueError('Invalid request line')
                    head = method == 'HEAD'
                    if self._url_token is not None:
                        # The raw path is a credential: never log it, redirect it, or normalize it.
                        expected = PATH.encode()+b'/'+self._url_token
                        authenticated = hmac.compare_digest(target.encode('ascii'), expected)
                        target = PATH if authenticated else '/not-found'
                    else:
                        authenticated = authenticated_headers(raw, self._token)
                    status, payload = response_for(method, target, authenticated, self.bridge is not None)
                    if method == 'POST' and target == PATH and not authenticated and isinstance(self.bridge, Verification):
                        self.bridge.unauthorized()
                    if method == 'POST' and target == PATH and authenticated and self.bridge is not None:
                        length = body_length(raw)
                        body = await asyncio.wait_for(reader.readexactly(length), HEADER_TIMEOUT)
                        status, payload = await self.bridge.accept(body)
                except BridgeError as exc:
                    status, payload = exc.status, {'accepted': False, 'error': exc.message}
                except (ValueError, UnicodeError, asyncio.IncompleteReadError):
                    status, payload = 400, {'accepted': False, 'error': 'invalid_request'}
                except asyncio.LimitOverrunError:
                    status, payload = 431, {'accepted': False, 'error': 'headers_too_large'}
                except TimeoutError:
                    status, payload = 408, {'accepted': False, 'error': 'request_timeout'}
                writer.write(wire_response(status, payload, head))
            await asyncio.wait_for(writer.drain(), 2)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            if admitted:
                self.connections -= 1
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (ConnectionError, TimeoutError):
                pass


async def main():
    url_mode = os.environ.get('PI_WHATSAPP_URL_AUTH') == '1'
    url_token = load_token('webhook-url-token', minimum=64) if url_mode else None
    receiver = Receiver(b'' if url_mode else load_token(), configured_bridge(), url_token)
    server = await asyncio.start_server(receiver.handle, HOST, PORT, limit=HEADER_LIMIT)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())

