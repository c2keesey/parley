import os
import signal
import stat
from contextlib import contextmanager

import pytest

from parley import config, processes

BIRTH = "linux:00000000-0000-0000-0000-000000000000:42"
REUSED_BIRTH = "linux:00000000-0000-0000-0000-000000000000:99"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path / "state")


@contextmanager
def permissive_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_signal_revalidates_birth_at_the_toctou_boundary(tmp_path, monkeypatch):
    marker = tmp_path / "state" / "speech.json"
    identities = iter((BIRTH, BIRTH, BIRTH, REUSED_BIRTH))
    monkeypatch.setattr(processes, "process_identity", lambda pid: next(identities))
    ownership = processes.claim(marker, 42, "speech")
    assert ownership is not None
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda pid, sig: pytest.fail("reused PID must not be signalled"),
    )

    assert not processes.send_signal(ownership, signal.SIGTERM)
    assert not marker.exists()


def test_delayed_old_cleanup_cannot_remove_reused_pid_owner(
        tmp_path, monkeypatch):
    marker = tmp_path / "state" / "listener.json"
    births = {42: BIRTH}
    monkeypatch.setattr(processes, "process_identity", births.get)
    old = processes.claim(marker, 42, "listener")
    assert old is not None

    births[42] = REUSED_BIRTH
    replacement = processes.claim(marker, 42, "listener")
    assert replacement is not None
    assert replacement != old

    assert not processes.release(old)
    assert processes.owned(marker, "listener") == replacement


def test_obsolete_capability_cannot_signal_replacement(tmp_path, monkeypatch):
    marker = tmp_path / "state" / "listener.json"
    births = {42: BIRTH}
    monkeypatch.setattr(processes, "process_identity", births.get)
    old = processes.claim(marker, 42, "listener")
    births[42] = REUSED_BIRTH
    replacement = processes.claim(marker, 42, "listener")
    killed = []
    monkeypatch.setattr(
        processes.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert not processes.send_signal(old, signal.SIGTERM)
    assert killed == []
    assert processes.owned(marker, "listener") == replacement


def test_dead_process_record_is_recovered_without_a_signal(tmp_path, monkeypatch):
    marker = tmp_path / "state" / "speech.json"
    births = {42: BIRTH}
    monkeypatch.setattr(processes, "process_identity", births.get)
    assert processes.claim(marker, 42, "speech") is not None
    births.clear()
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda pid, sig: pytest.fail("dead process must not be signalled"),
    )

    assert not processes.send_signal(marker, signal.SIGTERM, "speech")
    assert not marker.exists()


def test_process_records_and_locks_are_private_under_permissive_umask(
        tmp_path, monkeypatch):
    marker = tmp_path / "state" / "owners" / "listener.json"
    monkeypatch.setattr(processes, "process_identity", lambda pid: BIRTH)

    with permissive_umask():
        assert processes.claim(marker, 42, "listener") is not None

    assert mode(config.STATE) == 0o700
    assert mode(marker.parent) == 0o700
    assert mode(marker) == 0o600
    assert mode(marker.parent / ".ownership.lock") == 0o600


def test_process_lock_repairs_an_existing_permissive_mode(tmp_path, monkeypatch):
    marker = tmp_path / "state" / "owners" / "listener.json"
    monkeypatch.setattr(processes, "process_identity", lambda pid: BIRTH)
    ownership = processes.claim(marker, 42, "listener")
    lock = marker.parent / ".ownership.lock"
    lock.chmod(0o666)

    assert processes.owned(marker, "listener") == ownership
    assert mode(lock) == 0o600
