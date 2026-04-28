"""Distributed locks backed by the configured Django cache.

Two named locks live here:

* ``candidate_lock`` — held while a super-admin runs Add / Edit / Delete /
  Bulk-Delete on the Candidate roster (or re-imports the CSV). It serialises
  these operations across every Daphne worker and admin browser so the
  expensive post-write sync (relinking incidents, broadcasting WebSocket
  events) cannot race with itself.

* ``incident_bulk_delete_lock`` — held while a super-admin or room-admin
  runs the bulk-delete-messages flow. Posting new incidents (or editing an
  existing one) while the bulk delete is in flight is refused with HTTP 409
  so the freshly-posted message can never collide with the bulk-delete pass
  about to wipe a swath of rows.

WHY a cache-backed lock and not ``select_for_update``?

We want callers (the views) to be able to *immediately* refuse a competing
mutation with HTTP 409 + ``{busy: True}`` so the UX stays fast. A row-level
DB lock would block the second request until the first finishes, which
could be tens of seconds during a heavy CSV import — the user would just
see a hung modal. ``cache.add()`` is a non-blocking compare-and-set that
returns False instantly when the key is held; the second request then
returns 409 right away and the user sees a clear "thử lại sau" toast.

DEPLOYMENT NOTE — REDIS REQUIRED FOR MULTI-WORKER:

``cache.add()`` is only atomic across processes if the configured cache
backend is shared between workers (Redis or Memcached). In single-worker
``LocMemCache`` mode the lock works correctly inside that one process but
provides ZERO protection against a second worker mutating in parallel —
see the ``CACHE_BACKEND_KIND`` block in ``chatbox_vi_pham/settings.py``.
We log a one-time WARNING the first time a lock is exercised under a
non-shared backend so misconfigured deployments are loud, not silent.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# How long either lock is allowed to stay held before the cache auto-releases
# it. Tuned to comfortably cover a normal mutation + post-write sync (a few
# seconds) plus a generous slack so a stalled request can't deadlock the
# UI forever.
LOCK_TTL_SECONDS = 40

# ── Cache key naming. The ``cbvp:`` prefix lives in settings.KEY_PREFIX so
# multiple deployments sharing one Redis instance don't collide. ───────────
CANDIDATE_LOCK_CACHE_KEY = "candidates:mutation_lock"
CANDIDATE_LOCK_OWNER_HINT_KEY = "candidates:mutation_lock_owner"

INCIDENT_BULK_LOCK_CACHE_KEY = "incidents:bulk_delete_lock"
INCIDENT_BULK_LOCK_OWNER_HINT_KEY = "incidents:bulk_delete_lock_owner"

# Backwards-compatible aliases kept for any caller that still refers to the
# pre-refactor names. Existing code in ``views.py`` only goes through the
# helper functions below, so these are defensive.
LOCK_CACHE_KEY = CANDIDATE_LOCK_CACHE_KEY
LOCK_OWNER_HINT_KEY = CANDIDATE_LOCK_OWNER_HINT_KEY

# One-shot warning: log only once per process if a non-shared backend is
# in use, so we don't spam the log with the same message on every call.
_warned_non_shared_backend = False


@dataclass(frozen=True)
class LockHandle:
    """Opaque handle returned by ``acquire_*_lock`` on success.

    The token uniquely identifies the lock owner so ``release`` cannot
    accidentally drop someone else's lock if our own request stalled past
    the TTL and another writer has since acquired the slot.
    """
    token: str
    owner_user_id: Optional[int]
    owner_username: str
    acquired_at: float
    operation: str
    # Which lock this handle belongs to — needed by ``release`` to know
    # which cache key to verify and clear.
    lock_kind: str = "candidate"


@dataclass(frozen=True)
class LockState:
    """Snapshot of a lock as observed at a point in time."""
    busy: bool
    owner_user_id: Optional[int] = None
    owner_username: str = ""
    operation: str = ""
    acquired_at: Optional[float] = None


def _warn_if_non_shared_backend_once() -> None:
    """Log a single warning if the cache backend won't share state across workers."""
    global _warned_non_shared_backend
    if _warned_non_shared_backend:
        return
    kind = getattr(settings, "CACHE_BACKEND_KIND", None)
    if kind not in ("redis", "memcached"):
        logger.warning(
            "candidates lock is using a non-shared cache backend (kind=%r). "
            "This is fine for single-worker dev, but production deployments "
            "with multiple Daphne workers MUST set DJANGO_REDIS_URL or "
            "DJANGO_MEMCACHED_LOCATION — otherwise the lock does not "
            "prevent concurrent candidate mutations across workers.",
            kind,
        )
        _warned_non_shared_backend = True


def _acquire(
    *,
    cache_key: str,
    owner_hint_key: str,
    lock_kind: str,
    user_id: Optional[int],
    username: str,
    operation: str,
) -> Optional[LockHandle]:
    """Generic acquire — used by both candidate and incident-bulk locks."""
    _warn_if_non_shared_backend_once()

    token = uuid.uuid4().hex
    now = time.time()
    payload = {
        "token": token,
        "user_id": user_id,
        "username": username or "",
        "operation": operation or "",
        "acquired_at": now,
    }
    acquired = cache.add(cache_key, payload, timeout=LOCK_TTL_SECONDS)
    if not acquired:
        return None

    cache.set(owner_hint_key, payload, timeout=LOCK_TTL_SECONDS)

    return LockHandle(
        token=token,
        owner_user_id=user_id,
        owner_username=username or "",
        acquired_at=now,
        operation=operation or "",
        lock_kind=lock_kind,
    )


def _release(
    handle: LockHandle,
    *,
    cache_key: str,
    owner_hint_key: str,
) -> bool:
    """Generic release with token check — see ``release_candidate_lock`` doc."""
    current = cache.get(cache_key)
    if not isinstance(current, dict):
        cache.delete(owner_hint_key)
        return False
    if current.get("token") != handle.token:
        return False

    cache.delete(cache_key)
    cache.delete(owner_hint_key)
    return True


def _state(cache_key: str) -> LockState:
    payload = cache.get(cache_key)
    if not isinstance(payload, dict):
        return LockState(busy=False)
    return LockState(
        busy=True,
        owner_user_id=payload.get("user_id"),
        owner_username=payload.get("username") or "",
        operation=payload.get("operation") or "",
        acquired_at=payload.get("acquired_at"),
    )


# ── Candidate-mutation lock ───────────────────────────────────────────────


def acquire_candidate_lock(
    *,
    user_id: Optional[int],
    username: str,
    operation: str,
) -> Optional[LockHandle]:
    """Try to acquire the global candidate-mutation lock.

    Returns a ``LockHandle`` on success, or ``None`` if another writer is
    already holding it. Never blocks — checks once, returns immediately.

    The TTL is enforced server-side by the cache backend so a stalled
    holder (network drop, worker crash) auto-releases after
    ``LOCK_TTL_SECONDS`` and the lock becomes acquirable again.
    """
    return _acquire(
        cache_key=CANDIDATE_LOCK_CACHE_KEY,
        owner_hint_key=CANDIDATE_LOCK_OWNER_HINT_KEY,
        lock_kind="candidate",
        user_id=user_id,
        username=username,
        operation=operation,
    )


def release_candidate_lock(handle: LockHandle) -> bool:
    """Release a lock previously obtained via ``acquire_candidate_lock``.

    The release is *token-checked*: we only delete the key if its current
    payload's token matches the one we were given. This protects against
    the classic race where:

        worker A acquires → A stalls past the TTL → cache evicts A's key
        worker B acquires (legitimately) → A finishes and tries to release

    Without the token check, A's release would drop B's lock and let a
    third writer slip in.

    Returns True if we actually deleted the key, False if it had already
    expired or been replaced.
    """
    return _release(
        handle,
        cache_key=CANDIDATE_LOCK_CACHE_KEY,
        owner_hint_key=CANDIDATE_LOCK_OWNER_HINT_KEY,
    )


def get_lock_state() -> LockState:
    """Cheap snapshot of the candidate lock for callers that just want to peek."""
    return _state(CANDIDATE_LOCK_CACHE_KEY)


@contextmanager
def candidate_mutation_lock(
    *,
    user_id: Optional[int],
    username: str,
    operation: str,
):
    """Context manager flavour. Yields the ``LockHandle`` on success.

    Raises :class:`LockBusy` if the lock is already held — callers in
    Django views typically check upfront with ``acquire_candidate_lock``
    so they can craft a 409 response, but this is convenient for
    management commands and tests.
    """
    handle = acquire_candidate_lock(
        user_id=user_id,
        username=username,
        operation=operation,
    )
    if handle is None:
        raise LockBusy(get_lock_state())
    try:
        yield handle
    finally:
        release_candidate_lock(handle)


# ── Incident-bulk-delete lock ─────────────────────────────────────────────


def acquire_incident_bulk_lock(
    *,
    user_id: Optional[int],
    username: str,
    operation: str,
) -> Optional[LockHandle]:
    """Try to acquire the incident bulk-delete lock.

    Held while a privileged user is running ``incidents_bulk_delete``.
    While held, ``incident_create`` and ``incident_edit`` (POST) refuse
    new submissions with HTTP 409 so a freshly-posted message cannot
    race with the deletion pass.

    Independent of the candidate lock — both can be held concurrently
    by different writers without blocking each other.
    """
    return _acquire(
        cache_key=INCIDENT_BULK_LOCK_CACHE_KEY,
        owner_hint_key=INCIDENT_BULK_LOCK_OWNER_HINT_KEY,
        lock_kind="incident_bulk",
        user_id=user_id,
        username=username,
        operation=operation,
    )


def release_incident_bulk_lock(handle: LockHandle) -> bool:
    """Release the incident bulk-delete lock — token-checked."""
    return _release(
        handle,
        cache_key=INCIDENT_BULK_LOCK_CACHE_KEY,
        owner_hint_key=INCIDENT_BULK_LOCK_OWNER_HINT_KEY,
    )


def get_incident_bulk_lock_state() -> LockState:
    """Cheap snapshot of the incident bulk-delete lock."""
    return _state(INCIDENT_BULK_LOCK_CACHE_KEY)


# ── Errors ────────────────────────────────────────────────────────────────


class LockBusy(Exception):
    """Raised by ``candidate_mutation_lock`` when the lock is already held."""

    def __init__(self, state: LockState):
        super().__init__("Mutation lock is currently held")
        self.state = state


# ── User-facing messages ──────────────────────────────────────────────────
# Centralised so the wording stays consistent across the API and the
# WebSocket broadcasts.

BUSY_USER_MESSAGE = (
    "Một quá trình cập nhật cơ sở dữ liệu đang diễn ra, hãy thử lại sau."
)

INCIDENT_BUSY_USER_MESSAGE = (
    "Đang có một quá trình xoá nhiều tin nhắn diễn ra, hãy thử lại sau."
)
