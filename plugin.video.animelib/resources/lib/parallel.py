# -*- coding: utf-8 -*-
"""Several HTTPS GET requests at once in a single thread (non-blocking sockets).

Kodi's embedded Python crashes with threads inside plugin invocations (crash
dumps pointed at python3.8.dll right after a thread pool was used), so lists
load title details concurrently without threads.
"""
import gzip
import os
import selectors
import socket
import ssl
import time
import zlib
from urllib.parse import urlsplit

from . import http

_CONNECTING, _PROXY, _HANDSHAKE, _SENDING, _READING = range(5)


class _Conn(object):
    def __init__(self, index, url, headers, proxy, context):
        self.index = index
        parts = urlsplit(url)
        self.host = parts.hostname
        self.port = parts.port or 443
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        hdrs = {'Host': self.host, 'User-Agent': http.USER_AGENT,
                'Accept-Encoding': 'gzip', 'Connection': 'close'}
        hdrs.update(headers or {})
        self.request = ('GET %s HTTP/1.1\r\n' % path + ''.join(
            '%s: %s\r\n' % kv for kv in hdrs.items()) + '\r\n').encode('utf-8')
        self.proxy = proxy
        self.context = context
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        self.fd = self.sock.fileno()
        self.buffer = b''
        target = proxy or (self.host, self.port)
        self.sock.connect_ex(target)
        self.state = _CONNECTING

    def start_tls(self, selector):
        # wrap_socket() detaches the plain socket, so re-register the TLS one
        selector.unregister(self.sock)
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host,
                                             do_handshake_on_connect=False)
        selector.register(self.sock, selectors.EVENT_WRITE, self)
        self.state = _HANDSHAKE


def _proxy_from_env():
    value = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    if not value:
        return None
    parts = urlsplit(value if '://' in value else 'http://' + value)
    return (parts.hostname, parts.port or 8080) if parts.hostname else None


def _parse(raw, url):
    head, _, body = raw.partition(b'\r\n\r\n')
    lines = head.decode('iso-8859-1').split('\r\n')
    try:
        code = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return None
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(':')
        headers[key.strip().lower()] = value.strip()
    if headers.get('transfer-encoding', '').lower() == 'chunked':
        out, rest = b'', body
        while rest:
            size_line, _, rest = rest.partition(b'\r\n')
            try:
                size = int(size_line.split(b';')[0], 16)
            except ValueError:
                break
            if size == 0:
                break
            out, rest = out + rest[:size], rest[size + 2:]
        body = out
    encoding = headers.get('content-encoding', '').lower()
    try:
        if encoding == 'gzip':
            body = gzip.decompress(body)
        elif encoding == 'deflate':
            body = zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return None
    return http.Response(code, headers, body.decode('utf-8', errors='replace'), url)


def get_many(requests, concurrency=8, timeout=20):
    """requests: list of (url, headers). Returns a list of http.Response or None
    (network error / timeout), in the same order."""
    results = [None] * len(requests)
    pending = list(enumerate(requests))
    proxy = _proxy_from_env()
    context = ssl.create_default_context()
    selector = selectors.DefaultSelector()
    active = {}
    deadline = time.time() + timeout

    def open_next():
        while pending and len(active) < concurrency:
            index, (url, headers) = pending.pop(0)
            try:
                conn = _Conn(index, url, headers, proxy, context)
            except OSError:
                continue
            active[conn.fd] = conn
            selector.register(conn.sock, selectors.EVENT_WRITE, conn)

    def close(conn, result=None):
        results[conn.index] = result
        try:
            selector.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        active.pop(conn.fd, None)
        try:
            conn.sock.close()
        except OSError:
            pass

    def want(conn, events):
        selector.modify(conn.sock, events, conn)

    open_next()
    while active and time.time() < deadline:
        for key, _ in selector.select(timeout=max(0.05, deadline - time.time())):
            conn = key.data
            try:
                if conn.state == _CONNECTING:
                    err = conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if err:
                        close(conn)
                        continue
                    if conn.proxy:
                        conn.sock.sendall(('CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n' % (
                            conn.host, conn.port, conn.host, conn.port)).encode())
                        conn.state = _PROXY
                        want(conn, selectors.EVENT_READ)
                    else:
                        conn.start_tls(selector)
                elif conn.state == _PROXY:
                    data = conn.sock.recv(4096)
                    if not data:
                        close(conn)
                        continue
                    conn.buffer += data
                    if b'\r\n\r\n' in conn.buffer:
                        if b' 200' not in conn.buffer.split(b'\r\n', 1)[0]:
                            close(conn)
                            continue
                        conn.buffer = b''
                        conn.start_tls(selector)
                if conn.state == _HANDSHAKE:
                    try:
                        conn.sock.do_handshake()
                    except ssl.SSLWantReadError:
                        want(conn, selectors.EVENT_READ)
                        continue
                    except ssl.SSLWantWriteError:
                        want(conn, selectors.EVENT_WRITE)
                        continue
                    conn.state = _SENDING
                    want(conn, selectors.EVENT_WRITE)
                    continue
                if conn.state == _SENDING:
                    sent = conn.sock.send(conn.request)
                    conn.request = conn.request[sent:]
                    if not conn.request:
                        conn.state = _READING
                        want(conn, selectors.EVENT_READ)
                elif conn.state == _READING:
                    while True:
                        try:
                            data = conn.sock.recv(65536)
                        except ssl.SSLWantReadError:
                            break
                        if not data:
                            close(conn, _parse(conn.buffer, requests[conn.index][0]))
                            break
                        conn.buffer += data
            except (OSError, ssl.SSLError, ValueError):
                close(conn)
        open_next()
    for conn in list(active.values()):
        close(conn)
    selector.close()
    return results
