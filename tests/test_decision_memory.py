"""HOK-243 — proofs that the strategic decision memory is admissible, durable and bounded.

Every refusal proof asserts the same three counters afterwards. A store that
refuses correctly but moves its record count, generation or root seal on the way
has still let the caller change durable state, which is the failure this contract
exists to prevent.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    DECISION_EXPIRES_AT,
    DECISION_ID,
    DECISION_REVIEW_DUE_AT,
    EPOCH,
    HOST_ID,
    strategic_decision_payload,
)
from latent_compass.decision_memory import (
    DecisionBinding,
    DecisionMemoryStore,
    DecisionOriginKind,
    admit_strategic_decision,
)
from latent_compass.decision_memory.store import (
    DECISION_DATABASE_FILENAME,
    DecisionIntegrityKind,
)
from latent_compass.episode import AgentFamily
from latent_compass.errors import (
    ContractViolation,
    DecisionMemoryViolation,
    DecisionRevisionConflict,
    EpochClosed,
    IntegrityError,
    LedgerError,
    ProvenanceMismatch,
    SensitiveContentRefused,
    StoreNotFound,
    TransferRefused,
    UnsupportedContractVersion,
)

Clock = Callable[[], str]

BEFORE_REVIEW = "2026-09-01T00:00:00Z"
AFTER_REVIEW = "2026-12-01T00:00:00Z"
AFTER_EXPIRY = "2027-09-01T00:00:00Z"


def raw(store: DecisionMemoryStore) -> sqlite3.Connection:
    """The store's own connection.

    Reaching past the API is the point: the threat model is an adversary with
    write access to the database file, and every corruption proof below has to
    write the way that adversary would.
    """
    return store._conn  # noqa: SLF001 - deliberate: this is the threat model


def tamper(store: DecisionMemoryStore, sql: str, params: tuple[object, ...] = ()) -> None:
    connection = raw(store)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(sql, params)
    connection.execute("COMMIT")


def snapshot(store: DecisionMemoryStore) -> tuple[int, int, str]:
    """The three counters a refusal must never move."""
    return store.record_count(), store.generation(), store.root_seal()


def append_initial(store: DecisionMemoryStore, clock: Clock, **overrides: Any) -> tuple[str, int]:
    receipt = store.append_decision(strategic_decision_payload(**overrides), clock=clock)
    return receipt.content_seal, receipt.generation


# -- admission --------------------------------------------------------------


def test_a_complete_record_is_admitted_and_seals_reproducibly() -> None:
    record = admit_strategic_decision(strategic_decision_payload())
    again = admit_strategic_decision(strategic_decision_payload())
    assert record.record_seal() == again.record_seal()
    assert record.record_seal().startswith("sha256:")
    # Domain separation: the record and the projection it embeds seal apart even
    # though one canonically contains the other.
    assert record.record_seal() != record.projection.projection_seal()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("impact_class", "ROUTINE"),
        ("sensitivity", "UNKNOWN"),
        ("sensitivity", "HOLDOUT"),
    ],
)
def test_only_an_explicit_strategic_non_sensitive_opt_in_is_admitted(
    field: str, value: str
) -> None:
    payload = strategic_decision_payload()
    payload[field] = value
    with pytest.raises(SensitiveContentRefused):
        admit_strategic_decision(payload)


@pytest.mark.parametrize("field", ["impact_class", "sensitivity"])
def test_an_absent_classification_is_refused_rather_than_assumed(field: str) -> None:
    payload = strategic_decision_payload()
    del payload[field]
    with pytest.raises(SensitiveContentRefused):
        admit_strategic_decision(payload)


@pytest.mark.parametrize(
    "name",
    ["verdict", "outcome", "selected_direction_id", "score", "holdout", "api_key", "token"],
)
def test_a_forbidden_field_name_is_refused_at_the_top_level(name: str) -> None:
    payload = strategic_decision_payload()
    payload[name] = "anything"
    with pytest.raises(SensitiveContentRefused) as refusal:
        admit_strategic_decision(payload)
    assert refusal.value.code == "sensitive_content_refused"


def test_a_forbidden_field_name_is_refused_at_any_depth() -> None:
    payload = strategic_decision_payload()
    payload["projection"]["candidates"][0]["selected_route"] = "direction-alpha"
    with pytest.raises(SensitiveContentRefused):
        admit_strategic_decision(payload)


@pytest.mark.parametrize(
    "needle",
    [
        # Assembled at runtime so this module does not contain the literals the
        # repository's own secret scan forbids, and therefore match itself.
        "-----BEGIN " + "RSA PRIVATE KEY-----",
        "Authorization" + ": Basic " + ("a" * 24),
        "bearer " + "a" * 24,
        "AKIA" + "B" * 16,
        "ghp" + "_" + "c" * 30,
        "sk" + "-" + "d" * 30,
        "xoxb" + "-" + "1234567890abcdef",
        "eyJ" + "a" * 12 + "." + "b" * 12 + ".signature",
        "password" + "=" + "hunter2hunter2",
    ],
)
def test_a_known_credential_shape_is_refused(needle: str) -> None:
    payload = strategic_decision_payload()
    payload["projection"]["objective_summary"] = f"Choose a direction. {needle}"
    with pytest.raises(SensitiveContentRefused) as refusal:
        admit_strategic_decision(payload)
    assert isinstance(refusal.value.detail, dict)
    assert "shape" in refusal.value.detail


def test_the_refusal_never_echoes_the_material_it_refused() -> None:
    needle = "AKIA" + "E" * 16
    payload = strategic_decision_payload()
    payload["projection"]["objective_summary"] = needle
    with pytest.raises(SensitiveContentRefused) as refusal:
        admit_strategic_decision(payload)
    assert needle not in repr(refusal.value.detail)
    assert needle not in refusal.value.message


def test_oversized_text_is_refused_before_validation() -> None:
    payload = strategic_decision_payload()
    payload["projection"]["objective_summary"] = "a" * 4000
    with pytest.raises(SensitiveContentRefused):
        admit_strategic_decision(payload)


def test_deep_nesting_is_a_typed_refusal_before_canonicalisation() -> None:
    payload: object = strategic_decision_payload()
    for _ in range(1200):
        payload = {"nested": payload}
    with pytest.raises(DecisionMemoryViolation) as refusal:
        admit_strategic_decision(payload)
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["max_nesting_depth"] == 32


@pytest.mark.parametrize(
    "case",
    ["valid-shape", "credential", "forbidden-field", "oversized-text", "deep-nesting"],
)
def test_non_json_tuples_are_refused_before_their_content_can_bypass_screening(
    case: str,
) -> None:
    payload = strategic_decision_payload()
    projection = payload["projection"]
    if case == "valid-shape":
        projection["context_facts"] = tuple(projection["context_facts"])
    elif case == "credential":
        parameter = projection["candidates"][0]["parameters"][0]
        parameter["name"] = "AKIA" + "T" * 16
        projection["candidates"][0]["parameters"] = (parameter,)
    elif case == "forbidden-field":
        fact = {**projection["context_facts"][0], "score": 1}
        projection["context_facts"] = (fact,)
    elif case == "oversized-text":
        fact = {**projection["context_facts"][0], "statement": "x" * 4000}
        projection["context_facts"] = (fact,)
    else:
        nested: object = {"leaf": True}
        for _ in range(64):
            nested = {"nested": nested}
        projection["context_facts"] = (nested,)

    with pytest.raises(DecisionMemoryViolation) as refusal:
        admit_strategic_decision(payload)
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["received_type"] == "tuple"


def test_a_tuple_refusal_cannot_move_the_store(
    decision_store: DecisionMemoryStore, decision_root: Path, fixed_clock: Clock
) -> None:
    payload = strategic_decision_payload()
    payload["projection"]["context_facts"] = tuple(payload["projection"]["context_facts"])
    before = snapshot(decision_store)
    database = decision_root / DECISION_DATABASE_FILENAME
    before_bytes = database.read_bytes()

    with pytest.raises(DecisionMemoryViolation):
        decision_store.append_decision(payload, clock=fixed_clock)

    assert snapshot(decision_store) == before
    assert database.read_bytes() == before_bytes
    assert decision_store.verify().ok


def test_the_deadlines_must_be_ordered() -> None:
    with pytest.raises(DecisionMemoryViolation):
        admit_strategic_decision(strategic_decision_payload(review_due_at="2026-08-15T09:00:00Z"))
    with pytest.raises(DecisionMemoryViolation):
        admit_strategic_decision(strategic_decision_payload(expires_at="2026-10-01T09:00:00Z"))


def test_the_embedded_projection_must_describe_this_decision() -> None:
    payload = strategic_decision_payload()
    payload["projection"]["decision_point_id"] = "decision-other-0002"
    with pytest.raises(DecisionMemoryViolation):
        admit_strategic_decision(payload)


def test_the_projection_must_stay_judgeable() -> None:
    """Every candidate keeps all five dimensions, and a pair is the minimum."""
    single = strategic_decision_payload()
    single["projection"]["candidates"] = single["projection"]["candidates"][:1]
    with pytest.raises(DecisionMemoryViolation):
        admit_strategic_decision(single)

    incomplete = strategic_decision_payload()
    incomplete["projection"]["candidates"][0]["evidence"] = incomplete["projection"]["candidates"][
        0
    ]["evidence"][:4]
    with pytest.raises(DecisionMemoryViolation):
        admit_strategic_decision(incomplete)


def test_a_revision_declares_exactly_one_superseding_shape() -> None:
    with pytest.raises(DecisionMemoryViolation):
        admit_strategic_decision(
            strategic_decision_payload(supersedes_revision_seal="sha256:" + "a" * 64)
        )
    with pytest.raises(DecisionMemoryViolation):
        admit_strategic_decision(strategic_decision_payload(revision=2))


def test_an_unknown_contract_version_is_refused_as_such() -> None:
    with pytest.raises(UnsupportedContractVersion):
        admit_strategic_decision(strategic_decision_payload(contract_version="2.0.0"))


def test_a_store_root_symlink_is_refused_before_any_database_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContractViolation) as refusal:
        DecisionMemoryStore.create(
            redirected,
            store_id="store-test",
            host_id="host-test",
            agent_family=AgentFamily.CODEX,
            epoch="LC-E1",
        )
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "reparse_root"
    assert not (outside / DECISION_DATABASE_FILENAME).exists()


def test_a_database_file_symlink_is_refused_on_open(tmp_path: Path) -> None:
    outside_root = tmp_path / "outside-store"
    outside = DecisionMemoryStore.create(
        outside_root,
        store_id="store-outside",
        host_id="host-test",
        agent_family=AgentFamily.CODEX,
        epoch="LC-E1",
    )
    outside.close()
    root = tmp_path / "real-root"
    root.mkdir()
    (root / DECISION_DATABASE_FILENAME).symlink_to(outside_root / DECISION_DATABASE_FILENAME)

    with pytest.raises(ContractViolation) as refusal:
        DecisionMemoryStore.open(root)
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "reparse_database"


def test_named_credentials_are_refused_on_every_durable_metadata_surface(
    decision_store: DecisionMemoryStore, fixed_clock: Clock, tmp_path: Path
) -> None:
    needle = "AKIA" + "Z" * 16
    fresh_root = tmp_path / "credential-root"
    with pytest.raises(SensitiveContentRefused):
        DecisionMemoryStore.create(
            fresh_root,
            store_id="store-test",
            host_id=needle,
            agent_family=AgentFamily.CODEX,
            epoch="LC-E1",
        )
    assert not fresh_root.exists()

    append_initial(decision_store, fixed_clock)
    before = snapshot(decision_store)
    operations = (
        lambda: decision_store.revoke(
            DECISION_ID, reason=needle, revoked_by="operator-alpha", clock=fixed_clock
        ),
        lambda: decision_store.revoke(
            DECISION_ID, reason="superseded", revoked_by=needle, clock=fixed_clock
        ),
        lambda: decision_store.tombstone(DECISION_ID, 1, reason=needle, clock=fixed_clock),
        lambda: decision_store.abandon_epoch(reason=needle, clock=fixed_clock),
        lambda: decision_store.export_transfer(
            DECISION_ID,
            destination=DecisionBinding(
                host_id="host-target",
                agent_family=AgentFamily.CODEX,
                store_id="store-target",
                epoch="LC-E1",
            ),
            exported_by=needle,
            clock=fixed_clock,
        ),
    )
    for operation in operations:
        with pytest.raises(SensitiveContentRefused):
            operation()
        assert snapshot(decision_store) == before
    assert decision_store.verify().ok


# -- append, revisions and compare-and-set ----------------------------------


def test_the_initial_revision_lands_and_becomes_the_active_head(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    seal, generation = append_initial(decision_store, fixed_clock)
    assert generation == 1
    assert decision_store.verify().ok

    active = decision_store.get_active(DECISION_ID, as_of=BEFORE_REVIEW)
    assert (active.revision, active.origin_kind, active.redacted) == (
        1,
        DecisionOriginKind.NATIVE,
        False,
    )
    assert active.record_seal == seal
    assert active.review_overdue is False
    assert active.record is not None
    # The record carries no route, outcome or authorisation to carry.
    assert set(active.record.canonical_payload()) == {
        "binding",
        "captured_at",
        "contract_version",
        "decision_authority",
        "decision_id",
        "expires_at",
        "impact_class",
        "projection",
        "review_due_at",
        "revision",
        "sensitivity",
        "supersedes_revision_seal",
    }


def test_a_revision_must_link_to_the_exact_prior_revision_seal(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    seal, _ = append_initial(decision_store, fixed_clock)
    receipt = decision_store.append_decision(
        strategic_decision_payload(revision=2, supersedes_revision_seal=seal),
        clock=fixed_clock,
    )
    assert (receipt.revision, receipt.generation, receipt.record_count) == (2, 2, 2)
    assert decision_store.verify().ok
    assert decision_store.get_active(DECISION_ID, as_of=BEFORE_REVIEW).revision == 2


@pytest.mark.parametrize(
    ("revision", "use_head_seal"),
    [
        (1, False),  # replayed initial revision
        (2, False),  # right revision, wrong predecessor seal
        (3, True),  # fork past the head
    ],
)
def test_a_stale_or_forked_revision_is_refused_without_moving_the_store(
    decision_store: DecisionMemoryStore,
    fixed_clock: Clock,
    revision: int,
    use_head_seal: bool,
) -> None:
    head_seal, _ = append_initial(decision_store, fixed_clock)
    before = snapshot(decision_store)
    offered = head_seal if use_head_seal else "sha256:" + "f" * 64

    with pytest.raises(DecisionRevisionConflict):
        decision_store.append_decision(
            strategic_decision_payload(
                revision=revision,
                supersedes_revision_seal=None if revision == 1 else offered,
            ),
            clock=fixed_clock,
        )

    assert snapshot(decision_store) == before
    assert decision_store.verify().ok


def test_initial_history_is_never_replaced(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    seal, _ = append_initial(decision_store, fixed_clock)
    decision_store.append_decision(
        strategic_decision_payload(revision=2, supersedes_revision_seal=seal), clock=fixed_clock
    )
    before = snapshot(decision_store)

    with pytest.raises(DecisionRevisionConflict):
        decision_store.append_decision(
            strategic_decision_payload(captured_at="2026-08-17T09:00:00Z"), clock=fixed_clock
        )

    assert snapshot(decision_store) == before
    # Revision 1 is still readable, unchanged, at its original seal.
    assert (
        raw(decision_store)
        .execute("SELECT content_seal FROM events WHERE event_id = ?", (f"{DECISION_ID}:r1",))
        .fetchone()["content_seal"]
        == seal
    )


def test_expected_generation_is_a_compare_and_set(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    before = snapshot(decision_store)

    with pytest.raises(DecisionRevisionConflict) as refusal:
        decision_store.append_decision(
            strategic_decision_payload("decision-strategic-0002"),
            expected_generation=0,
            clock=fixed_clock,
        )
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "generation_mismatch"
    assert snapshot(decision_store) == before

    receipt = decision_store.append_decision(
        strategic_decision_payload("decision-strategic-0002"),
        expected_generation=1,
        clock=fixed_clock,
    )
    assert receipt.generation == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host_id", "host-beta"),
        ("store_id", "store-beta"),
        ("epoch", "LC-HOK181-E2-deadbeef"),
        ("agent_family", "codex"),
    ],
)
def test_a_record_bound_elsewhere_is_refused(
    decision_store: DecisionMemoryStore, fixed_clock: Clock, field: str, value: str
) -> None:
    before = snapshot(decision_store)
    payload = strategic_decision_payload()
    payload["binding"][field] = value
    with pytest.raises(ProvenanceMismatch):
        decision_store.append_decision(payload, clock=fixed_clock)
    assert snapshot(decision_store) == before


def test_an_abandoned_epoch_accepts_no_further_write(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    decision_store.abandon_epoch(reason="collection run void", clock=fixed_clock)
    before = snapshot(decision_store)

    with pytest.raises(EpochClosed):
        decision_store.append_decision(
            strategic_decision_payload("decision-strategic-0002"), clock=fixed_clock
        )
    assert snapshot(decision_store) == before
    assert decision_store.verify().ok


# -- store separation and Codex/Claude parity -------------------------------


def open_codex_store(root: Path, clock: Clock, *, name: str = "codex-root") -> DecisionMemoryStore:
    """A Codex-family memory in its own directory, bound to its own store id."""
    return DecisionMemoryStore.create(
        root / name,
        store_id="store-codex",
        host_id=HOST_ID,
        agent_family=AgentFamily.CODEX,
        epoch=EPOCH,
        clock=clock,
    )


def test_codex_and_claude_memories_are_separate_files_with_parallel_semantics(
    decision_store: DecisionMemoryStore, tmp_path: Path, fixed_clock: Clock
) -> None:
    """Same decision, two families: identical semantics, no shared byte or seal."""
    codex = open_codex_store(tmp_path, fixed_clock)
    try:
        claude_receipt = decision_store.append_decision(
            strategic_decision_payload(), clock=fixed_clock
        )
        codex_receipt = codex.append_decision(
            strategic_decision_payload(store_id="store-codex", agent_family="codex"),
            clock=fixed_clock,
        )

        # Parity: the same lifecycle position, the same counters, the same view.
        assert (claude_receipt.seq, claude_receipt.generation, claude_receipt.record_count) == (
            codex_receipt.seq,
            codex_receipt.generation,
            codex_receipt.record_count,
        )
        claude_active = decision_store.get_active(DECISION_ID, as_of=BEFORE_REVIEW)
        codex_active = codex.get_active(DECISION_ID, as_of=BEFORE_REVIEW)
        assert claude_active.record is not None
        assert codex_active.record is not None
        assert (
            claude_active.record.projection.canonical_payload()
            == codex_active.record.projection.canonical_payload()
        )
        assert claude_active.review_due_at == codex_active.review_due_at

        # Separation: different files, different genesis, different seals.
        assert decision_store.root != codex.root
        assert decision_store.binding().genesis_seal() != codex.binding().genesis_seal()
        assert claude_receipt.content_seal != codex_receipt.content_seal
        assert decision_store.root_seal() != codex.root_seal()
        assert decision_store.verify().ok
        assert codex.verify().ok

        # Nothing crosses implicitly: the Codex record is not admissible here.
        before = snapshot(decision_store)
        with pytest.raises(ProvenanceMismatch):
            decision_store.append_decision(
                strategic_decision_payload(
                    "decision-strategic-0002", store_id="store-codex", agent_family="codex"
                ),
                clock=fixed_clock,
            )
        assert snapshot(decision_store) == before
    finally:
        codex.close()


# -- revocation, redaction and expiry ---------------------------------------


def test_revocation_excludes_from_the_active_view_and_removes_nothing(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    seal, _ = append_initial(decision_store, fixed_clock)
    receipt = decision_store.revoke(
        DECISION_ID,
        reason="superseded by a wider programme",
        revoked_by="operator-alpha",
        clock=fixed_clock,
    )
    assert receipt.generation == 2
    assert decision_store.verify().ok
    assert decision_store.verify().revoked_count == 1

    with pytest.raises(StoreNotFound) as absence:
        decision_store.get_active(DECISION_ID, as_of=BEFORE_REVIEW)
    assert isinstance(absence.value.detail, dict)
    assert absence.value.detail["reason"] == "revoked"
    assert decision_store.list_active(as_of=BEFORE_REVIEW) == ()

    # The history is intact: the revision still carries its payload and its seal.
    row = (
        raw(decision_store)
        .execute(
            "SELECT payload, content_seal FROM events WHERE event_id = ?", (f"{DECISION_ID}:r1",)
        )
        .fetchone()
    )
    assert row["payload"] is not None
    assert row["content_seal"] == seal


def test_a_revoked_decision_can_be_neither_revised_nor_revoked_again(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    seal, _ = append_initial(decision_store, fixed_clock)
    decision_store.revoke(
        DECISION_ID, reason="void", revoked_by="operator-alpha", clock=fixed_clock
    )
    before = snapshot(decision_store)

    with pytest.raises(DecisionRevisionConflict):
        decision_store.append_decision(
            strategic_decision_payload(revision=2, supersedes_revision_seal=seal), clock=fixed_clock
        )
    with pytest.raises(DecisionRevisionConflict):
        decision_store.revoke(
            DECISION_ID, reason="again", revoked_by="operator-alpha", clock=fixed_clock
        )
    assert snapshot(decision_store) == before


def test_a_tombstone_drops_the_payload_and_leaves_the_rest_of_history_standing(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    seal, _ = append_initial(decision_store, fixed_clock)
    decision_store.append_decision(
        strategic_decision_payload(revision=2, supersedes_revision_seal=seal), clock=fixed_clock
    )
    root_before = decision_store.root_seal()

    receipt = decision_store.tombstone(
        DECISION_ID, 1, reason="subject erasure request", clock=fixed_clock
    )
    assert receipt.generation == 3
    # The redaction is itself an appended event, so the root advances rather than
    # the earlier history being quietly rewritten.
    assert decision_store.root_seal() != root_before

    report = decision_store.verify()
    assert report.ok
    assert (report.redacted_count, report.record_count) == (1, 2)

    row = (
        raw(decision_store)
        .execute(
            "SELECT payload, content_seal, chain_seal FROM events WHERE event_id = ?",
            (f"{DECISION_ID}:r1",),
        )
        .fetchone()
    )
    assert row["payload"] is None
    assert row["content_seal"] == seal

    with pytest.raises(DecisionRevisionConflict):
        decision_store.tombstone(DECISION_ID, 1, reason="again", clock=fixed_clock)


def test_a_tombstoned_head_stays_visible_and_says_its_payload_is_gone(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    decision_store.tombstone(DECISION_ID, 1, reason="erasure", clock=fixed_clock)

    active = decision_store.get_active(DECISION_ID, as_of=BEFORE_REVIEW)
    assert (active.redacted, active.record, active.expires_at) == (True, None, None)
    assert decision_store.verify().ok


def test_expiry_excludes_from_the_active_view_without_deleting_anything(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    before = snapshot(decision_store)

    assert decision_store.get_active(DECISION_ID, as_of=BEFORE_REVIEW).review_overdue is False
    assert decision_store.get_active(DECISION_ID, as_of=AFTER_REVIEW).review_overdue is True
    assert decision_store.get_active(DECISION_ID, as_of=DECISION_EXPIRES_AT).expires_at == (
        DECISION_EXPIRES_AT
    )

    with pytest.raises(StoreNotFound) as absence:
        decision_store.get_active(DECISION_ID, as_of=AFTER_EXPIRY)
    assert isinstance(absence.value.detail, dict)
    assert absence.value.detail["reason"] == "expired"
    assert decision_store.list_active(as_of=AFTER_EXPIRY) == ()
    assert decision_store.list_active(as_of=DECISION_REVIEW_DUE_AT) != ()
    assert snapshot(decision_store) == before


def test_an_as_of_that_is_not_a_canonical_instant_is_refused(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    for instant in ("2026-09-01", "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00.000Z"):
        with pytest.raises(ContractViolation):
            decision_store.get_active(DECISION_ID, as_of=instant)


def test_reads_are_bounded(decision_store: DecisionMemoryStore, fixed_clock: Clock) -> None:
    for index in range(4):
        append_initial(decision_store, fixed_clock, decision_id=f"decision-strategic-000{index}")
    assert len(decision_store.list_active(as_of=BEFORE_REVIEW, limit=2)) == 2
    for limit in (0, -1, 1001):
        with pytest.raises(ContractViolation):
            decision_store.list_active(as_of=BEFORE_REVIEW, limit=limit)
    with pytest.raises(ContractViolation):
        decision_store.list_active(as_of=BEFORE_REVIEW, offset=-1)


# -- integrity, interruption and concurrency --------------------------------


def test_an_altered_stored_payload_is_detected(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    altered = strategic_decision_payload(decision_authority="operator-impostor")
    tamper(
        decision_store,
        "UPDATE events SET payload = ? WHERE event_id = ?",
        (json.dumps(altered, sort_keys=True), f"{DECISION_ID}:r1"),
    )
    report = decision_store.verify()
    assert not report.ok
    assert DecisionIntegrityKind.PAYLOAD_ALTERED in {finding.kind for finding in report.findings}


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("decision_id", "decision-strategic-9999"),
        ("revision", 9),
        ("origin_kind", "FOREIGN_READ_ONLY"),
        ("origin_kind", "FORGED"),
    ],
)
def test_altering_event_identity_or_provenance_breaks_integrity(
    decision_store: DecisionMemoryStore,
    fixed_clock: Clock,
    column: str,
    value: object,
) -> None:
    append_initial(decision_store, fixed_clock)
    tamper(
        decision_store,
        f"UPDATE events SET {column} = ? WHERE seq = 1",  # noqa: S608 - closed test data
        (value,),
    )
    report = decision_store.verify()
    assert not report.ok
    assert DecisionIntegrityKind.CHAIN_BROKEN in {finding.kind for finding in report.findings}


def test_moving_a_revocation_to_another_decision_breaks_integrity(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    append_initial(decision_store, fixed_clock, decision_id="decision-strategic-0002")
    decision_store.revoke(
        DECISION_ID, reason="superseded", revoked_by="operator-alpha", clock=fixed_clock
    )
    tamper(
        decision_store,
        "UPDATE events SET decision_id = ? WHERE event_kind = 'REVOCATION'",
        ("decision-strategic-0002",),
    )
    assert not decision_store.verify().ok


def test_a_truncated_suffix_is_detected_by_the_anchor(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    seal, _ = append_initial(decision_store, fixed_clock)
    decision_store.append_decision(
        strategic_decision_payload(revision=2, supersedes_revision_seal=seal), clock=fixed_clock
    )
    tamper(decision_store, "DELETE FROM events WHERE seq = 2")
    report = decision_store.verify()
    assert not report.ok
    assert DecisionIntegrityKind.ANCHOR_MISMATCH in {finding.kind for finding in report.findings}


@pytest.mark.parametrize(
    "corruption_sql",
    [
        pytest.param("DELETE FROM events WHERE seq = 2", id="truncated-suffix"),
        pytest.param(
            "UPDATE store_meta SET value = 'sha256:" + "0" * 64 + "' WHERE key = 'anchor_tail'",
            id="divergent-anchor",
        ),
    ],
)
def test_abandon_refuses_to_reanchor_a_corrupt_store(
    decision_store: DecisionMemoryStore,
    decision_root: Path,
    fixed_clock: Clock,
    corruption_sql: str,
) -> None:
    seal, _ = append_initial(decision_store, fixed_clock)
    decision_store.append_decision(
        strategic_decision_payload(revision=2, supersedes_revision_seal=seal), clock=fixed_clock
    )
    tamper(decision_store, corruption_sql)
    database = decision_root / DECISION_DATABASE_FILENAME
    before_bytes = database.read_bytes()
    before = snapshot(decision_store)

    with pytest.raises(IntegrityError):
        decision_store.abandon_epoch(reason="stop after corruption", clock=fixed_clock)

    assert database.read_bytes() == before_bytes
    assert snapshot(decision_store) == before
    assert decision_store.binding().epoch_status.value == "open"
    assert not decision_store.verify().ok


def test_relabelling_the_host_breaks_the_chain_instead_of_renaming_history(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    tamper(decision_store, "UPDATE store_meta SET value = 'host-beta' WHERE key = 'host_id'")
    report = decision_store.verify()
    assert not report.ok
    kinds = {finding.kind for finding in report.findings}
    assert DecisionIntegrityKind.CHAIN_BROKEN in kinds
    assert DecisionIntegrityKind.BINDING_MISMATCH in kinds


def test_a_store_that_does_not_verify_refuses_every_further_write(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    tamper(decision_store, "UPDATE events SET appended_at = '2020-01-01T00:00:00Z' WHERE seq = 1")
    with pytest.raises(IntegrityError):
        decision_store.append_decision(
            strategic_decision_payload("decision-strategic-0002"), clock=fixed_clock
        )
    with pytest.raises(IntegrityError):
        decision_store.revoke(
            DECISION_ID, reason="x", revoked_by="operator-alpha", clock=fixed_clock
        )


def test_an_interrupted_append_leaves_no_partially_accepted_decision(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    """The row exists inside the transaction, then the transaction dies.

    A store that validated and returned early would pass a "nothing was written"
    assertion trivially; aborting on the *anchor update*, after the insert, is
    what makes this a proof of atomicity.
    """
    append_initial(decision_store, fixed_clock)
    before = snapshot(decision_store)
    raw(decision_store).execute(
        """
        CREATE TEMP TRIGGER interrupt_anchor_update
        BEFORE UPDATE ON store_meta
        WHEN OLD.key = 'anchor_generation'
         AND (SELECT COUNT(*) FROM events) = 2
        BEGIN
            SELECT RAISE(ABORT, 'power loss after event insert');
        END
        """
    )

    with pytest.raises(LedgerError, match="rejected an operation"):
        decision_store.append_decision(
            strategic_decision_payload("decision-strategic-0002"), clock=fixed_clock
        )

    assert snapshot(decision_store) == before
    assert decision_store.verify().ok
    with pytest.raises(StoreNotFound):
        decision_store.get_active("decision-strategic-0002", as_of=BEFORE_REVIEW)

    raw(decision_store).execute("DROP TRIGGER interrupt_anchor_update")
    receipt = decision_store.append_decision(
        strategic_decision_payload("decision-strategic-0002"), clock=fixed_clock
    )
    assert receipt.seq == 2, "the interrupted attempt must not have burned a sequence number"


def test_a_concurrent_writer_is_refused_rather_than_interleaved(
    decision_store: DecisionMemoryStore, decision_root: Path, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    before = snapshot(decision_store)
    rival = DecisionMemoryStore.open(decision_root)
    try:
        raw(decision_store).execute("BEGIN IMMEDIATE")
        with pytest.raises(LedgerError):
            rival.append_decision(
                strategic_decision_payload("decision-strategic-0002"), clock=fixed_clock
            )
        raw(decision_store).execute("ROLLBACK")
        assert snapshot(decision_store) == before
    finally:
        rival.close()


def test_a_colliding_event_identity_is_refused(
    decision_store: DecisionMemoryStore, decision_root: Path, fixed_clock: Clock
) -> None:
    """Two writers racing the same revision: the loser is refused, not merged."""
    append_initial(decision_store, fixed_clock)
    before = snapshot(decision_store)
    with pytest.raises(DecisionRevisionConflict):
        decision_store.append_decision(strategic_decision_payload(), clock=fixed_clock)
    assert snapshot(decision_store) == before
    assert (decision_root / DECISION_DATABASE_FILENAME).is_file()


# -- explicit transfer ------------------------------------------------------


def foreign_binding() -> DecisionBinding:
    return DecisionBinding(
        host_id=HOST_ID, agent_family=AgentFamily.CODEX, store_id="store-codex", epoch=EPOCH
    )


def open_codex_target(tmp_path: Path, clock: Clock) -> DecisionMemoryStore:
    return open_codex_store(tmp_path, clock, name="codex-target")


def test_an_envelope_binds_its_source_and_its_one_destination(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    seal, _ = append_initial(decision_store, fixed_clock)
    envelope = decision_store.export_transfer(
        DECISION_ID,
        destination=foreign_binding(),
        exported_by="operator-alpha",
        clock=fixed_clock,
    )
    assert envelope.source_record_seal == seal
    assert envelope.source_root_seal == decision_store.root_seal()
    assert envelope.destination_binding == foreign_binding()
    # Exporting writes nothing.
    assert snapshot(decision_store) == (1, 1, decision_store.root_seal())


def test_a_transfer_to_this_very_store_is_refused(
    decision_store: DecisionMemoryStore, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    with pytest.raises(TransferRefused):
        decision_store.export_transfer(
            DECISION_ID,
            destination=decision_store.binding().binding_ref(),
            exported_by="operator-alpha",
            clock=fixed_clock,
        )


def test_an_imported_decision_is_foreign_read_only(
    decision_store: DecisionMemoryStore, tmp_path: Path, fixed_clock: Clock
) -> None:
    seal, _ = append_initial(decision_store, fixed_clock)
    envelope = decision_store.export_transfer(
        DECISION_ID, destination=foreign_binding(), exported_by="operator-alpha", clock=fixed_clock
    )
    target = open_codex_target(tmp_path, fixed_clock)
    try:
        receipt = target.import_transfer(envelope.canonical_payload(), clock=fixed_clock)
        assert receipt.append.origin_kind is DecisionOriginKind.FOREIGN_READ_ONLY
        assert receipt.source_record_seal == seal
        assert target.verify().ok
        assert target.verify().foreign_count == 1

        # Imported content is never folded into the native view.
        assert target.list_active(as_of=BEFORE_REVIEW) == ()
        foreign = target.list_active(
            as_of=BEFORE_REVIEW, origin_kind=DecisionOriginKind.FOREIGN_READ_ONLY
        )
        assert [entry.decision_id for entry in foreign] == [DECISION_ID]

        # It cannot be revised here, and it is never re-exported from here.
        before = snapshot(target)
        with pytest.raises(DecisionRevisionConflict):
            target.append_decision(
                strategic_decision_payload(
                    revision=2,
                    supersedes_revision_seal=seal,
                    store_id="store-codex",
                    agent_family="codex",
                ),
                clock=fixed_clock,
            )
        with pytest.raises(TransferRefused):
            target.export_transfer(
                DECISION_ID,
                destination=decision_store.binding().binding_ref(),
                exported_by="operator-beta",
                clock=fixed_clock,
            )
        assert snapshot(target) == before
    finally:
        target.close()


def test_an_envelope_addressed_elsewhere_is_refused(
    decision_store: DecisionMemoryStore, tmp_path: Path, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    envelope = decision_store.export_transfer(
        DECISION_ID,
        destination=DecisionBinding(
            host_id="host-elsewhere",
            agent_family=AgentFamily.CODEX,
            store_id="store-codex",
            epoch=EPOCH,
        ),
        exported_by="operator-alpha",
        clock=fixed_clock,
    )
    target = open_codex_target(tmp_path, fixed_clock)
    try:
        before = snapshot(target)
        with pytest.raises(TransferRefused) as refusal:
            target.import_transfer(envelope.canonical_payload(), clock=fixed_clock)
        assert isinstance(refusal.value.detail, dict)
        assert refusal.value.detail["reason"] == "destination_mismatch"
        assert snapshot(target) == before
    finally:
        target.close()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("exported_by",), "operator-impostor"),
        (("source_root_seal",), "sha256:" + "0" * 64),
        (("record", "decision_authority"), "operator-impostor"),
    ],
)
def test_a_tampered_envelope_fails_closed(
    decision_store: DecisionMemoryStore,
    tmp_path: Path,
    fixed_clock: Clock,
    path: tuple[str, ...],
    value: str,
) -> None:
    append_initial(decision_store, fixed_clock)
    document = decision_store.export_transfer(
        DECISION_ID, destination=foreign_binding(), exported_by="operator-alpha", clock=fixed_clock
    ).canonical_payload()
    cursor: Any = document
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value

    target = open_codex_target(tmp_path, fixed_clock)
    try:
        before = snapshot(target)
        with pytest.raises(TransferRefused):
            target.import_transfer(document, clock=fixed_clock)
        assert snapshot(target) == before
        assert target.verify().ok
    finally:
        target.close()


def test_a_replayed_envelope_is_refused(
    decision_store: DecisionMemoryStore, tmp_path: Path, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    envelope = decision_store.export_transfer(
        DECISION_ID, destination=foreign_binding(), exported_by="operator-alpha", clock=fixed_clock
    )
    reissued = decision_store.export_transfer(
        DECISION_ID, destination=foreign_binding(), exported_by="operator-beta", clock=fixed_clock
    )
    assert reissued.transfer_seal != envelope.transfer_seal

    target = open_codex_target(tmp_path, fixed_clock)
    try:
        target.import_transfer(envelope.canonical_payload(), clock=fixed_clock)
        after_first = snapshot(target)

        with pytest.raises(TransferRefused):
            target.import_transfer(envelope.canonical_payload(), clock=fixed_clock)
        # A fresh envelope over the same source record is still a replay.
        with pytest.raises(TransferRefused):
            target.import_transfer(reissued.canonical_payload(), clock=fixed_clock)
        assert snapshot(target) == after_first
    finally:
        target.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("transfer_seal", "sha256:" + "0" * 64),
        ("imported_at", "2026-08-17T10:00:00Z"),
    ],
)
def test_altering_import_metadata_breaks_integrity(
    decision_store: DecisionMemoryStore,
    tmp_path: Path,
    fixed_clock: Clock,
    column: str,
    value: str,
) -> None:
    append_initial(decision_store, fixed_clock)
    envelope = decision_store.export_transfer(
        DECISION_ID,
        destination=foreign_binding(),
        exported_by="operator-alpha",
        clock=fixed_clock,
    )
    target = open_codex_target(tmp_path, fixed_clock)
    try:
        target.import_transfer(envelope.canonical_payload(), clock=fixed_clock)
        tamper(
            target,
            f"UPDATE imports SET {column} = ?",  # noqa: S608 - closed test data
            (value,),
        )
        report = target.verify()
        assert not report.ok
        assert DecisionIntegrityKind.IMPORT_MISMATCH in {
            finding.kind for finding in report.findings
        }
    finally:
        target.close()


def test_an_imported_identity_never_shadows_a_native_one(
    decision_store: DecisionMemoryStore, tmp_path: Path, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    envelope = decision_store.export_transfer(
        DECISION_ID, destination=foreign_binding(), exported_by="operator-alpha", clock=fixed_clock
    )
    target = open_codex_target(tmp_path, fixed_clock)
    try:
        target.append_decision(
            strategic_decision_payload(store_id="store-codex", agent_family="codex"),
            clock=fixed_clock,
        )
        before = snapshot(target)
        with pytest.raises(TransferRefused) as refusal:
            target.import_transfer(envelope.canonical_payload(), clock=fixed_clock)
        assert isinstance(refusal.value.detail, dict)
        assert refusal.value.detail["reason"] == "identity_collision"
        assert snapshot(target) == before
    finally:
        target.close()


def test_an_import_is_screened_by_the_same_admission_as_a_native_append(
    decision_store: DecisionMemoryStore, tmp_path: Path, fixed_clock: Clock
) -> None:
    append_initial(decision_store, fixed_clock)
    document = decision_store.export_transfer(
        DECISION_ID, destination=foreign_binding(), exported_by="operator-alpha", clock=fixed_clock
    ).canonical_payload()
    carried: Any = document["record"]
    carried["sensitivity"] = "HOLDOUT"

    target = open_codex_target(tmp_path, fixed_clock)
    try:
        before = snapshot(target)
        with pytest.raises(SensitiveContentRefused):
            target.import_transfer(document, clock=fixed_clock)
        assert snapshot(target) == before
    finally:
        target.close()
