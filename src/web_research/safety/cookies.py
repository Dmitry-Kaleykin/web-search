"""How long a site's cookie is remembered by the plain HTTP reader.

Letting `Set-Cookie` through the network broker is what makes a challenge round trip work. An
unbounded jar charges for that in a different currency: every later visit becomes a returning
visit, and the site's own cookie identifies this reader for as long as the process lives - days,
for an MCP server that nobody restarts. Remembering a cookie for minutes keeps the first behaviour
and gives up the second.
"""

from __future__ import annotations

import time
from http.cookiejar import Cookie, CookieJar


class ExpiringCookieJar(CookieJar):
    """Forgets a cookie a bounded time after the site that set it was last heard from.

    `CookieJar.clear_expired_cookies`, which HTTP clients call for you, only understands a
    cookie's own `Expires` attribute. Most tracking and session cookies have none, so a client
    that lives long enough keeps them forever. `ttl_seconds=0` stores nothing at all, which is
    the strictest setting a caller can ask for.
    """

    def __init__(self, ttl_seconds: float = 900.0, *, clock=time.time) -> None:
        super().__init__()
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._clock = clock
        self._planted: dict[str, float] = {}

    def set_cookie(self, cookie: Cookie) -> None:
        if self.ttl_seconds == 0:
            return
        # A refreshed value restarts the clock, so a site that keeps re-issuing its cookie during a
        # paginated read is not cut off mid-task, while a cookie it stops touching does die.
        self._planted[self._key(cookie)] = self._clock()
        super().set_cookie(cookie)

    def prune(self) -> int:
        """Drop what has aged out. Returns how many cookies went, for logging and tests."""
        now = self._clock()
        keep = []
        for stored in list(self):
            planted = self._planted.get(self._key(stored))
            fresh = (
                self.ttl_seconds > 0 and planted is not None and now - planted <= self.ttl_seconds
            )
            if fresh:
                keep.append(stored)
        dropped = len(self) - len(keep)
        if dropped:
            # Rebuilt rather than deleted one by one: `CookieJar` exposes no public single-cookie
            # removal, and a jar this small makes the difference invisible.
            self.clear()
            for stored in keep:
                CookieJar.set_cookie(self, stored)
            self._planted = {self._key(c): self._planted[self._key(c)] for c in keep}
        return dropped

    @staticmethod
    def _key(cookie: Cookie) -> str:
        # Names alone collide: two hosts on a registrable domain can set `session` alike.
        return f"{cookie.domain}|{cookie.path}|{cookie.name}"
