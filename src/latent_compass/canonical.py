"""Canonical serialisation and domain-separated seals.

A *seal* is a reproducible digest over a canonical byte encoding of a payload.
Two payloads that are logically identical must produce byte-identical canonical
encodings, and therefore identical seals, on any host running this version.

Canonicalisation rules
----------------------
* Object keys are sorted with the default (code point) ordering.
* No insignificant whitespace.
* UTF-8, not escaped to ASCII, so the same text has one encoding.
* Mapping keys must already be ``str``. Integer or ``None`` keys are refused
  rather than silently stringified, because ``{1: "a"}`` and ``{"1": "a"}``
  would otherwise collide.
* ``NaN``, ``Infinity`` and ``-Infinity`` are refused. They are the canonical
  smuggling vector for "an unknown that looks like a number", and this package
  fails closed on unknowns.
* Sets, bytes, dates and arbitrary objects are refused. Callers dump pydantic
  models with ``mode="json"`` first, so the input here is always plain JSON
  data.

Seals are domain-separated: the digest covers a package-wide separator plus a
caller-supplied domain tag plus the canonical bytes. An episode payload and a
chain link with coincidentally equal encodings therefore cannot produce the
same seal.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Final

__all__ = [
    "DOMAIN_SEPARATOR",
    "SEAL_PREFIX",
    "canonical_bytes",
    "canonical_text",
    "seal",
]

DOMAIN_SEPARATOR: Final = "latent-compass/seal/v1"
SEAL_PREFIX: Final = "sha256:"

_FIELD_SEPARATOR: Final = b"\x1f"


def _normalise(value: object, *, path: str = "$") -> object:
    """Recursively validate and normalise a JSON-shaped payload.

    Raises ``TypeError`` or ``ValueError`` for anything that cannot be encoded
    reproducibly. The ``path`` argument makes the refusal locatable.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        # Checked before int: bool is a subclass of int.
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float at {path}: {value!r}")
        return value
    if isinstance(value, (list, tuple)):
        return [_normalise(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        normalised: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string mapping key at {path}: {key!r}")
            normalised[key] = _normalise(item, path=f"{path}.{key}")
        return normalised
    raise TypeError(f"unserialisable value at {path}: {type(value).__name__}")


def canonical_text(payload: object) -> str:
    """Return the canonical JSON text for ``payload``."""
    return json.dumps(
        _normalise(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(payload: object) -> bytes:
    """Return the canonical UTF-8 encoding for ``payload``."""
    return canonical_text(payload).encode("utf-8")


def seal(domain: str, payload: object) -> str:
    """Return a domain-separated SHA-256 seal over ``payload``.

    ``domain`` names what is being sealed (for example ``"episode.content"``).
    Distinct domains cannot collide even on identical payload bytes.
    """
    if not domain:
        raise ValueError("seal domain must be a non-empty string")
    digest = hashlib.sha256()
    digest.update(DOMAIN_SEPARATOR.encode("utf-8"))
    digest.update(_FIELD_SEPARATOR)
    digest.update(domain.encode("utf-8"))
    digest.update(_FIELD_SEPARATOR)
    digest.update(canonical_bytes(payload))
    return SEAL_PREFIX + digest.hexdigest()
