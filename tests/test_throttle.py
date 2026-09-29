# -*- coding: utf-8 -*-
from facegate.throttle import GreetThrottle


def test_first_call_allowed():
    t = GreetThrottle(cooldown=10)
    assert t.allow("michael", now=0.0) is True


def test_second_call_within_cooldown_blocked():
    t = GreetThrottle(cooldown=10)
    assert t.allow("michael", now=0.0) is True
    assert t.allow("michael", now=5.0) is False


def test_call_after_cooldown_allowed():
    t = GreetThrottle(cooldown=10)
    assert t.allow("michael", now=0.0) is True
    assert t.allow("michael", now=10.5) is True


def test_different_names_independent():
    t = GreetThrottle(cooldown=10)
    assert t.allow("michael", now=0.0) is True
    assert t.allow("anna", now=1.0) is True
    assert t.allow("michael", now=2.0) is False


def test_reset_single():
    t = GreetThrottle(cooldown=100)
    t.allow("michael", now=0.0)
    t.reset("michael")
    assert t.allow("michael", now=1.0) is True


def test_reset_all():
    t = GreetThrottle(cooldown=100)
    t.allow("michael", now=0.0)
    t.allow("anna", now=0.0)
    t.reset()
    assert t.allow("michael", now=1.0) is True
