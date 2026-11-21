from __future__ import annotations

from http.cookiejar import Cookie

from web_research.safety.cookies import ExpiringCookieJar


def cookie(
    name: str = "session", value: str = "a", domain: str = "example.com", path: str = "/"
) -> Cookie:
    # A site cookie with no `Expires`: what `clear_expired_cookies` cannot see, and therefore what
    # a jar that lives for days remembers forever.
    return Cookie(
        0,
        name,
        value,
        None,
        False,
        domain,
        True,
        False,
        path,
        True,
        False,
        None,
        False,
        None,
        {},
        {},
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_a_cookie_survives_inside_its_lifetime_and_dies_outside_it():
    clock = Clock()
    jar = ExpiringCookieJar(60, clock=clock)
    jar.set_cookie(cookie())
    assert len(jar) == 1

    clock.now += 30
    assert jar.prune() == 0
    assert len(jar) == 1

    clock.now += 40
    assert jar.prune() == 1
    assert len(jar) == 0


def test_a_site_that_refreshes_its_cookie_is_not_cut_off_mid_read():
    clock = Clock()
    jar = ExpiringCookieJar(60, clock=clock)
    jar.set_cookie(cookie(value="first"))
    for _ in range(5):
        clock.now += 50
        jar.set_cookie(cookie(value="refreshed"))
        assert jar.prune() == 0
    assert [c.value for c in jar] == ["refreshed"]

    clock.now += 61
    assert jar.prune() == 1


def test_zero_lifetime_stores_nothing_because_a_challenge_cannot_be_remembered():
    jar = ExpiringCookieJar(0)
    jar.set_cookie(cookie())
    assert len(jar) == 0
    assert jar.prune() == 0


def test_two_hosts_sharing_a_cookie_name_keep_separate_lifetimes():
    clock = Clock()
    jar = ExpiringCookieJar(60, clock=clock)
    jar.set_cookie(cookie(domain="a.example", value="1"))
    clock.now += 40
    jar.set_cookie(cookie(domain="b.example", value="2"))

    clock.now += 30
    assert jar.prune() == 1
    assert {c.domain for c in jar} == {"b.example"}
