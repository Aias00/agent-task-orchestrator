"""Bounded JSON-RPC over the local Codex daemon's Unix WebSocket socket."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time


class Client:
    def __init__(self, home, timeout=20):
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.buffer = b''
        self.sequence = 0
        try:
            self.sock.connect(str(Path(home)/'app-server-control/app-server-control.sock'))
            key = base64.b64encode(os.urandom(16)).decode()
            request = ('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                       'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                       f'Sec-WebSocket-Key: {key}\r\n\r\n')
            self.sock.sendall(request.encode())
            while b'\r\n\r\n' not in self.buffer:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError('daemon closed handshake')
                self.buffer += chunk
                if len(self.buffer) > 65536:
                    raise ValueError('oversized daemon handshake')
            headers, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
            lines = headers.decode().split('\r\n')
            expected = base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            fields = {line.split(':', 1)[0].lower(): line.split(':', 1)[1] for line in lines[1:] if ':' in line}
            if ' 101 ' not in lines[0] or fields.get('sec-websocket-accept', '').strip() != expected:
                raise ValueError('daemon rejected WebSocket handshake')
            self.request('initialize', {'clientInfo': {'name': 'ctask-controller', 'version': '1.0'},
                                       'capabilities': {'experimentalApi': False}})
            self.send({'method': 'initialized', 'params': {}})
        except Exception:
            self.sock.close()
            raise

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def frame(self, payload, opcode=1):
        if len(payload) > 16*1024*1024:
            raise ValueError('oversized daemon request')
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size <= 65535:
            header += bytes([0x80 | 126]) + struct.pack('!H', size)
        else:
            header += bytes([0x80 | 127]) + struct.pack('!Q', size)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, value):
        self.sock.settimeout(self.timeout)
        self.frame(json.dumps(value, ensure_ascii=False).encode())

    def read(self, n, deadline):
        while len(self.buffer) < n:
            left = deadline-time.monotonic()
            if left <= 0:
                raise TimeoutError('daemon response timeout')
            self.sock.settimeout(left)
            chunk = self.sock.recv(min(65536, n-len(self.buffer)))
            if not chunk:
                raise ConnectionError('daemon closed connection')
            self.buffer += chunk
        result, self.buffer = self.buffer[:n], self.buffer[n:]
        return result

    def receive(self, deadline):
        chunks = bytearray()
        while True:
            first, second = self.read(2, deadline)
            if first & 0x70 or second & 0x80:
                raise ValueError('unsupported compressed/masked daemon response')
            size = second & 0x7f
            if size == 126:
                size = struct.unpack('!H', self.read(2, deadline))[0]
            elif size == 127:
                size = struct.unpack('!Q', self.read(8, deadline))[0]
            if size + len(chunks) > 16*1024*1024:
                raise ValueError('oversized daemon response')
            data = self.read(size, deadline)
            opcode = first & 0x0f
            if opcode == 8:
                raise ConnectionError('daemon closed WebSocket')
            if opcode == 9:
                self.frame(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise ValueError('unsupported daemon frame')
            chunks.extend(data)
            if first & 0x80:
                return json.loads(chunks)

    def request(self, method, params):
        self.sequence += 1
        identifier = self.sequence
        self.send({'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic()+self.timeout
        while True:
            value = self.receive(deadline)
            if value.get('id') == identifier and ('result' in value or 'error' in value):
                if 'error' in value:
                    raise ValueError(f'daemon {method}: {value["error"]}')
                return value['result']
            if 'id' in value and 'method' in value:
                # Creating threads never approves server-side permission requests.
                self.send({'id': value['id'], 'error': {'code': -32601, 'message': 'Not supported by ctask controller'}})
