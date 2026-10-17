import pytest
from django.db import OperationalError, connection

from ledger.services import replica
from ledger.services.replica import choose_read_alias


@pytest.fixture(autouse=True)
def reset_breaker(monkeypatch):
    monkeypatch.setattr(replica, "_unavailable_until", 0.0)


@pytest.fixture
def enabled(settings):
    settings.READ_REPLICA_ENABLED = True
    settings.READ_REPLICA_MAX_LAG_SECONDS = 5


def fake_status(monkeypatch, result):
    calls = []

    def status(start_lsn):
        calls.append(start_lsn)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(replica, "replica_status", status)
    return calls


def test_disabled_never_touches_replica(settings, monkeypatch):
    settings.READ_REPLICA_ENABLED = False
    calls = fake_status(monkeypatch, (True, 0.0))
    assert choose_read_alias('bounded') == 'default'
    assert choose_read_alias('strong') == 'default'
    assert calls == []


@pytest.mark.django_db
@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("status, bounded, strong", [
    ((True, None), 'replica', 'replica'),    # caught up to request start: read-your-writes
    ((False, 1.0), 'replica', 'default'),    # lagging within bound
    ((False, 5.0), 'replica', 'default'),    # bound is inclusive
    ((False, 10.0), 'default', 'default'),   # lagging beyond bound
    ((False, None), 'default', 'default'),   # lag unknown -> stale
])
def test_freshness_rules(monkeypatch, status, bounded, strong):
    fake_status(monkeypatch, status)
    assert choose_read_alias('bounded') == bounded
    assert choose_read_alias('strong') == strong


@pytest.mark.django_db
@pytest.mark.usefixtures("enabled")
def test_status_uses_primary_wal_position(monkeypatch):
    calls = fake_status(monkeypatch, (True, None))
    choose_read_alias()
    with connection.cursor() as c:
        c.execute("SELECT %s::pg_lsn <= pg_current_wal_lsn()", [calls[0]])
        assert c.fetchone()[0]


@pytest.mark.django_db
@pytest.mark.usefixtures("enabled")
def test_unreachable_replica_falls_back_and_backs_off(monkeypatch):
    calls = fake_status(monkeypatch, OperationalError("connection refused"))
    assert choose_read_alias() == 'default'
    assert choose_read_alias() == 'default'
    assert len(calls) == 1  # second call skipped the replica entirely

    monkeypatch.setattr(replica, "_unavailable_until", 0.0)  # back-off elapsed
    fake_status(monkeypatch, (True, None))
    assert choose_read_alias() == 'replica'


@pytest.mark.django_db(databases=['default', 'replica'])
def test_status_query_against_primary_mirror_reports_caught_up():
    with connection.cursor() as c:
        c.execute("SELECT pg_current_wal_lsn()")
        start_lsn = c.fetchone()[0]
    caught_up, _lag = replica.replica_status(start_lsn)
    assert caught_up is True
