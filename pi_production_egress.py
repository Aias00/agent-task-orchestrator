"""Owned, bounded CONNECT egress. No credentials or HTTP request bodies retained."""
import ipaddress
import secrets
import select
import socket
import socketserver
import threading
import time


class EgressProxy:
    def __init__(self, host, addresses, *, check, resolver=socket.getaddrinfo,
                 connector=socket.create_connection):
        self.host = host
        self.addresses = frozenset(addresses)
        if not self.addresses or any(not ipaddress.ip_address(ip).is_global for ip in self.addresses):
            raise ValueError('invalid production egress addresses')
        self.check, self.resolver, self.connector = check, resolver, connector
        self.lock = threading.RLock()
        self.closed = False
        self.grants = {}
        self.sockets = set()
        self.server = self.thread = None

    def validate_dns(self):
        addresses = {item[4][0] for item in self.resolver(self.host, 443, type=socket.SOCK_STREAM)}
        if addresses != self.addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError('production DNS binding changed')

    def issue(self, remaining_ms):
        with self.lock:
            if self.closed or len(self.grants) >= 16:
                raise ValueError('production egress closed')
            grant = secrets.token_urlsafe(32)
            self.grants[grant] = time.monotonic() + min(60, remaining_ms/1000)
            return grant

    def start(self):
        if self.server or self.closed:
            raise ValueError('egress is not fresh')
        self.validate_dns()
        proxy = self
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            block_on_close = True
            def handle_error(self, *_):
                pass  # Never format requests/exception messages.
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy.handle(self.request)
        self.server = Server(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={'poll_interval': .05}, daemon=True, name='owned-pi-egress')
        self.thread.start()
        return self.server.server_address[1]

    def handle(self, client):
        upstream = None
        try:
            with self.lock:
                if self.closed:
                    return
                self.sockets.add(client)
            client.settimeout(1)
            data = b''
            while b'\r\n\r\n' not in data:
                part = client.recv(1024)
                if not part or len(data)+len(part) > 4096:
                    return
                data += part
            head, tail = data.split(b'\r\n\r\n', 1)
            if tail:
                return
            lines = head.decode('ascii').split('\r\n')
            expected = f'CONNECT {self.host}:443 HTTP/1.1'
            if (len(lines) != 3 or lines[0] != expected
                    or lines[1] != f'Host: {self.host}:443'
                    or not lines[2].startswith('Proxy-Authorization: Bearer ')):
                return
            token = lines[2].removeprefix('Proxy-Authorization: Bearer ')
            with self.lock:
                expiry = self.grants.pop(token, 0)
                if self.closed or expiry <= time.monotonic():
                    return
                self.check()
                self.validate_dns()
                upstream = self.connector((sorted(self.addresses)[0], 443), timeout=2)
                self.sockets.update((client, upstream))
            client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            transferred = 0
            while time.monotonic() < expiry:
                with self.lock:
                    if self.closed:
                        return
                    self.check()
                ready, _, _ = select.select([client, upstream], [], [], .1)
                for source in ready:
                    chunk = source.recv(65536)
                    if not chunk:
                        return
                    transferred += len(chunk)
                    if transferred > 2*1024*1024:
                        return
                    (upstream if source is client else client).sendall(chunk)
        except (OSError, ValueError, KeyError, UnicodeError):
            return
        finally:
            with self.lock:
                for stream in (client, upstream):
                    if stream:
                        self.sockets.discard(stream)
                        try: stream.close()
                        except OSError: pass

    def close(self):
        with self.lock:
            self.closed = True
            self.grants.clear()
            for stream in tuple(self.sockets):
                try: stream.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                stream.close()
            self.sockets.clear()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                raise ValueError('production egress cleanup unproved')
            self.server = None
