"""Single-instance lock tests.

The bug this guards against is a second live process starting while a (possibly
wedged) first one still holds the port and DB - the second dies on 'address
already in use' and launchd keeps restarting it.  The flock guard must make that
an explicit, early failure instead.
"""

from __future__ import annotations

import socket

import pytest

from arcus.singleton import SingleInstance


def test_acquire_is_exclusive_on_the_same_lock(tmp_path):
    lock = tmp_path / "mainnet.lock"
    first = SingleInstance(lock)
    assert first.acquire() is True
    assert first.held is True

    # A second instance on the same path must be refused.
    second = SingleInstance(lock)
    assert second.acquire() is False
    assert second.held is False

    # After the first releases, a fresh acquire succeeds.
    first.release()
    assert first.held is False
    third = SingleInstance(lock)
    assert third.acquire() is True
    third.release()


def test_different_lock_paths_are_independent(tmp_path):
    a = SingleInstance(tmp_path / "a.lock")
    b = SingleInstance(tmp_path / "b.lock")
    assert a.acquire() is True
    assert b.acquire() is True  # different config -> different lock -> both run
    a.release()
    b.release()


def test_release_is_idempotent(tmp_path):
    lock = SingleInstance(tmp_path / "x.lock")
    assert lock.acquire() is True
    lock.release()
    lock.release()  # no error
