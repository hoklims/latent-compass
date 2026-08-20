"""HOK-187 — proofs that the ledger is append-only, atomic and replayable."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import EPOCH, HOST_ID, STORE_ID, episode_payload
from latent_compass.episode import AgentFamily, load_episode
from latent_compass.errors import (
    ContractViolation,
    DuplicateEpisode,
    EpochClosed,
    IntegrityError,
    LedgerError,
    ProvenanceMismatch,
    StoreAlreadyExists,
    StoreNotFound,
)
from latent_compass.ledger import DATABASE_FILENAME, IntegrityKind, LedgerStore

Clock = Callable[[], str]


def raw(store: LedgerStore) -> sqlite3.Connection:
    """The store's own connection.

    Reaching past the API is the point: the threat model is an adversary with
    write access to the database file, and every corruption proof below has to
    write the way that adversary would. Routed through one accessor so the
    private access is declared once instead of scattered.
    """
    return store._conn  # noqa: SLF001 - deliberate: this is the threat model


def tamper(store: LedgerStore, sql: str, params: tuple[object, ...] = ()) -> None:
    """Write straight into the database, as an attacker with disk access would."""
    connection = raw(store)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(sql, params)
    connection.execute("COMMIT")


def append_reference(store: LedgerStore, clock: Clock, episode_id: str = "ep-00000001") -> None:
    store.append(load_episode(episode_payload(episode_id)), clock=clock)


# -- creation and binding --------------------------------------------------


def test_a_new_store_is_bound_to_one_host_family_and_epoch(store: LedgerStore) -> None:
    binding = store.binding()
    assert binding.host_id == HOST_ID
    assert binding.store_id == STORE_ID
    assert binding.epoch == EPOCH
    assert binding.agent_family is AgentFamily.CLAUDE
    assert store.root_seal() == binding.genesis_seal()


def test_creating_a_store_twice_in_one_root_is_refused(store: LedgerStore) -> None:
    with pytest.raises(StoreAlreadyExists):
        LedgerStore.create(
            store.root,
            store_id="other",
            host_id="other-host",
            agent_family=AgentFamily.CODEX,
            epoch=EPOCH,
        )


def test_opening_an_absent_store_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StoreNotFound):
        LedgerStore.open(tmp_path / "nowhere")


def test_an_unknown_ledger_format_refuses_to_open(store: LedgerStore) -> None:
    tamper(
        store,
        "UPDATE store_meta SET value = '9.0.0' WHERE key = 'ledger_format_version'",
    )
    root = store.root
    store.close()
    with pytest.raises(ContractViolation) as refusal:
        LedgerStore.open(root)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == "future"


# -- the write path --------------------------------------------------------


def test_append_returns_a_receipt_and_advances_the_root_seal(
    store: LedgerStore, fixed_clock: Clock
) -> None:
    genesis = store.root_seal()
    receipt = store.append(load_episode(episode_payload()), clock=fixed_clock)
    assert receipt.seq == 1
    assert receipt.root_seal == store.root_seal() != genesis
    assert store.count() == 1


def test_a_duplicate_episode_is_refused_and_changes_nothing(
    store: LedgerStore, fixed_clock: Clock
) -> None:
    append_reference(store, fixed_clock)
    before_seal, before_count = store.root_seal(), store.count()
    with pytest.raises(DuplicateEpisode) as refusal:
        append_reference(store, fixed_clock)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["episode_id"] == "ep-00000001"
    assert (store.root_seal(), store.count()) == (before_seal, before_count)
    assert store.verify().ok


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host_id", "host-beta"),
        ("store_id", "store-beta"),
        ("epoch", "LC-HOK181-E2-deadbeef"),
        ("agent_family", "codex"),
    ],
)
def test_provenance_that_does_not_match_the_store_is_refused(
    store: LedgerStore, fixed_clock: Clock, field: str, value: str
) -> None:
    payload = episode_payload()
    payload["provenance"][field] = value
    before_seal, before_count = store.root_seal(), store.count()
    with pytest.raises(ProvenanceMismatch) as refusal:
        store.append(load_episode(payload), clock=fixed_clock)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["field"] == field
    assert (store.root_seal(), store.count()) == (before_seal, before_count)


def test_a_claude_episode_cannot_enter_a_codex_store(
    tmp_path: Path, store: LedgerStore, fixed_clock: Clock
) -> None:
    """Host stores stay separate, and a refused import leaves both untouched."""
    codex = LedgerStore.create(
        tmp_path / "codex-root",
        store_id="store-codex",
        host_id="host-codex",
        agent_family=AgentFamily.CODEX,
        epoch=EPOCH,
        clock=fixed_clock,
    )
    try:
        append_reference(store, fixed_clock)
        claude_seal, claude_count = store.root_seal(), store.count()
        codex_seal, codex_count = codex.root_seal(), codex.count()

        with pytest.raises(ProvenanceMismatch):
            codex.append(load_episode(episode_payload("ep-00000009")), clock=fixed_clock)

        assert (codex.root_seal(), codex.count()) == (codex_seal, codex_count)
        assert (store.root_seal(), store.count()) == (claude_seal, claude_count)
        assert codex.root_seal() != store.root_seal()
        assert codex.verify().ok
        assert store.verify().ok
    finally:
        codex.close()


def test_an_interrupted_append_leaves_no_partially_accepted_episode(
    store: LedgerStore, fixed_clock: Clock
) -> None:
    """The row exists inside the transaction, then the transaction dies.

    Asserting ``seen_inside`` is what makes this a proof of atomicity rather
    than a proof that the write never started: an implementation that validated
    and returned early would fail here, not pass.
    """
    append_reference(store, fixed_clock)
    before_seal, before_count = store.root_seal(), store.count()
    raw(store).execute(
        """
        CREATE TEMP TRIGGER interrupt_anchor_update
        BEFORE UPDATE ON store_meta
        WHEN OLD.key = 'anchor_count'
         AND (SELECT COUNT(*) FROM episodes) = 2
        BEGIN
            SELECT RAISE(ABORT, 'power loss after episode insert');
        END
        """
    )

    with pytest.raises(LedgerError, match="rejected an operation"):
        store.append(load_episode(episode_payload("ep-00000002")), clock=fixed_clock)

    assert (store.root_seal(), store.count()) == (before_seal, before_count)
    assert store.verify().ok
    with pytest.raises(StoreNotFound):
        store.get("ep-00000002")

    raw(store).execute("DROP TRIGGER interrupt_anchor_update")
    receipt = store.append(load_episode(episode_payload("ep-00000002")), clock=fixed_clock)
    assert receipt.seq == 2, "the interrupted attempt must not have burned a sequence number"


# -- reads -----------------------------------------------------------------


def test_reads_are_bounded(store: LedgerStore, fixed_clock: Clock) -> None:
    for index in range(5):
        store.append(load_episode(episode_payload(f"ep-0000000{index}")), clock=fixed_clock)
    assert [record.seq for record in store.list_records(limit=2)] == [1, 2]
    assert [record.seq for record in store.list_records(limit=2, offset=3)] == [4, 5]
    for limit in (0, -1, 1001):
        with pytest.raises(ContractViolation):
            store.list_records(limit=limit)
    with pytest.raises(ContractViolation):
        store.list_records(offset=-1)


def test_a_stored_episode_round_trips_intact(store: LedgerStore, fixed_clock: Clock) -> None:
    original = load_episode(episode_payload())
    store.append(original, clock=fixed_clock)
    record = store.get("ep-00000001")
    assert record.episode is not None
    assert record.episode.canonical_payload() == original.canonical_payload()
    assert record.content_seal == original.content_seal()


# -- integrity -------------------------------------------------------------


def test_an_empty_store_verifies_against_its_genesis(store: LedgerStore) -> None:
    report = store.verify()
    assert report.ok
    assert report.checked == 0
    assert report.root_seal == store.binding().genesis_seal()


def test_an_altered_payload_is_detected(store: LedgerStore, fixed_clock: Clock) -> None:
    append_reference(store, fixed_clock)
    row = raw(store).execute("SELECT payload FROM episodes WHERE seq = 1").fetchone()
    forged = str(row["payload"]).replace('"cost":1.5', '"cost":0.1')
    assert forged != str(row["payload"])
    tamper(store, "UPDATE episodes SET payload = ? WHERE seq = 1", (forged,))

    report = store.verify()
    assert not report.ok
    assert {finding.kind for finding in report.findings} == {IntegrityKind.PAYLOAD_ALTERED}
    with pytest.raises(IntegrityError):
        store.replay()


def test_altering_payload_and_content_seal_together_breaks_the_chain(
    store: LedgerStore, fixed_clock: Clock
) -> None:
    append_reference(store, fixed_clock)
    forged_payload = episode_payload("ep-00000001")
    forged_payload["economics"] = {
        "cost": 0.1,
        "information_gain": 0.25,
        "reversibility": 0.9,
        "uncertainty": 0.2,
    }
    forged = load_episode(forged_payload)
    import json

    tamper(
        store,
        "UPDATE episodes SET payload = ?, content_seal = ? WHERE seq = 1",
        (
            json.dumps(forged.canonical_payload(), sort_keys=True, separators=(",", ":")),
            forged.content_seal(),
        ),
    )
    report = store.verify()
    assert not report.ok
    assert IntegrityKind.CHAIN_BROKEN in {finding.kind for finding in report.findings}


def test_reordering_two_rows_breaks_the_chain(store: LedgerStore, fixed_clock: Clock) -> None:
    append_reference(store, fixed_clock, "ep-00000001")
    store.append(load_episode(episode_payload("ep-00000002")), clock=fixed_clock)
    rows = (
        raw(store)
        .execute("SELECT seq, episode_id, content_seal, payload FROM episodes ORDER BY seq")
        .fetchall()
    )
    first, second = rows[0], rows[1]
    swap = (
        # The UNIQUE constraint on episode_id forces the swap through a
        # placeholder, which is itself a guard against a naive in-place reorder.
        ("UPDATE episodes SET episode_id = 'ep-swap-temp' WHERE seq = 1", ()),
        (
            "UPDATE episodes SET episode_id = ?, content_seal = ?, payload = ? WHERE seq = 2",
            (first["episode_id"], first["content_seal"], first["payload"]),
        ),
        (
            "UPDATE episodes SET episode_id = ?, content_seal = ?, payload = ? WHERE seq = 1",
            (second["episode_id"], second["content_seal"], second["payload"]),
        ),
    )
    raw(store).execute("BEGIN IMMEDIATE")
    for statement, params in swap:
        raw(store).execute(statement, params)
    raw(store).execute("COMMIT")

    report = store.verify()
    assert not report.ok
    assert IntegrityKind.CHAIN_BROKEN in {finding.kind for finding in report.findings}


def test_a_deleted_row_breaks_the_sequence(store: LedgerStore, fixed_clock: Clock) -> None:
    for index in (1, 2, 3):
        store.append(load_episode(episode_payload(f"ep-0000000{index}")), clock=fixed_clock)
    tamper(store, "DELETE FROM episodes WHERE seq = 2")
    report = store.verify()
    assert not report.ok
    kinds = {finding.kind for finding in report.findings}
    assert IntegrityKind.SEQUENCE_INVALID in kinds
    assert IntegrityKind.CHAIN_BROKEN in kinds


def test_a_payload_dropped_without_a_tombstone_is_reported(
    store: LedgerStore, fixed_clock: Clock
) -> None:
    append_reference(store, fixed_clock)
    tamper(store, "UPDATE episodes SET payload = NULL, redacted = 1 WHERE seq = 1")
    report = store.verify()
    assert not report.ok
    assert IntegrityKind.REDACTED_WITHOUT_TOMBSTONE in {finding.kind for finding in report.findings}


def test_a_full_rewrite_by_an_administrator_is_not_detected(
    tmp_path: Path, fixed_clock: Clock
) -> None:
    """The documented limit of a hash chain, proved rather than merely claimed.

    Two stores with the same binding and different histories both verify. The
    chain establishes internal consistency, never authenticity; detecting a
    wholesale forgery needs an external anchor this package does not have.
    """

    def build(root: Path, episode_id: str) -> tuple[bool, str]:
        built = LedgerStore.create(
            root,
            store_id=STORE_ID,
            host_id=HOST_ID,
            agent_family=AgentFamily.CLAUDE,
            epoch=EPOCH,
            clock=fixed_clock,
        )
        try:
            built.append(load_episode(episode_payload(episode_id)), clock=fixed_clock)
            return built.verify().ok, built.root_seal()
        finally:
            built.close()

    genuine_ok, genuine_seal = build(tmp_path / "genuine", "ep-00000001")
    forged_ok, forged_seal = build(tmp_path / "forged", "ep-00000042")
    assert genuine_ok
    assert forged_ok
    assert genuine_seal != forged_seal


# -- replay and export -----------------------------------------------------


def test_replay_reproduces_the_root_seal(store: LedgerStore, fixed_clock: Clock) -> None:
    for index in (1, 2, 3):
        store.append(load_episode(episode_payload(f"ep-0000000{index}")), clock=fixed_clock)
    report = store.replay()
    assert report.episode_count == 3
    assert report.root_seal == store.root_seal()
    assert report.replayed_episode_ids == ("ep-00000001", "ep-00000002", "ep-00000003")
    assert store.replay().replay_digest == report.replay_digest


def test_identical_content_yields_identical_seals_across_stores(
    tmp_path: Path, fixed_clock: Clock
) -> None:
    def build(root: Path, cost: float) -> tuple[str, str, str]:
        built = LedgerStore.create(
            root,
            store_id=STORE_ID,
            host_id=HOST_ID,
            agent_family=AgentFamily.CLAUDE,
            epoch=EPOCH,
            clock=fixed_clock,
        )
        try:
            for index in (1, 2):
                payload = episode_payload(f"ep-0000000{index}")
                payload["economics"]["cost"] = cost
                built.append(load_episode(payload), clock=fixed_clock)
            return (
                built.root_seal(),
                built.replay().replay_digest,
                built.export().export_seal,
            )
        finally:
            built.close()

    left = build(tmp_path / "left", 1.5)
    right = build(tmp_path / "right", 1.5)
    different = build(tmp_path / "different", 1.6)

    assert left == right
    assert left != different


def test_export_is_stable_and_carries_the_root_seal(store: LedgerStore, fixed_clock: Clock) -> None:
    append_reference(store, fixed_clock)
    first = store.export()
    second = store.export()
    assert first.export_seal == second.export_seal
    assert first.root_seal == store.root_seal()
    assert first.episode_count == 1
    assert first.records[0]["episode_id"] == "ep-00000001"


# -- governance ------------------------------------------------------------


def test_a_tombstone_redacts_the_payload_without_rewriting_history(
    store: LedgerStore, fixed_clock: Clock
) -> None:
    append_reference(store, fixed_clock, "ep-00000001")
    store.append(load_episode(episode_payload("ep-00000002")), clock=fixed_clock)
    before_seal = store.root_seal()
    before_digest = store.replay().replay_digest

    stone = store.tombstone("ep-00000001", reason="subject erasure request", clock=fixed_clock)
    assert stone.episode_id == "ep-00000001"

    assert store.root_seal() == before_seal, "redaction must not rewrite the chain"
    report = store.verify()
    assert report.ok
    assert report.redacted == 1
    assert store.count() == 2, "the row survives; only the payload is gone"

    record = store.get("ep-00000001")
    assert record.redacted is True
    assert record.episode is None
    assert record.tombstone is not None
    assert record.tombstone.reason == "subject erasure request"

    replayed = store.replay()
    assert replayed.redacted_episode_ids == ("ep-00000001",)
    assert replayed.replayed_episode_ids == ("ep-00000002",)
    assert replayed.replay_digest != before_digest, "a redaction is visible, not silent"
    assert replayed.root_seal == before_seal


def test_a_tombstone_is_not_a_second_deletion_route(store: LedgerStore, fixed_clock: Clock) -> None:
    append_reference(store, fixed_clock)
    store.tombstone("ep-00000001", reason="first", clock=fixed_clock)
    with pytest.raises(LedgerError, match="already redacted"):
        store.tombstone("ep-00000001", reason="second", clock=fixed_clock)
    with pytest.raises(StoreNotFound):
        store.tombstone("ep-does-not-exist", reason="none", clock=fixed_clock)


def test_a_tombstone_appears_in_the_export(store: LedgerStore, fixed_clock: Clock) -> None:
    append_reference(store, fixed_clock)
    store.tombstone("ep-00000001", reason="subject erasure request", clock=fixed_clock)
    document = store.export()
    assert len(document.tombstones) == 1
    assert document.records[0]["payload"] is None
    assert document.records[0]["redacted"] is True


def test_abandoning_an_epoch_stops_appends_and_preserves_everything(
    store: LedgerStore, fixed_clock: Clock
) -> None:
    append_reference(store, fixed_clock)
    before_seal, before_count = store.root_seal(), store.count()

    binding = store.abandon_epoch(reason="corpus superseded", clock=fixed_clock)
    assert binding.epoch_status.value == "abandoned"

    with pytest.raises(EpochClosed):
        store.append(load_episode(episode_payload("ep-00000002")), clock=fixed_clock)
    with pytest.raises(EpochClosed):
        store.abandon_epoch(reason="again", clock=fixed_clock)

    assert (store.root_seal(), store.count()) == (before_seal, before_count)
    assert store.verify().ok
    assert store.replay().episode_count == 1


def test_a_store_is_a_single_file_in_its_root(store: LedgerStore, fixed_clock: Clock) -> None:
    append_reference(store, fixed_clock)
    assert sorted(path.name for path in store.root.iterdir()) == [DATABASE_FILENAME]
