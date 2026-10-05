"""MA-118 §10's concurrency/load test — explicitly "the single most
important test in the whole plan" (impl-plan §3 step 2): run N concurrent
`reserve` calls against a product stocked for exactly N-1, assert exactly
N-1 succeed and exactly 1 fails with InsufficientStockError, and
`available` never goes negative. Also covers §10's other two concurrency
cases (N concurrent commits for the same reservation; N concurrent
reserves with the same idempotency key).

Runs against REAL Postgres (this repo's local-dev stack — see
local-dev/docker-compose.yml's `postgres` service), not SQLite: SQLite's
`FOR UPDATE` is a documented no-op (see stock_repository.py's module
docstring), so it cannot verify real lock-contention behavior at all —
every assertion in this file would trivially pass against SQLite
regardless of whether the row lock actually serializes concurrent
writers, which is exactly the thing being tested.

Skips automatically (not a failure) if a real Postgres isn't reachable —
this test is not expected to run as part of the ordinary SQLite-only unit
suite; it is run explicitly, against `docker compose up -d postgres`
(see services/local-dev/README.md), as its own gate per the impl-plan's
Acceptance Check (§6): "pytest services/inventory/tests/ passes,
including the concurrency test, before this plan is considered
complete."
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from adapters.stock_repository import SqlAlchemyStockRepository, create_schema, stock_table
from domain.exceptions import InsufficientStockError

_POSTGRES_URL = os.environ.get(
    "INVENTORY_TEST_POSTGRES_URL",
    "postgresql+psycopg2://milkful:milkful@localhost:5432/milkful_inventory",
)


@pytest.fixture(scope="module")
def pg_engine():
    # pool_size generous enough for every thread any test below spawns to
    # hold its own checked-out connection simultaneously without queuing
    # on the pool itself (which would mask, not test, DB-level lock
    # contention).
    engine = create_engine(_POSTGRES_URL, pool_size=40, max_overflow=20)
    try:
        with engine.connect():
            pass
    except OperationalError:
        pytest.skip(
            f"Real Postgres not reachable at {_POSTGRES_URL} — start it via "
            "`docker compose up -d postgres` in services/local-dev/ to run this test."
        )
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def repo(pg_engine):
    return SqlAlchemyStockRepository(pg_engine)


def _seed_product(pg_engine, on_hand: int) -> str:
    product_id = f"concurrency-test-{uuid.uuid4().hex}"
    with pg_engine.begin() as conn:
        conn.execute(
            stock_table.insert().values(product_id=product_id, on_hand=on_hand, reserved=0)
        )
    return product_id


def test_n_concurrent_reserves_against_stock_for_exactly_n_minus_1(repo, pg_engine):
    n = 20
    product_id = _seed_product(pg_engine, on_hand=n - 1)

    def _try_reserve(i: int):
        try:
            repo.reserve(product_id, f"order-{i}", quantity=1, ttl_seconds=900)
            return "success"
        except InsufficientStockError:
            return "insufficient"

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_try_reserve, i) for i in range(n)]
        outcomes = [f.result() for f in as_completed(futures)]

    assert outcomes.count("success") == n - 1
    assert outcomes.count("insufficient") == 1

    stock, _ = repo.get_stock_with_next_batch(product_id)
    assert stock.available == 0  # never negative, and exactly exhausted


def test_n_concurrent_commits_for_the_same_reservation_decrement_on_hand_once(repo, pg_engine):
    n = 15
    product_id = _seed_product(pg_engine, on_hand=10)
    repo.reserve(product_id, "order-shared", quantity=4, ttl_seconds=900)

    def _try_commit(_i: int):
        reservation, _changed = repo.commit_reservation(product_id, "order-shared")
        return reservation.status.value

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_try_commit, i) for i in range(n)]
        results = [f.result() for f in as_completed(futures)]

    assert all(status == "COMMITTED" for status in results)
    stock, _ = repo.get_stock_with_next_batch(product_id)
    assert stock.on_hand == 6  # 10 - 4, decremented exactly once, not n times
    assert stock.reserved == 0


def test_n_concurrent_reserves_with_the_same_idempotency_key(repo, pg_engine):
    n = 15
    product_id = _seed_product(pg_engine, on_hand=100)

    def _try_reserve(_i: int):
        reservation, created = repo.reserve(
            product_id, "order-same-key", quantity=5, ttl_seconds=900
        )
        return reservation.id, created

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_try_reserve, i) for i in range(n)]
        results = [f.result() for f in as_completed(futures)]

    reservation_ids = {r[0] for r in results}
    created_count = sum(1 for _, created in results if created)

    assert len(reservation_ids) == 1  # exactly one reservation row
    assert created_count == 1  # exactly one of the n calls actually created it

    stock, _ = repo.get_stock_with_next_batch(product_id)
    assert stock.reserved == 5  # decremented exactly once, not n times
