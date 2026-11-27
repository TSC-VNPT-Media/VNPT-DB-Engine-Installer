"""Short-lived HTTP proxy on the install node for nodes that cannot reach the repos.

The node running this tool is assumed to have a route to the package repositories.
Remote nodes only need to reach that node's cluster IP. While apply/enforce runs,
dnf and curl on those nodes use this proxy. The proxy stops when the run ends.
"""

import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

DEFAULT_PORT = 8888
_MAX_HEADER = 65536
_HOP_HEADERS = {"proxy-connection", "proxy-authorization", "connection"}


def bridge_port(cluster):
    return int((cluster or {}).get("package_bridge_port", DEFAULT_PORT))


def extra_firewall_ports(cfg, host):
    """Inbound port the install node must accept when it bridges package downloads."""
    cluster = (cfg or {}).get("cluster") or {}
    if not cluster.get("package_bridge") or not host.get("local"):
        return []
    return [bridge_port(cluster)]


def proxy_env(cfg, host_obj):
    """Proxy variables for one remote host. Empty on the install node and when disabled."""
    cluster = cfg["cluster"]
    if not cluster.get("package_bridge") or host_obj.get("local"):
        return {}
    local = next(h for h in cfg["topology"]["hosts"] if h.get("local"))
    listen = local.get("node_ip") or local["ip"]
    url = f"http://{listen}:{bridge_port(cluster)}"
    bypass = ["localhost", "127.0.0.1", "::1"]
    for host in cfg["topology"]["hosts"]:
        bypass.append(host.get("node_ip") or host["ip"])
        bypass.append(host["ip"])
    no_proxy = ",".join(dict.fromkeys(bypass))
    return {
        "http_proxy": url,
        "https_proxy": url,
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "no_proxy": no_proxy,
        "NO_PROXY": no_proxy,
    }


class PackageBridge:
    """Forward proxy bound to the install node's cluster IP."""

    def __init__(self, bind_ip, port, allowed_ips):
        self.bind_ip = bind_ip
        self.port = int(port)
        self.allowed_ips = {ip for ip in allowed_ips if ip}
        self._server = None
        self._thread = None

    def start(self):
        server = socketserver.ThreadingTCPServer(
            (self.bind_ip, self.port), _ProxyHandler, bind_and_activate=False)
        server.allow_reuse_address = True
        server.daemon_threads = True
        server.allowed_ips = self.allowed_ips
        server.server_bind()
        server.server_activate()
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="vde-package-bridge", daemon=True)
        self._thread.start()

    def stop(self):
        server = self._server
        self._server = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if self.client_address[0] not in self.server.allowed_ips:
            return
        client = self.request
        client.settimeout(30)
        try:
            data = b""
            while b"\r\n\r\n" not in data and len(data) < _MAX_HEADER:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
            if b"\r\n\r\n" not in data:
                return
            head, rest = data.split(b"\r\n\r\n", 1)
            lines = head.decode("iso-8859-1", "replace").split("\r\n")
            method, target, proto = lines[0].split(" ", 2)
            if method.upper() == "CONNECT":
                self._connect(client, target)
            else:
                self._forward(client, method, target, proto, lines[1:], rest)
        except (OSError, ValueError):
            return

    def _connect(self, client, target):
        host, sep, port_text = target.rpartition(":")
        if not sep or not host:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            port = int(port_text)
            upstream = socket.create_connection((host, port), timeout=30)
        except (OSError, ValueError):
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(None)
            upstream.settimeout(None)
            _tunnel(client, upstream)
        finally:
            upstream.close()

    def _forward(self, client, method, target, proto, header_lines, buffered):
        parts = urlsplit(target)
        if parts.scheme != "http" or not parts.hostname:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = []
        content_length = 0
        for line in header_lines:
            name, sep, value = line.partition(":")
            if not sep or name.strip().lower() in _HOP_HEADERS:
                continue
            if name.strip().lower() == "content-length":
                content_length = int(value.strip() or "0")
            headers.append(f"{name.strip()}: {value.strip()}")
        headers.append("Connection: close")
        body = buffered
        while len(body) < content_length:
            chunk = client.recv(min(65536, content_length - len(body)))
            if not chunk:
                return
            body += chunk
        payload = (f"{method} {path} {proto}\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1")
        try:
            upstream = socket.create_connection((parts.hostname, port), timeout=30)
        except OSError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            upstream.sendall(payload + body[:content_length])
            _copy(upstream, client)
        finally:
            upstream.close()


def _copy(source, destination):
    source.settimeout(None)
    while True:
        data = source.recv(65536)
        if not data:
            return
        destination.sendall(data)


def _tunnel(left, right):
    sockets = [left, right]
    while True:
        readable, _, _ = select.select(sockets, [], [], 300)
        if not readable:
            return
        for sock in readable:
            other = right if sock is left else left
            data = sock.recv(65536)
            if not data:
                return
            other.sendall(data)
