"""The schema lock of the start (bootstrap/startup.py): a worker waits while another connection migrates, with no
deadline, and never leaves its connection open on a failure. The live behaviour of GET_LOCK (a held lock times out
with 0, IS_USED_LOCK names the holder, a closed holder frees it) was checked on MariaDB 11.8 (CHANGELOG)."""
import pytest

from app.backend.bootstrap import startup


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _Conn:
    """Answers GET_LOCK from a script; IS_USED_LOCK names connection 7"""

    def __init__(self, get_lock_answers, fail_on=None):
        self.get_lock_answers = list(get_lock_answers)
        self.fail_on = fail_on
        self.statements = []
        self.closed = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise ConnectionError("lost")
        if "GET_LOCK" in sql:
            return _Result(self.get_lock_answers.pop(0))
        if "IS_USED_LOCK" in sql:
            return _Result(7)
        return _Result(1)

    async def close(self):
        self.closed = True


class _Engine:
    def __init__(self, conn, dialect="mysql"):
        self._conn = conn
        self.dialect = type("D", (), {"name": dialect})()

    async def connect(self):
        return self._conn


def _use(monkeypatch, conn, dialect="mysql"):
    async def get_engine():
        return _Engine(conn, dialect)
    monkeypatch.setattr(startup, "get_engine", get_engine)


async def _enter_and_exit():
    async with startup._SchemaLock():
        pass


def _calls(conn, name):
    return [params for sql, params in conn.statements if name in sql]


async def test_a_worker_waits_while_another_connection_migrates(monkeypatch):
    conn = _Conn([0, 0, 0, 1])
    _use(monkeypatch, conn)
    await _enter_and_exit()
    attempts = _calls(conn, "GET_LOCK(")
    assert len(attempts) == 4
    assert all(params["timeout"] == startup.SCHEMA_LOCK_POLL_SECONDS for params in attempts)
    assert len(_calls(conn, "IS_USED_LOCK")) == 3
    assert len(_calls(conn, "RELEASE_LOCK")) == 1
    assert conn.closed


async def test_a_free_lock_is_taken_at_once(monkeypatch):
    conn = _Conn([1])
    _use(monkeypatch, conn)
    await _enter_and_exit()
    assert len(_calls(conn, "GET_LOCK(")) == 1
    assert _calls(conn, "IS_USED_LOCK") == []


async def test_a_get_lock_error_fails_the_start_and_closes_the_connection(monkeypatch):
    conn = _Conn([0, None])
    _use(monkeypatch, conn)
    with pytest.raises(RuntimeError, match="GET_LOCK"):
        await _enter_and_exit()
    assert conn.closed
    assert _calls(conn, "RELEASE_LOCK") == []


async def test_a_lost_connection_while_waiting_closes_it_and_reraises(monkeypatch):
    conn = _Conn([0], fail_on="IS_USED_LOCK")
    _use(monkeypatch, conn)
    with pytest.raises(ConnectionError):
        await _enter_and_exit()
    assert conn.closed


async def test_other_dialects_take_no_lock(monkeypatch):
    conn = _Conn([])
    _use(monkeypatch, conn, dialect="sqlite")
    await _enter_and_exit()
    assert conn.statements == []
