"""Compatibility entry point for the installed passive shadow hook."""

from __future__ import annotations

from _latent_compass_shadow_hook_impl import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
