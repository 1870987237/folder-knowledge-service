"""Bounded UTF-8 fragments and process-local, scope-bound continuation tokens."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from .errors import KnowledgeError


_CURSOR_SECRET = secrets.token_bytes(32)
_NEWLINE = re.compile(rb'\r\n|\r|\n')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def file_version(st):
    return 'stat-v1-' + fingerprint([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns])


def encode_cursor(scope, state):
    raw = json.dumps({'scope': scope, 'state': state}, ensure_ascii=True, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(hmac.digest(_CURSOR_SECRET, raw, 'sha256') + raw).decode().rstrip('=')


def decode_cursor(token, scope):
    try:
        if len(token) > 16384:
            raise ValueError()
        raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        signature, payload = raw[:32], raw[32:]
        if not hmac.compare_digest(signature, hmac.digest(_CURSOR_SECRET, payload, 'sha256')):
            raise ValueError()
        data = json.loads(payload)
        if data['scope'] != scope:
            raise ValueError()
        return data['state']
    except (ValueError, TypeError, KeyError):
        raise KnowledgeError('invalid_cursor', 'Cursor expired or belongs to another request; start again', 400) from None


class Budget:
    def __init__(self, byte_limit, seconds):
        self.limit = byte_limit
        self.bytes = 0
        self.deadline = time.monotonic() + seconds
        self.reason = None

    def check(self):
        if time.monotonic() >= self.deadline:
            self.reason = 'time_limit'
            return False
        return True


class FragmentReader:
    """Never materializes a whole line. Positions are UTF-8 byte boundaries."""
    def __init__(self, stream, size, budget, position=None):
        self.stream, self.size, self.budget = stream, size, budget
        self.offset, self.line, self.column = position or (0, 1, 1)
        self.buffer = b''

    @property
    def position(self):
        return [self.offset, self.line, self.column]

    @property
    def eof(self):
        return self.offset >= self.size

    def _fill(self):
        if not self.budget.check():
            return False
        capacity = min(65536 - len(self.buffer), self.budget.limit - self.budget.bytes,
                       self.size - self.offset - len(self.buffer))
        if capacity <= 0:
            self.budget.reason = 'scan_limit'
            return False
        self.stream.seek(self.offset + len(self.buffer))
        data = self.stream.read(capacity)
        self.budget.bytes += len(data)
        if not data:
            raise KnowledgeError('file_changed', 'File changed while reading; start again', 409)
        self.buffer += data
        return True

    def next(self, max_chars=65536):
        if self.eof or not self.budget.check():
            return None
        if not self.buffer and not self._fill():
            return None
        # Do not split CRLF or a UTF-8 code point across returned fragments.
        while True:
            raw = self.buffer
            physical_end = self.offset + len(raw) >= self.size
            safe = raw[:-1] if raw.endswith(b'\r') and not physical_end else raw
            delimiter = _NEWLINE.search(safe)
            content_end = delimiter.start() if delimiter else len(safe)
            consumed = delimiter.end() if delimiter else content_end
            content = safe[:content_end]
            bom = 3 if self.offset == 0 and content.startswith(b'\xef\xbb\xbf') else 0
            try:
                text = content[bom:].decode('utf-8')
            except UnicodeDecodeError as exc:
                if not delimiter and not physical_end and exc.reason == 'unexpected end of data':
                    content_end = bom + exc.start
                    consumed = content_end
                    text = content[bom:content_end].decode('utf-8')
                else:
                    raise KnowledgeError('unsupported_encoding', 'Read portion must be UTF-8 text without NUL bytes', 415) from None
            if consumed or physical_end:
                break
            if not self._fill():
                return None
        if '\x00' in text:
            raise KnowledgeError('unsupported_encoding', 'Read portion contains NUL bytes', 415)
        if bom and not text and not delimiter and physical_end:
            self.offset += consumed
            self.buffer = self.buffer[consumed:]
            return None
        complete = bool(delimiter) or (physical_end and consumed == len(raw))
        if len(text) > max_chars:
            text = text[:max_chars]
            consumed = bom + len(text.encode('utf-8'))
            complete = False
        start = self.position
        self.buffer = self.buffer[consumed:]
        self.offset += consumed
        if complete:
            self.line += 1
            self.column = 1
        else:
            self.column += len(text)
        return {'line': start[1], 'column': start[2], 'text': text,
                'complete': complete, 'position': start, 'next_position': self.position}
