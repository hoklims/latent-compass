"""Hostile and functional coverage for latent_compass.lab.source_session.

Every test drives real ``tmp_path`` reads through the full bridge — model,
snapshot capture, catalog and core state — never a stubbed source layer,
except the one test that isolates the same-read digest comparison from the
snapshot-drift pre-check by neutralising ``revalidate_source_snapshot``
specifically (see its docstring for why that isolation is necessary).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from latent_compass.episode import AgentFamily
from latent_compass.lab.errors import (
    LabCrossModelStateError,
    LabRepeatedProbeError,
    LabReplayViolationError,
)
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.observations import (
    DriftStatus,
    FileRevalidationResult,
    FileRevalidationStatus,
    HostBinding,
    LiteralMatchReport,
    SourceFileIdentity,
    SourceObservationViolation,
    SourceSnapshot,
    SourceSnapshotRevalidation,
    capture_source_snapshot,
    observe_literal_matches,
)
from latent_compass.lab.source_session import (
    LabSourceSessionViolationError,
    derive_lab_binding,
    load_source_probe_catalog,
    observe_probe_from_source,
)
from latent_compass.lab.state import initial_state

OBSERVED_AT = "2026-09-18T00:00:00Z"
HOST_ID = "host-alpha"
ROOT_ID = "root-alpha"


@pytest.mark.parametrize(
    "alias", ["a/./b", "a//b", "a\\b", "C:\\outside", "C:outside", "a/", "./a", "a/../b"]
)
def test_source_and_catalog_refuse_noncanonical_path_labels(alias: str) -> None:
    with pytest.raises(ValidationError):
        SourceFileIdentity(relative_path=alias, byte_digest="sha256:" + "a" * 64, size_bytes=0)
    payload = catalog_payload()
    payload["probes"][0]["relative_paths"] = [alias]
    with pytest.raises(LabSourceSessionViolationError):
        load_source_probe_catalog(payload)


def test_bridge_revalidates_noncanonical_manifest_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "file-a.txt", "TARGET-A\n")
    _write(tmp_path, "file-b.txt", "TARGET-B\n")
    snapshot = _capture(tmp_path)
    catalog = load_source_probe_catalog(catalog_payload())
    m = model()
    prior = initial_state(
        m, state_id="episode-alias", binding=derive_lab_binding(m, snapshot, catalog)
    )
    alias = "./file-a.txt"
    bad_snapshot = snapshot.model_copy(
        update={
            "manifest": (
                snapshot.manifest[0].model_copy(update={"relative_path": alias}),
                snapshot.manifest[1],
            )
        }
    )
    bad_catalog = catalog.model_copy(
        update={
            "probes": (
                catalog.probes[0].model_copy(update={"relative_paths": (alias,)}),
                catalog.probes[1],
            )
        }
    )

    def unexpected_read(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("noncanonical manifest reached filesystem revalidation")

    monkeypatch.setattr(
        "latent_compass.lab.source_session.revalidate_source_snapshot", unexpected_read
    )
    with pytest.raises(LabSourceSessionViolationError):
        _observe(
            m,
            prior,
            root=tmp_path,
            snapshot=bad_snapshot,
            catalog=bad_catalog,
            probe_id="check-a",
            observation_id="obs-alias",
        )


def test_drift_after_real_literal_read_is_refused_and_prior_remains_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "TARGET-A\n"
    _write(tmp_path, "file-a.txt", original)
    _write(tmp_path, "file-b.txt", "TARGET-B\n")
    snapshot = _capture(tmp_path)
    catalog = load_source_probe_catalog(catalog_payload())
    m = model()
    prior = initial_state(
        m, state_id="episode-post-drift", binding=derive_lab_binding(m, snapshot, catalog)
    )

    def read_then_change(*args: Any, **kwargs: Any) -> LiteralMatchReport:
        report = observe_literal_matches(*args, **kwargs)
        _write(tmp_path, "file-a.txt", "changed after the real read\n")
        return report

    with monkeypatch.context() as patch:
        patch.setattr("latent_compass.lab.source_session.observe_literal_matches", read_then_change)
        with pytest.raises(LabSourceSessionViolationError) as excinfo:
            _observe(
                m,
                prior,
                root=tmp_path,
                snapshot=snapshot,
                catalog=catalog,
                probe_id="check-a",
                observation_id="obs-post-drift",
            )
        assert isinstance(excinfo.value.detail, dict)
        assert excinfo.value.detail["reason"] == "post_read_drift"
    _write(tmp_path, "file-a.txt", original)
    recovered = _observe(
        m,
        prior,
        root=tmp_path,
        snapshot=snapshot,
        catalog=catalog,
        probe_id="check-a",
        observation_id="obs-recovered",
    )
    assert recovered.state.revision == 1
    assert recovered.outcome_id == "a-hi"


def two_probe_model_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": "1.0.0",
        "model_id": "source-session-test-model",
        "worlds": [
            {"id": "world-lo-lo", "weight": 1},
            {"id": "world-lo-hi", "weight": 1},
            {"id": "world-hi-lo", "weight": 1},
            {"id": "world-hi-hi", "weight": 1},
        ],
        "probes": [
            {
                "id": "check-a",
                "cost": 1,
                "outcome_space": ["a-lo", "a-hi"],
                "outcomes": {
                    "world-lo-lo": "a-lo",
                    "world-lo-hi": "a-lo",
                    "world-hi-lo": "a-hi",
                    "world-hi-hi": "a-hi",
                },
            },
            {
                "id": "check-b",
                "cost": 1,
                "outcome_space": ["b-lo", "b-hi"],
                "outcomes": {
                    "world-lo-lo": "b-lo",
                    "world-hi-lo": "b-lo",
                    "world-lo-hi": "b-hi",
                    "world-hi-hi": "b-hi",
                },
            },
        ],
        "decisions": [
            {
                "id": "decide-equal",
                "losses": {
                    "world-lo-lo": 0,
                    "world-hi-hi": 0,
                    "world-lo-hi": 10,
                    "world-hi-lo": 10,
                },
                "required_evidence": [],
            },
            {
                "id": "decide-abstain",
                "losses": {
                    "world-lo-lo": 3,
                    "world-lo-hi": 3,
                    "world-hi-lo": 3,
                    "world-hi-hi": 3,
                },
                "required_evidence": [],
            },
        ],
        "abstain_decision_id": "decide-abstain",
    }
    payload.update(overrides)
    return payload


def model() -> DiagnosisModel:
    return load_model(two_probe_model_payload())


def catalog_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": "1.0.0",
        "probes": [
            {
                "probe_id": "check-a",
                "relative_paths": ["file-a.txt"],
                "query": "TARGET-A",
                "present_outcome_id": "a-hi",
                "absent_outcome_id": "a-lo",
            },
            {
                "probe_id": "check-b",
                "relative_paths": ["file-b.txt"],
                "query": "TARGET-B",
                "present_outcome_id": "b-hi",
                "absent_outcome_id": "b-lo",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _write(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8")


def _host(agent_family: AgentFamily = AgentFamily.CLAUDE) -> HostBinding:
    return HostBinding(host_id=HOST_ID, agent_family=agent_family)


def _capture(root: Path, *, agent_family: AgentFamily = AgentFamily.CLAUDE) -> SourceSnapshot:
    return capture_source_snapshot(
        root,
        [Path("file-a.txt"), Path("file-b.txt")],
        host=_host(agent_family),
        root_id=ROOT_ID,
        captured_at=OBSERVED_AT,
        max_bytes_per_file=4096,
    )


def _observe(
    m: DiagnosisModel,
    prior: Any,
    *,
    root: Path,
    snapshot: SourceSnapshot,
    catalog: Any,
    probe_id: str,
    observation_id: str,
    observed_at: str = OBSERVED_AT,
    expected_host_id: str = HOST_ID,
    expected_agent_family: AgentFamily = AgentFamily.CLAUDE,
    expected_root_id: str = ROOT_ID,
    history: Any = None,
) -> Any:
    return observe_probe_from_source(
        m,
        prior,
        root=root,
        snapshot=snapshot,
        catalog=catalog,
        probe_id=probe_id,
        observation_id=observation_id,
        observed_at=observed_at,
        expected_host_id=expected_host_id,
        expected_agent_family=expected_agent_family,
        expected_root_id=expected_root_id,
        history=history,
    )


def test_a_match_maps_to_the_present_outcome(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter\n")
    _write(tmp_path, "file-b.txt", "nothing here\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-present", binding=binding)

    result = _observe(
        m,
        zero,
        root=tmp_path,
        snapshot=snap,
        catalog=cat,
        probe_id="check-a",
        observation_id="obs-1",
    )

    assert result.outcome_id == "a-hi"
    assert result.state.revision == 1
    assert result.match_report.results[0].match_count == 1


def test_zero_matches_across_selected_files_maps_to_the_absent_outcome(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "nothing relevant here\n")
    _write(tmp_path, "file-b.txt", "nothing here either\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-absent", binding=binding)

    result = _observe(
        m,
        zero,
        root=tmp_path,
        snapshot=snap,
        catalog=cat,
        probe_id="check-a",
        observation_id="obs-1",
    )

    assert result.outcome_id == "a-lo"
    assert result.state.revision == 1
    assert result.match_report.results[0].match_count == 0


def test_legitimate_two_probe_continuation_narrows_to_one_world(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter\n")
    _write(tmp_path, "file-b.txt", "before\nTARGET-B\nafter\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-continuation", binding=binding)

    first = _observe(
        m,
        zero,
        root=tmp_path,
        snapshot=snap,
        catalog=cat,
        probe_id="check-a",
        observation_id="obs-1",
    )
    assert first.outcome_id == "a-hi"
    assert first.state.revision == 1

    second = _observe(
        m,
        first.state,
        root=tmp_path,
        snapshot=snap,
        catalog=cat,
        probe_id="check-b",
        observation_id="obs-2",
        observed_at="2026-09-18T00:05:00Z",
        history=[zero, first.state],
    )

    assert second.outcome_id == "b-hi"
    assert second.state.revision == 2
    assert dict(second.state.posterior_weights) == {"world-hi-hi": 1}


def test_repeated_probe_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter\n")
    _write(tmp_path, "file-b.txt", "nothing here\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-repeat", binding=binding)

    first = _observe(
        m,
        zero,
        root=tmp_path,
        snapshot=snap,
        catalog=cat,
        probe_id="check-a",
        observation_id="obs-1",
    )

    with pytest.raises(LabRepeatedProbeError):
        _observe(
            m,
            first.state,
            root=tmp_path,
            snapshot=snap,
            catalog=cat,
            probe_id="check-a",
            observation_id="obs-2",
            history=[zero, first.state],
        )


def test_cross_model_state_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter\n")
    _write(tmp_path, "file-b.txt", "nothing here\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-cross-model", binding=binding)
    other_model = load_model(two_probe_model_payload(model_id="a-different-model"))

    with pytest.raises(LabCrossModelStateError):
        _observe(
            other_model,
            zero,
            root=tmp_path,
            snapshot=snap,
            catalog=cat,
            probe_id="check-a",
            observation_id="obs-1",
        )


def test_a_tampered_state_fails_replay_and_leaves_state_untouched(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter\n")
    _write(tmp_path, "file-b.txt", "nothing here\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-tamper", binding=binding)
    tampered = zero.model_copy(update={"posterior_weights": {"world-lo-lo": 999}})

    with pytest.raises(LabReplayViolationError):
        _observe(
            m,
            tampered,
            root=tmp_path,
            snapshot=snap,
            catalog=cat,
            probe_id="check-a",
            observation_id="obs-tamper",
        )

    # The untampered prior is untouched and still replays legitimately.
    result = _observe(
        m,
        zero,
        root=tmp_path,
        snapshot=snap,
        catalog=cat,
        probe_id="check-a",
        observation_id="obs-recovered",
    )
    assert result.state.revision == 1


def test_host_scope_mismatch_is_refused_before_any_read(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter\n")
    _write(tmp_path, "file-b.txt", "nothing here\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-host-mismatch", binding=binding)

    with pytest.raises(SourceObservationViolation):
        _observe(
            m,
            zero,
            root=tmp_path,
            snapshot=snap,
            catalog=cat,
            probe_id="check-a",
            observation_id="obs-1",
            expected_host_id="a-different-host",
        )


def test_root_scope_mismatch_is_refused_before_any_read(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter\n")
    _write(tmp_path, "file-b.txt", "nothing here\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-root-mismatch", binding=binding)

    with pytest.raises(SourceObservationViolation):
        _observe(
            m,
            zero,
            root=tmp_path,
            snapshot=snap,
            catalog=cat,
            probe_id="check-a",
            observation_id="obs-1",
            expected_root_id="a-different-root",
        )


def test_pre_read_drift_is_refused_and_prior_state_remains_usable(tmp_path: Path) -> None:
    """Also exercises "changed bytes, same match line": ``file-a.txt`` grows a
    line *after* the matching one, so the matching line number would be
    unchanged if it were reread naively — the whole-manifest drift check
    catches the byte change regardless."""
    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter\n")
    _write(tmp_path, "file-b.txt", "nothing here\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-pre-drift", binding=binding)

    original = (tmp_path / "file-a.txt").read_text(encoding="utf-8")
    _write(tmp_path, "file-a.txt", original + "an-appended-unrelated-line\n")

    with pytest.raises(LabSourceSessionViolationError) as excinfo:
        _observe(
            m,
            zero,
            root=tmp_path,
            snapshot=snap,
            catalog=cat,
            probe_id="check-a",
            observation_id="obs-drifted",
        )
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["reason"] == "pre_read_drift"

    # Reverting the mutation proves the prior state and snapshot were never
    # consumed or corrupted by the refusal.
    _write(tmp_path, "file-a.txt", original)
    result = _observe(
        m,
        zero,
        root=tmp_path,
        snapshot=snap,
        catalog=cat,
        probe_id="check-a",
        observation_id="obs-recovered",
    )
    assert result.outcome_id == "a-hi"
    assert result.state.revision == 1


def test_binary_content_is_refused_and_never_mapped_to_absent(tmp_path: Path) -> None:
    (tmp_path / "file-a.txt").write_bytes(b"\xff\xfe\x00binary-not-utf8")
    _write(tmp_path, "file-b.txt", "nothing here\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-binary", binding=binding)

    with pytest.raises(LabSourceSessionViolationError) as excinfo:
        _observe(
            m,
            zero,
            root=tmp_path,
            snapshot=snap,
            catalog=cat,
            probe_id="check-a",
            observation_id="obs-binary",
        )
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["reason"] == "binary_or_invalid_utf8"

    # The prior state is still usable for the other, unaffected probe.
    result = _observe(
        m,
        zero,
        root=tmp_path,
        snapshot=snap,
        catalog=cat,
        probe_id="check-b",
        observation_id="obs-b",
    )
    assert result.outcome_id == "b-lo"
    assert result.state.revision == 1


def _always_unchanged_revalidation(
    root: Path,
    snapshot: SourceSnapshot,
    *,
    host: HostBinding,
    root_id: str,
    revalidated_at: str,
    current_manifest_targets: Any = None,
) -> SourceSnapshotRevalidation:
    return SourceSnapshotRevalidation(
        contract_version="1.0.0",
        host=host,
        root_id=root_id,
        snapshot_seal=snapshot.manifest_seal(),
        revalidated_at=revalidated_at,
        results=tuple(
            FileRevalidationResult(
                relative_path=identity.relative_path,
                status=FileRevalidationStatus.UNCHANGED,
                previous_byte_digest=identity.byte_digest,
                current_byte_digest=identity.byte_digest,
                refusal_reason=None,
            )
            for identity in snapshot.manifest
        ),
        added_relative_paths=(),
        removed_relative_paths=(),
        status=DriftStatus.UNCHANGED,
    )


def test_same_read_digest_mismatch_is_refused_independently_of_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolates the SAME-READ byte_digest/size compare from the snapshot-drift
    pre/post check: with ``revalidate_source_snapshot`` neutralised to always
    report ``UNCHANGED``, a file mutated after capture can only be caught by
    comparing the literal-match read's own digest against the manifest."""
    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter\n")
    _write(tmp_path, "file-b.txt", "nothing here\n")
    snap = _capture(tmp_path)
    cat = load_source_probe_catalog(catalog_payload())
    m = model()
    binding = derive_lab_binding(m, snap, cat)
    zero = initial_state(m, state_id="episode-same-read", binding=binding)

    _write(tmp_path, "file-a.txt", "before\nTARGET-A\nafter-but-mutated\n")
    monkeypatch.setattr(
        "latent_compass.lab.source_session.revalidate_source_snapshot",
        _always_unchanged_revalidation,
    )

    with pytest.raises(LabSourceSessionViolationError) as excinfo:
        _observe(
            m,
            zero,
            root=tmp_path,
            snapshot=snap,
            catalog=cat,
            probe_id="check-a",
            observation_id="obs-same-read",
        )
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["reason"] == "same_read_digest_mismatch"


def test_catalog_probe_coverage_mismatch_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "TARGET-A\n")
    _write(tmp_path, "file-b.txt", "TARGET-B\n")
    snap = _capture(tmp_path)
    incomplete = catalog_payload()
    incomplete["probes"] = [incomplete["probes"][0]]
    cat = load_source_probe_catalog(incomplete)
    m = model()

    with pytest.raises(LabSourceSessionViolationError) as excinfo:
        derive_lab_binding(m, snap, cat)
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["reason"] == "catalog_probe_coverage_mismatch"


def test_catalog_path_outside_snapshot_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "TARGET-A\n")
    _write(tmp_path, "file-b.txt", "TARGET-B\n")
    snap = _capture(tmp_path)
    bad = catalog_payload()
    bad["probes"][0]["relative_paths"] = ["a-file-outside-the-snapshot.txt"]
    cat = load_source_probe_catalog(bad)
    m = model()

    with pytest.raises(LabSourceSessionViolationError) as excinfo:
        derive_lab_binding(m, snap, cat)
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["reason"] == "catalog_path_outside_snapshot"


def test_catalog_outcome_undeclared_by_the_model_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "file-a.txt", "TARGET-A\n")
    _write(tmp_path, "file-b.txt", "TARGET-B\n")
    snap = _capture(tmp_path)
    bad = catalog_payload()
    bad["probes"][0]["present_outcome_id"] = "not-a-real-outcome"
    cat = load_source_probe_catalog(bad)
    m = model()

    with pytest.raises(LabSourceSessionViolationError) as excinfo:
        derive_lab_binding(m, snap, cat)
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["reason"] == "catalog_outcome_undeclared"


def test_codex_and_claude_are_declared_lab_identities_not_live_host_parity(
    tmp_path: Path,
) -> None:
    """CODEX and CLAUDE here are catalog/snapshot-declared lab identities used to
    exercise the family axis of :class:`~latent_compass.lab.state.LabBinding`;
    neither case is evidence about a real host running either agent family."""
    _write(tmp_path, "file-a.txt", "TARGET-A\n")
    _write(tmp_path, "file-b.txt", "TARGET-B\n")
    cat = load_source_probe_catalog(catalog_payload())
    m = model()

    codex_snapshot = _capture(tmp_path, agent_family=AgentFamily.CODEX)
    claude_snapshot = _capture(tmp_path, agent_family=AgentFamily.CLAUDE)

    codex_binding = derive_lab_binding(m, codex_snapshot, cat)
    claude_binding = derive_lab_binding(m, claude_snapshot, cat)

    assert codex_binding.agent_family is AgentFamily.CODEX
    assert claude_binding.agent_family is AgentFamily.CLAUDE
    # Same model, manifest, root and catalog: the source scope itself is
    # family-agnostic even though the two bindings, as a whole, are distinct.
    assert codex_binding.source_scope_digest == claude_binding.source_scope_digest
    assert codex_binding != claude_binding
