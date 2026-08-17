"""Hostile regressions for the LC-HOK181-P2 corrective tranche.

Each test here reproduces a defect that the P1 candidate exhibited. Every one
of them failed against that candidate; each is written so it fails again the
moment the defect is reintroduced.

The concurrency tests use real operating-system processes, launched via
``subprocess`` from a worker script written into ``tmp_path``. Threads would
prove nothing about the cross-process guarantee, and a mock would prove nothing
at all.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    EPOCH,
    HOST_ID,
    STORE_ID,
    episode_payload,
    measurement_payload,
    protocol_payload,
    snapshot_tree,
    write_json,
)
from latent_compass.cli import EXIT_INTEGRITY, EXIT_OK, EXIT_REFUSED, EXIT_STORE, main
from latent_compass.episode import REDACTABLE_PATHS, AgentFamily, load_episode
from latent_compass.errors import (
    ContractViolation,
    EpisodeValidationError,
    EpochClosed,
    IntegrityError,
    ProtocolViolation,
    UnsupportedContractVersion,
)
from latent_compass.ledger import DATABASE_FILENAME, IntegrityKind, LedgerStore
from latent_compass.protocol import (
    HoldoutLedger,
    evaluate,
    load_measurement_set,
    load_preregistration,
)

NEXT_EPOCH = "LC-HOK181-E2-0000beef"


def run(*argv: str) -> tuple[int, Any, Any]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, json.loads(out.getvalue() or "null"), json.loads(err.getvalue() or "null")


def fresh_store(root: Path) -> LedgerStore:
    return LedgerStore.create(
        root,
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        epoch=EPOCH,
    )


def raw(store: LedgerStore) -> sqlite3.Connection:
    """The store's own connection: the threat model is direct disk access."""
    return store._conn  # noqa: SLF001


def tamper(store: LedgerStore, sql: str, params: tuple[object, ...] = ()) -> None:
    connection = raw(store)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(sql, params)
    connection.execute("COMMIT")


def spawn(workers: Sequence[tuple[Path, list[str]]]) -> list[subprocess.CompletedProcess[str]]:
    """Start every worker, then wait. Overlap is what makes the race real."""
    running = [
        subprocess.Popen(  # noqa: S603 - fixed interpreter, script written by this test
            [sys.executable, str(script), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for script, args in workers
    ]
    results = []
    for process in running:
        stdout, stderr = process.communicate(timeout=120)
        results.append(
            subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        )
    return results


# ===========================================================================
# HOK-185 — episode contract
# ===========================================================================


def test_an_episode_without_a_schema_version_is_refused() -> None:
    """No version is ever injected on the reader's behalf."""
    payload = episode_payload()
    del payload["schema_version"]
    with pytest.raises(EpisodeValidationError) as refusal:
        load_episode(payload)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == "absent"


def test_an_advisory_without_a_contract_version_is_refused() -> None:
    payload = episode_payload()
    del payload["decision"]["advisory"]["contract_version"]
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_a_nested_advisory_from_the_future_is_refused() -> None:
    """A future version at depth is as unreadable as one at the top level."""
    payload = episode_payload()
    payload["decision"]["advisory"]["contract_version"] = "9.0.0"
    with pytest.raises(UnsupportedContractVersion) as refusal:
        load_episode(payload)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail == {
        "contract": "authority",
        "version": "9.0.0",
        "reason": "future",
        "supported": ["1.0.0", "2.0.0"],
    }


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-14T10:00:00.000Z",
        "2026-08-14T10:00:00.000000Z",
        "2026-08-14T10:00:00+00:00",
        "2026-08-14T10:00:00",
        "2026-08-14T25:00:00Z",
        "2026-02-30T10:00:00Z",
    ],
)
def test_a_sealed_timestamp_has_exactly_one_encoding(timestamp: str) -> None:
    """Same instant, different bytes, different seal — so only one is accepted."""
    payload = episode_payload()
    payload["provenance"]["recorded_at"] = timestamp
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_an_unobserved_outcome_cannot_report_violations() -> None:
    """A violation count is an observation like any other."""
    payload = episode_payload()
    payload["outcome"] = {"observability": "PARTIAL", "observed": False, "violations": 3}
    with pytest.raises(EpisodeValidationError, match="strict contract validation"):
        load_episode(payload)


def test_a_redaction_must_correspond_to_a_real_absence() -> None:
    """The oracle proves the value is gone, not that a marker is present."""
    present = episode_payload(
        redactions=[{"path": "external_verdict", "reason": "operator request"}]
    )
    assert present["external_verdict"] is not None
    with pytest.raises(EpisodeValidationError, match="strict contract validation"):
        load_episode(present)

    absent = episode_payload(
        redactions=[{"path": "external_verdict", "reason": "operator request"}]
    )
    absent["external_verdict"] = None
    episode = load_episode(absent)
    assert episode.external_verdict is None
    assert (
        "external_verdict"
        not in json.dumps(
            {k: v for k, v in episode.canonical_payload().items() if k != "redactions"}
        )
        or episode.canonical_payload()["external_verdict"] is None
    )


def test_only_genuinely_optional_fields_may_be_declared_redacted() -> None:
    for path in ("economics.cost", "episode_id", "state.levels.0.summary_digest"):
        assert path not in REDACTABLE_PATHS
        with pytest.raises(EpisodeValidationError):
            load_episode(episode_payload(redactions=[{"path": path, "reason": "x"}]))


# ===========================================================================
# HOK-186 — protocol
# ===========================================================================


def test_a_protocol_without_a_contract_version_is_refused() -> None:
    payload = protocol_payload()
    del payload["contract_version"]
    with pytest.raises(ProtocolViolation) as refusal:
        load_preregistration(payload)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == "absent"


def test_measurements_must_name_the_pre_registered_corpus(protocol: Any) -> None:
    """Without this binding, any corpus could be scored under any protocol."""
    payload = measurement_payload(protocol.protocol_seal(), corpus_seal="sha256:some-other-corpus")
    with pytest.raises(ProtocolViolation, match="pre-registered corpus"):
        evaluate(protocol, load_measurement_set(payload))


def test_a_measurement_set_without_a_contract_version_is_refused(protocol: Any) -> None:
    payload = measurement_payload(protocol.protocol_seal())
    del payload["contract_version"]
    with pytest.raises(ProtocolViolation):
        load_measurement_set(payload)


def test_a_revision_reusing_the_same_holdout_corpus_cannot_re_arm_it(
    protocol: Any, tmp_path: Path
) -> None:
    """The spent resource is the corpus, not the protocol seal."""
    ledger = HoldoutLedger(tmp_path / "usage.json")
    first = evaluate(
        protocol,
        load_measurement_set(
            measurement_payload(protocol.protocol_seal(), split="HOLDOUT", purpose="FINAL_VERDICT")
        ),
        holdout_ledger=ledger,
        consumed_at="2026-08-14T13:00:00Z",
    )
    assert first.corpus_seal == protocol.holdout_corpus_seal()

    revised = protocol.revise(revision=2, epoch=NEXT_EPOCH)
    assert revised.protocol_seal() != protocol.protocol_seal()
    assert revised.holdout_corpus_seal() == protocol.holdout_corpus_seal()

    with pytest.raises(ProtocolViolation, match="already been consumed"):
        evaluate(
            revised,
            load_measurement_set(
                measurement_payload(
                    revised.protocol_seal(),
                    split="HOLDOUT",
                    purpose="FINAL_VERDICT",
                    epoch=NEXT_EPOCH,
                )
            ),
            holdout_ledger=ledger,
            consumed_at="2026-08-14T14:00:00Z",
        )


def test_a_fresh_holdout_corpus_is_still_spendable(protocol: Any, tmp_path: Path) -> None:
    """The guard binds the corpus, and must not block a genuinely new one."""
    ledger = HoldoutLedger(tmp_path / "usage.json")
    evaluate(
        protocol,
        load_measurement_set(
            measurement_payload(protocol.protocol_seal(), split="HOLDOUT", purpose="FINAL_VERDICT")
        ),
        holdout_ledger=ledger,
        consumed_at="2026-08-14T13:00:00Z",
    )
    splits = [dict(spec) for spec in protocol.canonical_payload()["splits"]]
    for spec in splits:
        if spec["split"] == "HOLDOUT":
            spec["corpus_seal"] = "sha256:holdout-corpus-round-two"
    successor = protocol.revise(revision=2, epoch=NEXT_EPOCH, splits=splits)
    verdict = evaluate(
        successor,
        load_measurement_set(
            measurement_payload(
                successor.protocol_seal(),
                split="HOLDOUT",
                purpose="FINAL_VERDICT",
                epoch=NEXT_EPOCH,
                corpus_seal="sha256:holdout-corpus-round-two",
            )
        ),
        holdout_ledger=ledger,
        consumed_at="2026-08-14T15:00:00Z",
    )
    assert verdict.corpus_seal == "sha256:holdout-corpus-round-two"


def test_every_metric_present_must_carry_every_pre_registered_seed(protocol: Any) -> None:
    """An optional metric scored on one seed is a cherry-picked metric."""
    payload = measurement_payload(protocol.protocol_seal())
    payload["measurements"].append(
        {"metric": "drift-divergence", "split": "VALIDATION", "seed": 11, "value": 0.01}
    )
    with pytest.raises(ProtocolViolation, match="missing measurements for pre-registered seeds"):
        evaluate(protocol, load_measurement_set(payload))


HOLDOUT_WORKER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from latent_compass.protocol import HoldoutLedger
    from latent_compass.errors import ProtocolViolation

    # Widen the read-decide-write window deterministically. Held under the lock,
    # this changes nothing; without the lock it guarantees every process reads
    # "unused" before any of them writes. Process scheduling alone would leave
    # the race to chance, and a race that only sometimes happens is not a proof.
    _original_read = HoldoutLedger._read_unlocked

    def _slow_read(self):
        entries = _original_read(self)
        time.sleep(0.30)
        return entries

    HoldoutLedger._read_unlocked = _slow_read

    ledger = HoldoutLedger(Path(sys.argv[1]))
    corpus = sys.argv[2]

    # Start together: every worker waits for the same wall-clock instant, so the
    # windows overlap by construction rather than by luck.
    start_at = float(sys.argv[4])
    while time.time() < start_at:
        time.sleep(0.005)

    try:
        ledger.consume(
            corpus,
            consumed_at="2026-08-14T13:00:00Z",
            protocol_id="test-protocol",
            protocol_seal="sha256:test-protocol",
            epoch="LC-HOK181-E1-concurrency",
            verdict_seal="sha256:test-verdict",
            measurement_set_seal="sha256:test-measurements",
        )
        print("CONSUMED")
    except ProtocolViolation as exc:
        print("REFUSED|" + exc.message)
    """
)


def test_exactly_one_process_may_spend_a_holdout_corpus(tmp_path: Path) -> None:
    """Cross-process atomicity, proved with real processes."""
    script = tmp_path / "worker.py"
    script.write_text(HOLDOUT_WORKER, encoding="utf-8")
    ledger_path = tmp_path / "usage.json"
    start_at = f"{time.time() + 4.0:.3f}"
    results = spawn(
        [
            (script, [str(ledger_path), "sha256:contended-corpus", f"w{index}", start_at])
            for index in range(6)
        ]
    )
    outcomes = [result.stdout.strip() for result in results]
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    assert outcomes.count("CONSUMED") == 1, outcomes
    refused = [line for line in outcomes if line.startswith("REFUSED|")]
    assert len(refused) == 5, outcomes
    assert all("already been consumed" in line for line in refused), refused
    entries = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert set(entries) == {"sha256:contended-corpus"}


def test_concurrent_consumptions_of_different_corpora_are_all_recorded(tmp_path: Path) -> None:
    """No lost update: a read-modify-write outside a lock would drop entries."""
    script = tmp_path / "worker.py"
    script.write_text(HOLDOUT_WORKER, encoding="utf-8")
    ledger_path = tmp_path / "usage.json"
    corpora = [f"sha256:corpus-{index}" for index in range(8)]
    start_at = f"{time.time() + 4.0:.3f}"
    results = spawn([(script, [str(ledger_path), corpus, corpus, start_at]) for corpus in corpora])
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    assert [result.stdout.strip() for result in results] == ["CONSUMED"] * len(corpora), [
        result.stdout.strip() for result in results
    ]
    entries = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert set(entries) == set(corpora), "a concurrent write was lost"


# ===========================================================================
# HOK-187 — ledger
# ===========================================================================


def test_an_episode_that_bypassed_validation_is_revalidated_on_append(
    store_root: Path,
) -> None:
    """``model_copy`` skips validation entirely, so ``append`` cannot trust it."""
    store = fresh_store(store_root)
    try:
        valid = load_episode(episode_payload())
        # A cross-field invariant, broken with individually valid parts. The
        # receipt never looks at candidates, so only revalidating the episode
        # catches this — an oracle that used episode_id would pass either way,
        # because the receipt validates identifiers itself.
        forged = valid.model_copy(update={"candidates": (valid.candidates[0],)})
        assert len(forged.candidates) == 1
        assert forged.candidates[0].propensity == 0.6, "the distribution no longer sums to 1"

        with pytest.raises(ContractViolation) as refusal:
            store.append(forged)
        assert "propensities must sum" in str(refusal.value.detail)
        assert store.count() == 0
        assert store.verify().ok
    finally:
        store.close()


@pytest.mark.parametrize(
    "produced",
    ["not-a-timestamp", "2026-08-14T12:00:00.000Z", "2026-08-14T12:00:00+00:00", "", "0"],
)
def test_an_invalid_clock_is_refused_before_anything_is_committed(
    store_root: Path, produced: str
) -> None:
    """The P1 defect committed the row, then raised while building the receipt."""
    store = fresh_store(store_root)
    try:
        before = store.root_seal()
        with pytest.raises(ContractViolation):
            store.append(load_episode(episode_payload()), clock=lambda: produced)
        assert store.count() == 0, "a row survived a failed append"
        assert store.root_seal() == before
        assert store.verify().ok
    finally:
        store.close()


def test_a_clock_that_raises_is_a_typed_refusal(store_root: Path) -> None:
    store = fresh_store(store_root)
    try:

        def broken() -> str:
            raise RuntimeError("no clock")

        with pytest.raises(ContractViolation, match="clock raised"):
            store.append(load_episode(episode_payload()), clock=broken)
        assert store.count() == 0
    finally:
        store.close()


def test_admissibility_is_re_read_inside_the_append_transaction(store_root: Path) -> None:
    """No raw connection callback can commit around the append transaction."""
    store = fresh_store(store_root)
    try:
        store.append(load_episode(episode_payload("ep-00000001")))
        store.abandon_epoch(reason="closed by the authority")
        with pytest.raises(EpochClosed):
            store.append(load_episode(episode_payload("ep-00000002")))
        assert store.count() == 1
        assert store.binding().epoch_status.value == "abandoned"
    finally:
        store.close()


def test_relabelling_the_binding_breaks_the_chain(store_root: Path) -> None:
    """Genesis is recomputed from identity, never read back from storage."""
    store = fresh_store(store_root)
    try:
        store.append(load_episode(episode_payload()))
        assert store.verify().ok
        tamper(store, "UPDATE store_meta SET value = 'host-attacker' WHERE key = 'host_id'")
        report = store.verify()
        assert not report.ok
        kinds = {finding.kind for finding in report.findings}
        assert IntegrityKind.CHAIN_BROKEN in kinds
        assert IntegrityKind.PROVENANCE_MISMATCH in kinds
        with pytest.raises(IntegrityError):
            store.replay()
    finally:
        store.close()


def test_deleting_the_last_row_is_detected_by_the_anchor(store_root: Path) -> None:
    """A truncated chain still verifies; a truncated chain plus anchor does not."""
    store = fresh_store(store_root)
    try:
        for index in (1, 2, 3):
            store.append(load_episode(episode_payload(f"ep-0000000{index}")))
        assert store.verify().ok
        tamper(store, "DELETE FROM episodes WHERE seq = 3")
        report = store.verify()
        assert not report.ok
        assert IntegrityKind.ANCHOR_MISMATCH in {finding.kind for finding in report.findings}
        with pytest.raises(IntegrityError):
            store.replay()
    finally:
        store.close()


def test_removing_the_anchor_is_itself_a_finding(store_root: Path) -> None:
    store = fresh_store(store_root)
    try:
        store.append(load_episode(episode_payload()))
        tamper(store, "DELETE FROM store_meta WHERE key = 'anchor_seal'")
        report = store.verify()
        assert not report.ok
        assert IntegrityKind.ANCHOR_MISSING in {finding.kind for finding in report.findings}
    finally:
        store.close()


def test_a_tombstone_cannot_launder_a_corruption(store_root: Path) -> None:
    store = fresh_store(store_root)
    try:
        store.append(load_episode(episode_payload()))
        tamper(store, "UPDATE episodes SET payload = replace(payload, '1.5', '0.1')")
        assert not store.verify().ok
        with pytest.raises(IntegrityError, match="refusing to redact"):
            store.tombstone("ep-00000001", reason="cover the tracks")
        assert store.get("ep-00000001").redacted is False
    finally:
        store.close()


def test_a_corrupt_store_produces_no_export_seal(store_root: Path) -> None:
    store = fresh_store(store_root)
    try:
        store.append(load_episode(episode_payload()))
        tamper(store, "UPDATE episodes SET payload = replace(payload, '1.5', '0.1')")
        with pytest.raises(IntegrityError, match="refusing to export"):
            store.export()
    finally:
        store.close()


def test_an_export_is_internally_consistent(store_root: Path) -> None:
    store = fresh_store(store_root)
    try:
        for index in (1, 2, 3):
            store.append(load_episode(episode_payload(f"ep-0000000{index}")))
        document = store.export()
        assert document.episode_count == len(document.records)
        assert document.root_seal == document.records[-1]["chain_seal"]
        assert [record["seq"] for record in document.records] == [1, 2, 3]
    finally:
        store.close()


APPEND_WORKER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from latent_compass.ledger import LedgerStore
    from latent_compass.episode import load_episode
    import json

    root, payload_dir = sys.argv[1], Path(sys.argv[2])
    start, count = int(sys.argv[3]), int(sys.argv[4])
    store = LedgerStore.open(root)
    try:
        for index in range(start, start + count):
            payload = json.loads((payload_dir / f"ep-{index:08d}.json").read_text("utf-8"))
            try:
                store.append(load_episode(payload))
            except Exception as exc:
                print(type(exc).__name__)
    finally:
        store.close()
    print("DONE")
    """
)


def test_an_export_taken_during_concurrent_appends_is_a_single_state(
    tmp_path: Path, store_root: Path
) -> None:
    """A snapshot must never mix rows from two states."""
    store = fresh_store(store_root)
    store.close()
    payload_dir = tmp_path / "payloads"
    payload_dir.mkdir()
    for index in range(1, 41):
        write_json(payload_dir / f"ep-{index:08d}.json", episode_payload(f"ep-{index:08d}"))

    script = tmp_path / "append_worker.py"
    script.write_text(APPEND_WORKER, encoding="utf-8")
    writer = subprocess.Popen(  # noqa: S603 - fixed interpreter, script written by this test
        [sys.executable, str(script), str(store_root), str(payload_dir), "1", "40"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        snapshots = 0
        for _ in range(25):
            reader = LedgerStore.open(store_root)
            try:
                document = reader.export()
            except Exception:  # noqa: S112 - a busy database is not the property under test
                continue
            finally:
                reader.close()
            snapshots += 1
            assert document.episode_count == len(document.records)
            expected_seqs = list(range(1, len(document.records) + 1))
            assert [record["seq"] for record in document.records] == expected_seqs
            if document.records:
                assert document.root_seal == document.records[-1]["chain_seal"]
    finally:
        writer.communicate(timeout=120)
    assert snapshots >= 1, "no snapshot was taken; the test proved nothing"


CREATE_WORKER = textwrap.dedent(
    """
    import sys
    from latent_compass.ledger import LedgerStore
    from latent_compass.episode import AgentFamily
    from latent_compass.errors import StoreAlreadyExists, LedgerError

    try:
        store = LedgerStore.create(
            sys.argv[1], store_id=sys.argv[2], host_id="host-alpha",
            agent_family=AgentFamily.CLAUDE, epoch="LC-HOK181-E1-7d1fc727",
        )
        store.close()
        print("CREATED")
    except (StoreAlreadyExists, LedgerError) as exc:
        print("REFUSED")
    """
)


def test_a_losing_concurrent_create_never_deletes_the_winners_store(tmp_path: Path) -> None:
    """The loser must not unlink a database it did not create."""
    script = tmp_path / "create_worker.py"
    script.write_text(CREATE_WORKER, encoding="utf-8")
    root = tmp_path / "contended"
    results = spawn([(script, [str(root), f"store-{index}"]) for index in range(6)])
    outcomes = [result.stdout.strip() for result in results]
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    assert outcomes.count("CREATED") == 1, outcomes
    assert (root / DATABASE_FILENAME).is_file(), "the winner's database was deleted"
    store = LedgerStore.open(root)
    try:
        assert store.verify().ok
    finally:
        store.close()


# ===========================================================================
# HOK-187 — CLI containment and error rendering
# ===========================================================================


def initialise(root: Path) -> None:
    assert (
        run(
            "init",
            "--root",
            str(root),
            "--store-id",
            STORE_ID,
            "--host-id",
            HOST_ID,
            "--agent-family",
            "claude",
            "--epoch",
            EPOCH,
        )[0]
        == EXIT_OK
    )


@pytest.mark.parametrize("shape", ["traversal", "absolute", "sibling"])
def test_export_out_cannot_escape_the_ledger_root(tmp_path: Path, shape: str) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "notes.txt").write_text("untouched", encoding="utf-8")
    (tmp_path / "ledger-sibling").mkdir()
    root = tmp_path / "ledger"
    initialise(root)
    episode = write_json(tmp_path / "ep.json", episode_payload())
    assert run("append", "--root", str(root), "--episode", str(episode))[0] == EXIT_OK

    before = snapshot_tree(tmp_path)
    target = {
        "traversal": Path("..") / "sentinel" / "exfiltrated.json",
        "absolute": sentinel / "exfiltrated.json",
        "sibling": tmp_path / "ledger-sibling" / "exfiltrated.json",
    }[shape]

    code, _, err = run("export", "--root", str(root), "--out", str(target))
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"
    assert "inside the root" in err["message"]
    assert snapshot_tree(tmp_path) == before, "a refused export still wrote something"


def test_a_relative_out_lands_where_the_caller_named_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved against the working directory, not against --root.

    Resolving a relative path against the root would still be confined, and
    would silently write to ``<root>/<root>/...`` — somewhere the caller never
    named. Confinement must not be bought with a surprise.
    """
    root = tmp_path / "ledger"
    initialise(root)
    monkeypatch.chdir(tmp_path)
    code, out, _ = run("export", "--root", "ledger", "--out", "ledger/snapshot.json")
    assert code == EXIT_OK
    assert (root / "snapshot.json").is_file()
    assert not (root / "ledger").exists()
    assert Path(out["exported_to"]) == (root / "snapshot.json").resolve()


def test_export_out_refuses_to_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    initialise(root)
    destination = root / "snapshot.json"
    assert run("export", "--root", str(root), "--out", str(destination))[0] == EXIT_OK
    original = destination.read_bytes()

    code, _, err = run("export", "--root", str(root), "--out", str(destination))
    assert code == EXIT_REFUSED
    assert "refusing to overwrite" in err["message"]
    assert destination.read_bytes() == original


def test_export_to_stdout_needs_no_root_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    initialise(root)
    before = snapshot_tree(tmp_path)
    code, out, _ = run("export", "--root", str(root))
    assert code == EXIT_OK
    assert out["export"]["export_seal"].startswith("sha256:")
    assert snapshot_tree(tmp_path) == before


def test_a_holdout_ledger_outside_the_declared_root_is_refused(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    write_root = tmp_path / "protocol-root"
    write_root.mkdir()
    protocol_file = write_json(tmp_path / "protocol.json", protocol_payload())
    _, out, _ = run("protocol", "validate", "--file", str(protocol_file))
    measurements = write_json(
        tmp_path / "m.json",
        measurement_payload(out["protocol_seal"], split="HOLDOUT", purpose="FINAL_VERDICT"),
    )
    before = snapshot_tree(tmp_path)
    code, _, err = run(
        "protocol",
        "verdict",
        "--protocol",
        str(protocol_file),
        "--measurements",
        str(measurements),
        "--root",
        str(write_root),
        "--holdout-ledger",
        str(sentinel / "usage.json"),
    )
    assert code == EXIT_REFUSED
    assert "inside the root" in err["message"]
    assert snapshot_tree(tmp_path) == before


def test_writing_without_a_declared_root_is_refused(tmp_path: Path) -> None:
    protocol_file = write_json(tmp_path / "protocol.json", protocol_payload())
    _, out, _ = run("protocol", "validate", "--file", str(protocol_file))
    measurements = write_json(tmp_path / "m.json", measurement_payload(out["protocol_seal"]))
    code, _, err = run(
        "protocol",
        "verdict",
        "--protocol",
        str(protocol_file),
        "--measurements",
        str(measurements),
        "--out",
        str(tmp_path / "verdict.json"),
    )
    assert code == EXIT_REFUSED
    assert err["detail"]["requires"] == "--root"


def test_a_non_utf8_file_is_refused_with_a_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "episode.json"
    path.write_bytes(b'{"schema_version": "1.0.0", "note": "\xff\xfe not utf-8"}')
    code, _, err = run("validate", "--episode", str(path))
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"
    assert "UTF-8" in err["message"]


def test_non_standard_json_is_refused_with_a_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "episode.json"
    path.write_text("{'schema_version': '1.0.0',}", encoding="utf-8")
    code, _, err = run("validate", "--episode", str(path))
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"
    assert err["detail"]["line"] == 1


def test_a_file_that_is_not_a_sqlite_database_is_a_typed_integrity_error(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ledger"
    root.mkdir()
    (root / DATABASE_FILENAME).write_bytes(b"this is definitely not a sqlite database")
    code, _, err = run("verify", "--root", str(root))
    assert code == EXIT_INTEGRITY
    assert err["error"] == "integrity_error"


def test_a_truncated_database_is_a_typed_error(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    initialise(root)
    database = root / DATABASE_FILENAME
    database.write_bytes(database.read_bytes()[:64])
    code, _, err = run("verify", "--root", str(root))
    assert code in {EXIT_INTEGRITY, EXIT_STORE}
    assert err["error"] in {"integrity_error", "ledger_error"}


def test_a_missing_episode_file_is_a_store_error(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    initialise(root)
    code, _, err = run("append", "--root", str(root), "--episode", str(tmp_path / "absent.json"))
    assert code == EXIT_STORE
    assert err["error"] == "ledger_error"


def test_an_unwritable_export_destination_fails_cleanly(tmp_path: Path) -> None:
    """A directory where a file is expected: an OSError that must not escape."""
    root = tmp_path / "ledger"
    initialise(root)
    blocked = root / "snapshot.json"
    blocked.mkdir()
    code, _, err = run("export", "--root", str(root), "--out", str(blocked))
    assert code in {EXIT_REFUSED, EXIT_STORE}
    assert err["error"] in {"contract_violation", "filesystem_error", "ledger_error"}
    assert "Traceback" not in json.dumps(err)


def test_no_cli_failure_reaches_the_user_as_a_traceback(tmp_path: Path) -> None:
    """Every documented failure path renders as one JSON document."""
    root = tmp_path / "ledger"
    initialise(root)
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    cases: list[tuple[list[str], int]] = [
        (["validate", "--episode", str(broken)], EXIT_REFUSED),
        (["show", "--root", str(root), "--episode-id", "ep-absent"], EXIT_STORE),
        (["list", "--root", str(root), "--limit", "0"], EXIT_REFUSED),
        (["append", "--root", str(tmp_path / "nowhere"), "--episode", str(broken)], EXIT_REFUSED),
    ]
    for argv, expected in cases:
        code, _, err = run(*argv)
        assert code == expected, argv
        assert isinstance(err, dict), argv
        assert "error" in err, argv
        assert "Traceback" not in json.dumps(err)


def test_the_environment_is_never_consulted_for_a_write_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confinement comes from the command line, not from ambient configuration."""
    monkeypatch.setenv("LATENT_COMPASS_ROOT", str(tmp_path / "sentinel"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "sentinel"))
    root = tmp_path / "ledger"
    initialise(root)
    assert not (tmp_path / "sentinel").exists()
    assert os.environ["LATENT_COMPASS_ROOT"] == str(tmp_path / "sentinel")
