"""
Moderation hooks.

Per the spec, the real moderation strategy is:
  - Governance-curated model allowlist (exclude purpose-built-for-harm models) -> enforced
    at the *network* layer in Phase 2, not here.
  - CSAM image-output hash-matching -> enforced *here* on the host, on every image output,
    as a condition of the host's bond. Non-negotiable.
  - Host discretion + ToS.

This module is the per-host enforcement point. Text generation has no hashing equivalent;
image outputs must be hashed against known-CSAM databases (PhotoDNA / NCMEC-style) before
they ever leave the host.
"""
from __future__ import annotations


class ModerationError(Exception):
    """Raised to block an output. The host must refuse to return the content."""


def check_image_output(image_bytes: bytes) -> None:
    """
    Phase 1: perceptual-hash the image and match against a known-CSAM hash set.
    Raise ModerationError on a match; the host drops the output and may flag the requester.
    """
    # TODO Phase 1: integrate a perceptual-hash matcher + industry hash list.
    # Intentionally a hard seam: image models MUST NOT ship until this is real.
    return None


def model_allowed(model_name: str, allowlist: set[str] | None) -> bool:
    """Network-governance allowlist check (allowlist distributed in Phase 2)."""
    if allowlist is None:
        return True  # Phase 0: no governance layer yet
    return model_name in allowlist
