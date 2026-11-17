"""Bootstrap all ancillary HTTP clients (including Crawl4AI robots retrieval)."""

from __future__ import annotations

import httpx

from .transport import BrokerTransport, SyncBrokerTransport


def main() -> None:
    async_client, sync_client = httpx.AsyncClient, httpx.Client

    class AsyncClient(async_client):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", BrokerTransport())
            kwargs["trust_env"] = False
            super().__init__(*args, **kwargs)

    class Client(sync_client):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", SyncBrokerTransport())
            kwargs["trust_env"] = False
            super().__init__(*args, **kwargs)

    httpx.AsyncClient, httpx.Client = AsyncClient, Client
    from ..server import main as serve

    serve()


if __name__ == "__main__":
    main()
