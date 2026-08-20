"""HOK-188 — the offline benchmark.

Four pre-registered baselines, run over the ``VALIDATION`` split of a sealed
corpus, under one common deterministic budget, evaluated with inverse propensity
scoring against logged bandit feedback, and written into a versioned,
reproducible, independently verifiable vector report.

What this is not
----------------
It is not a model, and it does not become one. Nothing here fits, trains, tunes,
adapts or persists a policy; the four baselines are pure functions over a public
projection of a case. It emits no operational advisory and cannot influence any
agent — the benchmark is a measuring instrument pointed at a fixed corpus.

It is also not evidence of value. The corpus shipped with this repository is
small and synthetic: it demonstrates that the pipeline runs, reproduces and
refuses correctly. It supports no claim that any baseline is good, or that
Latent Compass improves anything. See ``docs/benchmark-protocol.md``.

Dependency direction
--------------------
``canonical/contracts/episode/protocol -> benchmark -> cli``. This package
imports neither ``authority`` nor ``ledger``, and neither imports it. A
benchmark report is not an authorisation and must never be accepted as one.
"""

from __future__ import annotations

from latent_compass.benchmark.baselines import (
    BASELINES,
    BaselineAlgorithm,
    BaselineChoice,
    BaselineId,
    SelectionContext,
    baseline_registry_seal,
    select,
)
from latent_compass.benchmark.budget import (
    BudgetGrant,
    BudgetLedgerEntry,
    BudgetReceipt,
    CaseBudgetMeter,
)
from latent_compass.benchmark.corpus import (
    NOMINAL_GROUP,
    BenchmarkCase,
    CaseFile,
    CorpusManifest,
    CorpusProvenance,
    DistributionKind,
    PublicCandidateView,
    PublicCaseView,
    SplitManifestEntry,
    build_manifest,
    load_case_file,
    load_corpus_manifest,
    read_split_file,
    split_corpus_seal,
    verify_manifest,
)
from latent_compass.benchmark.holdout import (
    HOLDOUT_PLAN_SCOPE,
    HoldoutPlan,
    create_holdout_plan,
    load_holdout_plan,
    recompute_holdout_plan_seal,
)
from latent_compass.benchmark.metrics import (
    DriftGroupEstimate,
    MetricValue,
    SeedEvaluation,
    evaluate_seed,
)
from latent_compass.benchmark.ope import (
    CaseEligibility,
    ChannelEstimate,
    OutcomeChannel,
    SupportDiagnostics,
    TrialRecord,
)
from latent_compass.benchmark.report import (
    BaselineReport,
    BenchmarkMeasurementSet,
    BenchmarkReport,
    BenchmarkVerdict,
    load_benchmark_report,
    recompute_report_seal,
    score_benchmark_measurements,
)
from latent_compass.benchmark.runner import run_benchmark, verify_report
from latent_compass.benchmark.spec import (
    BENCHMARK_METRIC_DIRECTIONS,
    BENCHMARK_METRIC_NAMES,
    BaselineBinding,
    BenchmarkSpec,
    load_benchmark_spec,
)

__all__ = [
    "BASELINES",
    "BENCHMARK_METRIC_DIRECTIONS",
    "BENCHMARK_METRIC_NAMES",
    "HOLDOUT_PLAN_SCOPE",
    "NOMINAL_GROUP",
    "BaselineAlgorithm",
    "BaselineBinding",
    "BaselineChoice",
    "BaselineId",
    "BaselineReport",
    "BenchmarkCase",
    "BenchmarkMeasurementSet",
    "BenchmarkReport",
    "BenchmarkSpec",
    "BenchmarkVerdict",
    "BudgetGrant",
    "BudgetLedgerEntry",
    "BudgetReceipt",
    "CaseBudgetMeter",
    "CaseEligibility",
    "CaseFile",
    "ChannelEstimate",
    "CorpusManifest",
    "CorpusProvenance",
    "DistributionKind",
    "DriftGroupEstimate",
    "HoldoutPlan",
    "MetricValue",
    "OutcomeChannel",
    "PublicCandidateView",
    "PublicCaseView",
    "SeedEvaluation",
    "SelectionContext",
    "SplitManifestEntry",
    "SupportDiagnostics",
    "TrialRecord",
    "baseline_registry_seal",
    "build_manifest",
    "create_holdout_plan",
    "evaluate_seed",
    "load_benchmark_report",
    "load_benchmark_spec",
    "load_case_file",
    "load_corpus_manifest",
    "load_holdout_plan",
    "read_split_file",
    "recompute_holdout_plan_seal",
    "recompute_report_seal",
    "run_benchmark",
    "score_benchmark_measurements",
    "select",
    "split_corpus_seal",
    "verify_manifest",
    "verify_report",
]
