#!/usr/bin/env python3
"""Loopback-only TCP bridge to Grocy's egress-disabled Docker lab network.

Docker does not publish ports for its internal network on this host. This bridge
preserves HTTP bytes and only connects to the three fixed private lab addresses.
It never logs request paths, headers, cookies or payloads.
"""
import json
from pathlib import Path
import socket
import socketserver
import threading

CONFIG = Path('/home/wwadmin/grocyste-work/lab/private/bridge-targets.json')


class Bridge(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            upstream = socket.create_connection(self.server.target, timeout=10)
        except OSError:
            return
        upstream.settimeout(None)

        def forward(source, destination):
            try:
                while data := source.recv(65536):
                    destination.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        worker = threading.Thread(target=forward, args=(self.request, upstream), daemon=True)
        worker.start()
        forward(upstream, self.request)
        worker.join(timeout=2)
        upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    targets = json.loads(CONFIG.read_text())
    for port, address in targets.items():
        if not (address.startswith('172.') or address.startswith('10.')):
            raise RuntimeError('Bridge refuses a non-private target')
        server = Server(('127.0.0.1', int(port)), Bridge)
        server.target = (address, 80)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()
