"""Contract versions and the shared strict-validation entry point.

Every payload that crosses a boundary declares a contract version. This module
owns the supported-version sets and the single helper that turns a pydantic
failure into this package's own typed refusal.

Version policy
--------------
Versions are ``MAJOR.MINOR.PATCH``. A build implements an explicit set of
versions and nothing else:

* a version above the supported set is refused as ``future`` — the payload was
  written by a newer build and this one must not guess at its meaning;
* any other unsupported version is refused as ``unknown`` — retired versions are
  not silently migrated.

There is deliberately no implicit migration. Migrating data is an explicit,
auditable act, never a side effect of reading.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from latent_compass.errors import ContractViolation, UnsupportedContractVersion

__all__ = [
    "AUTHORITY_CONTRACT_VERSION",
    "BENCHMARK_CONTRACT_VERSION",
    "EPISODE_CONTRACT_VERSION",
    "JUDGEABLE_PROJECTION_CONTRACT_VERSION",
    "LEDGER_FORMAT_VERSION",
    "PROTOCOL_CONTRACT_VERSION",
    "STRICT_CONFIG",
    "SUPPORTED_AUTHORITY_VERSIONS",
    "SUPPORTED_BENCHMARK_VERSIONS",
    "SUPPORTED_EPISODE_VERSIONS",
    "SUPPORTED_JUDGEABLE_PROJECTION_VERSIONS",
    "SUPPORTED_LEDGER_FORMATS",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "FiniteFloat",
    "Identifier",
    "NonNegativeFloat",
    "Seal",
    "StrictModel",
    "Timestamp",
    "UnitInterval",
    "check_contract_version",
    "parse_version",
    "require_unit_interval",
    "validate_contract",
]

EPISODE_CONTRACT_VERSION: Final = "1.0.0"
JUDGEABLE_PROJECTION_CONTRACT_VERSION: Final = "1.0.0"
PROTOCOL_CONTRACT_VERSION: Final = "1.0.0"
AUTHORITY_CONTRACT_VERSION: Final = "2.0.0"
LEDGER_FORMAT_VERSION: Final = "1.0.0"

#: The HOK-188 offline benchmark contracts — corpus, spec and report. Versioned
#: on its own axis: the benchmark is a *consumer* of the HOK-181 protocol
#: contract, so it must be able to move without dragging the pre-registration
#: contract with it, and vice versa.
BENCHMARK_CONTRACT_VERSION: Final = "1.0.0"

SUPPORTED_EPISODE_VERSIONS: Final = frozenset({EPISODE_CONTRACT_VERSION})
SUPPORTED_JUDGEABLE_PROJECTION_VERSIONS: Final = frozenset({JUDGEABLE_PROJECTION_CONTRACT_VERSION})
SUPPORTED_PROTOCOL_VERSIONS: Final = frozenset({PROTOCOL_CONTRACT_VERSION})
SUPPORTED_AUTHORITY_VERSIONS: Final = frozenset({"1.0.0", AUTHORITY_CONTRACT_VERSION})
SUPPORTED_LEDGER_FORMATS: Final = frozenset({LEDGER_FORMAT_VERSION})
SUPPORTED_BENCHMARK_VERSIONS: Final = frozenset({BENCHMARK_CONTRACT_VERSION})

#: Shared configuration for every contract model.
#:
#: ``extra="forbid"`` is what rejects an injected ``action`` or ``authority``
#: field. ``strict=True`` is what rejects coercions such as ``"0.5"`` or ``1``
#: where a float is required. ``frozen=True`` makes a validated contract
#: immutable, so nothing can be mutated after the seal is computed.
STRICT_CONFIG: Final = ConfigDict(
    extra="forbid",
    strict=True,
    frozen=True,
    validate_default=True,
    revalidate_instances="always",
)


class StrictModel(BaseModel):
    """Base class for every versioned contract model."""

    model_config = STRICT_CONFIG

    def canonical_payload(self) -> dict[str, object]:
        """Return a plain JSON-shaped mapping suitable for sealing."""
        return self.model_dump(mode="json")


def _as_json_text(payload: object) -> str:
    """Serialise a payload for JSON-mode validation.

    ``StrEnum`` members serialise as their value because they are ``str``
    subclasses, so internally built payloads and payloads read from disk take
    the same path.
    """
    try:
        return json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(
            "payload is not JSON-serialisable",
            detail={"cause": str(exc)},
        ) from exc


_IDENTIFIER_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$"

#: One encoding, and only one: ISO-8601, UTC, second precision, trailing ``Z``.
#:
#: A sealed timestamp must have a single byte representation. Fractional
#: seconds, a ``+00:00`` offset and a bare local time all denote instants this
#: schema can express, and each would seal differently, so each is refused
#: rather than normalised. Normalising would mean the bytes that were sealed are
#: not the bytes that were supplied.
_TIMESTAMP_PATTERN: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _require_canonical_timestamp(value: str) -> str:
    if _TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "timestamp must be exactly YYYY-MM-DDTHH:MM:SSZ — "
            "no fractional seconds, no numeric offset, no local time"
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"timestamp is not a real UTC instant: {value!r}") from exc
    return value


Identifier = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
Timestamp = Annotated[
    str, Field(min_length=20, max_length=20), AfterValidator(_require_canonical_timestamp)
]
Seal = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


def require_unit_interval(value: float, *, name: str) -> float:
    """Refuse anything that is not a real number in ``[0, 1]``.

    Used where a bare float crosses the API without a pydantic model to guard
    it. ``NaN`` fails every comparison, so a plain ``value <= threshold`` test
    silently answers "no" for ``NaN`` and "yes" for ``-5.0``; neither is an
    answer this package is entitled to give.
    """
    # The annotation says float; callers are not obliged to honour it, and a
    # bool is an int in Python, so both are checked at runtime.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(
            f"{name} must be a real number", detail={"name": name, "received": repr(value)}
        )
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise ContractViolation(
            f"{name} must be finite", detail={"name": name, "received": repr(value)}
        )
    if not (0.0 <= numeric <= 1.0):
        raise ContractViolation(
            f"{name} must lie in [0, 1]", detail={"name": name, "received": numeric}
        )
    return numeric


def parse_version(version: str) -> tuple[int, int, int]:
    """Parse ``MAJOR.MINOR.PATCH`` into a comparable tuple."""
    parts = version.split(".")
    if len(parts) != 3:
        raise UnsupportedContractVersion(
            f"malformed contract version: {version!r}",
            detail={"version": version, "reason": "malformed"},
        )
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError as exc:
        raise UnsupportedContractVersion(
            f"malformed contract version: {version!r}",
            detail={"version": version, "reason": "malformed"},
        ) from exc
    if major < 0 or minor < 0 or patch < 0:
        raise UnsupportedContractVersion(
            f"malformed contract version: {version!r}",
            detail={"version": version, "reason": "malformed"},
        )
    return major, minor, patch


def check_contract_version(declared: str, supported: frozenset[str], contract: str) -> str:
    """Refuse any version this build does not implement.

    Returns ``declared`` unchanged when it is supported, so call sites can use
    the result directly.
    """
    if declared in supported:
        return declared
    declared_tuple = parse_version(declared)
    highest = max(parse_version(candidate) for candidate in supported)
    reason = "future" if declared_tuple > highest else "unknown"
    raise UnsupportedContractVersion(
        f"{contract} version {declared!r} is not supported by this build",
        detail={
            "contract": contract,
            "version": declared,
            "reason": reason,
            "supported": sorted(supported),
        },
    )


def validate_contract[ModelT: BaseModel](
    model: type[ModelT],
    payload: object,
    *,
    error: type[ContractViolation],
    context: str,
) -> ModelT:
    """Validate ``payload`` against ``model``, refusing with a typed error.

    Validation runs in pydantic's **JSON strict mode**, which is the mode that
    matches the input domain: every payload either came from a file or is about
    to be sealed as JSON. Strict JSON mode still refuses coercions — ``"0.7"``
    for a float, ``"3"`` for an int, ``1``/``0`` for a bool — and still refuses
    unknown fields; it differs from strict *Python* mode only in accepting the
    representations JSON actually has, such as a string for an enum member and
    an array for a tuple.

    pydantic's ``ValidationError`` never escapes the package boundary: callers
    see a :class:`~latent_compass.errors.ContractViolation` subclass carrying a
    structured, JSON-safe ``detail``.
    """
    try:
        return model.model_validate_json(_as_json_text(payload))
    except ValidationError as exc:
        raise error(
            f"{context} failed strict contract validation",
            detail={
                "context": context,
                "violations": [
                    {
                        "location": ".".join(str(part) for part in item["loc"]),
                        "type": item["type"],
                        "message": item["msg"],
                    }
                    for item in exc.errors(include_url=False)
                ],
            },
        ) from exc
