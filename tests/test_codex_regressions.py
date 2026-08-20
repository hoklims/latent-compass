"""Independent Codex regressions for the LC-HOK181-P2 review epoch."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from conftest import EPOCH, HOST_ID, STORE_ID, episode_payload, reseal
from latent_compass.authority import Actor, ContinueKill, LifecycleState, authorize_transition
from latent_compass.cli import _write_atomically
from latent_compass.episode import AgentFamily, load_episode
from latent_compass.errors import (
    AuthorityRefusal,
    ContractViolation,
    IntegrityError,
)
from latent_compass.ledger import IntegrityKind, LedgerStore
from latent_compass.protocol import (
    HoldoutLedger,
    MeasurementSet,
    Preregistration,
    Verdict,
    load_verdict,
)


def _fresh_store(root: Path) -> LedgerStore:
    return LedgerStore.create(
        root,
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        epoch=EPOCH,
    )


def _tamper(store: LedgerStore, sql: str, params: tuple[object, ...] = ()) -> None:
    store._conn.execute("BEGIN IMMEDIATE")  # noqa: SLF001 - hostile disk access
    store._conn.execute(sql, params)  # noqa: SLF001 - hostile disk access
    store._conn.execute("COMMIT")  # noqa: SLF001 - hostile disk access


def test_invalid_create_does_not_claim_a_database_or_directory(tmp_path: Path) -> None:
    root = tmp_path / "invalid-ledger"

    with pytest.raises(ContractViolation):
        LedgerStore.create(
            root,
            store_id="",
            host_id=HOST_ID,
            agent_family=AgentFamily.CLAUDE,
            epoch=EPOCH,
        )

    assert not root.exists()
    with _fresh_store(root) as store:
        assert store.verify().ok


def test_append_cannot_launder_a_truncated_tail(tmp_path: Path) -> None:
    with _fresh_store(tmp_path / "ledger") as store:
        for index in (1, 2, 3):
            store.append(load_episode(episode_payload(f"ep-0000000{index}")))
        anchored = store._meta("anchor_seal")  # noqa: SLF001 - attack baseline
        _tamper(store, "DELETE FROM episodes WHERE seq = 3")

        with pytest.raises(IntegrityError):
            store.append(load_episode(episode_payload("ep-00000004")))

        assert store.count() == 2
        assert store._meta("anchor_seal") == anchored  # noqa: SLF001
        assert not store.verify().ok


def test_verify_replay_and_tombstone_verify_inside_their_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _fresh_store(tmp_path / "ledger") as store:
        store.append(load_episode(episode_payload()))
        original = LedgerStore._verify_unlocked  # noqa: SLF001 - transaction oracle
        transaction_states: list[bool] = []

        def observed(self: LedgerStore):  # type: ignore[no-untyped-def]
            transaction_states.append(self._conn.in_transaction)
            return original(self)

        monkeypatch.setattr(LedgerStore, "_verify_unlocked", observed)
        assert store.verify().ok
        assert store.replay().episode_count == 1
        store.tombstone("ep-00000001", reason="retention expiry")

        assert transaction_states == [True, True, True]


def test_append_exposes_no_raw_connection_commit_hook() -> None:
    assert "_before_commit" not in inspect.signature(LedgerStore.append).parameters


def test_nonstandard_nan_json_is_an_integrity_finding(tmp_path: Path) -> None:
    with _fresh_store(tmp_path / "ledger") as store:
        store.append(load_episode(episode_payload()))
        row = store._conn.execute(  # noqa: SLF001 - hostile disk access
            "SELECT payload FROM episodes WHERE seq = 1"
        ).fetchone()
        payload = json.loads(str(row["payload"]))
        payload["economics"]["cost"] = float("nan")
        _tamper(
            store,
            "UPDATE episodes SET payload = ? WHERE seq = 1",
            (json.dumps(payload, allow_nan=True),),
        )

        report = store.verify()
        assert not report.ok
        assert IntegrityKind.PAYLOAD_UNPARSEABLE in {finding.kind for finding in report.findings}


def test_tombstone_on_an_unredacted_episode_is_an_integrity_finding(tmp_path: Path) -> None:
    with _fresh_store(tmp_path / "ledger") as store:
        store.append(load_episode(episode_payload()))
        _tamper(
            store,
            "INSERT INTO tombstones (episode_id, reason, created_at, mode) VALUES (?, ?, ?, ?)",
            ("ep-00000001", "invented", "2026-08-14T13:00:00Z", "TOMBSTONE"),
        )

        report = store.verify()
        assert not report.ok
        assert IntegrityKind.TOMBSTONE_ON_UNREDACTED in {
            finding.kind for finding in report.findings
        }


@pytest.mark.parametrize(("field", "value"), [("reason", ""), ("mode", "STORE_DESTRUCTION")])
def test_invalid_tombstone_contract_is_an_integrity_finding(
    tmp_path: Path, field: str, value: str
) -> None:
    with _fresh_store(tmp_path / field) as store:
        store.append(load_episode(episode_payload()))
        store.tombstone("ep-00000001", reason="retention expiry")
        statement = {
            "reason": "UPDATE tombstones SET reason = ?",
            "mode": "UPDATE tombstones SET mode = ?",
        }[field]
        _tamper(store, statement, (value,))

        report = store.verify()
        assert not report.ok
        assert IntegrityKind.TOMBSTONE_INVALID in {finding.kind for finding in report.findings}


@pytest.mark.parametrize("version", [None, "99.0.0"])
def test_verdict_contract_version_is_required_and_supported(
    holdout_verdict: Verdict, version: str | None
) -> None:
    payload = holdout_verdict.canonical_payload()
    if version is None:
        payload.pop("contract_version")
    else:
        payload["contract_version"] = version

    with pytest.raises(ContractViolation):
        load_verdict(payload)


def test_self_consistent_but_invented_continue_is_refused(
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_ledger: HoldoutLedger,
    holdout_verdict: Verdict,
) -> None:
    invented_metrics = tuple(
        metric.model_copy(update={"aggregate": metric.threshold, "passed": True})
        for metric in holdout_verdict.metrics
    )
    invented = reseal(
        holdout_verdict,
        decision=ContinueKill.CONTINUE,
        metrics=invented_metrics,
        failing_metrics=(),
    )

    with pytest.raises(AuthorityRefusal):
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.PROMOTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=holdout_measurements,
            verdict=invented,
            holdout_ledger=holdout_ledger,
            human_acknowledged=True,
        )


def test_supplied_forged_kill_is_verified_even_when_rejecting(
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_ledger: HoldoutLedger,
    holdout_verdict: Verdict,
) -> None:
    forged = reseal(
        holdout_verdict,
        decision=ContinueKill.KILL,
        failing_metrics=("success-rate",),
    )

    with pytest.raises(AuthorityRefusal):
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.REJECTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=holdout_measurements,
            verdict=forged,
            holdout_ledger=holdout_ledger,
        )


def test_final_holdout_authority_requires_the_matching_consumption_receipt(
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_verdict: Verdict,
    tmp_path: Path,
) -> None:
    with pytest.raises(AuthorityRefusal):
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.PROMOTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=holdout_measurements,
            verdict=holdout_verdict,
            holdout_ledger=HoldoutLedger(tmp_path / "empty.json"),
            human_acknowledged=True,
        )


def test_atomic_writer_never_overwrites_an_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "proof.json"
    _write_atomically(destination, "first")

    with pytest.raises(FileExistsError):
        _write_atomically(destination, "second")

    assert destination.read_text(encoding="utf-8") == "first"
