"""Pinned SearXNG with every engine's HTTPX transport replaced by the broker."""

from __future__ import annotations

from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

import httpx

from .transport import BrokerTransport


def main() -> None:
    import searx.network.client as client
    import searx.network.network as network

    # This is the adapter used by the pinned SearXNG build. Any other engine networking
    # fails at the OS boundary: this container has --network=none, including no DNS.
    def new_client(*args, **kwargs):
        hooks = kwargs.get("hook_log_response", args[10] if len(args) > 10 else None)
        return httpx.AsyncClient(
            transport=BrokerTransport(),
            trust_env=False,
            max_redirects=5,
            event_hooks={"response": [hooks]} if hooks else None,
        )

    client.new_client = new_client
    network.new_client = new_client
    from searx.webapp import app

    class ThreadedServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    with make_server("127.0.0.1", 8080, app, server_class=ThreadedServer) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
