"""HOK-225 — structural proof for the current pairwise-data refusal.

This test inspects Python contracts only. It opens no corpus split.
"""

from latent_compass.benchmark.corpus import PublicCandidateView
from latent_compass.episode import Candidate, Economics, Episode


def test_candidate_projection_has_no_judgeable_action_semantics() -> None:
    expected_candidate_fields = {"direction_id", "propensity", "prior_uncertainty"}

    assert set(Candidate.model_fields) == expected_candidate_fields
    assert set(PublicCandidateView.model_fields) == expected_candidate_fields

    forbidden_candidate_evidence = {
        "description",
        "parameters",
        "constraints",
        "success",
        "violations",
        "cost",
        "information_gain",
        "reversibility",
    }
    assert forbidden_candidate_evidence.isdisjoint(Candidate.model_fields)


def test_economics_are_episode_level_not_candidate_specific() -> None:
    assert "economics" in Episode.model_fields
    assert set(Economics.model_fields) == {
        "cost",
        "information_gain",
        "reversibility",
        "uncertainty",
    }
    assert "economics" not in Candidate.model_fields
