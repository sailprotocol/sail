"""
Moderation seam (per-host enforcement point).

Two non-negotiable mechanisms, built as architecture now (before image models or outside hosts):

  1. Model allowlist — the host refuses to serve any model not on a configured allowlist, and
     clients filter discovered listings the same way. WHO curates the canonical list (governance,
     distribution, signing) is deferred to Phase 4; this is the working mechanism. When no
     allowlist is configured the check is permissive (no governance layer yet).

  2. CSAM image-output gate — FAIL-CLOSED. The host refuses to serve any image-modality model
     unless a real CSAM hash matcher is configured, and every image output MUST pass through
     check_image_output() before it leaves the host. Text generation has no hashing equivalent.

A real hash-DB integration (PhotoDNA / NCMEC-style) requires organizational enrollment and is a
HARD PREREQUISITE for enabling image models. Until then `CSAM_HASHER` is unset, image modality is
disabled, and check_image_output() raises. Never weaken or bypass these seams to "simplify."
"""
from __future__ import annotations

import os
import pathlib


class ModerationError(Exception):
    """Raised to block an output or refuse to serve. The host must not return the content."""


# --- model allowlist --------------------------------------------------------
def load_allowlist() -> set[str] | None:
    """Union of MODEL_ALLOWLIST (comma-separated) and MODEL_ALLOWLIST_PATH (file, one per line,
    '#' comments). Returns None when neither is configured -> permissive (no governance yet)."""
    names: set[str] = set()
    configured = False
    inline = os.getenv("MODEL_ALLOWLIST")
    if inline:
        configured = True
        names.update(n.strip() for n in inline.split(",") if n.strip())
    path = os.getenv("MODEL_ALLOWLIST_PATH")
    if path:
        configured = True
        p = pathlib.Path(path)
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    names.add(line)
    return names if configured else None


def model_allowed(model_name: str, allowlist: set[str] | None) -> bool:
    """Pure check: allowlist None -> allow (no governance configured); else membership."""
    if allowlist is None:
        return True
    return model_name in allowlist


def is_model_allowed(model_name: str) -> bool:
    """Allowlist check against the process's configured allowlist (host serve / client filter)."""
    return model_allowed(model_name, load_allowlist())


# --- CSAM image-output gate (fail-closed) -----------------------------------
def csam_matcher_configured() -> bool:
    """True only if a CSAM hash matcher is configured via CSAM_HASHER. No real matcher ships in
    this repo, so this is False in practice and image modality stays disabled."""
    return bool(os.getenv("CSAM_HASHER", "").strip())


def check_image_output(image_bytes: bytes) -> None:
    """Mandatory chokepoint for EVERY image output before it leaves the host. FAIL-CLOSED:
    with no matcher configured this raises, so image bytes can never ship unchecked. With a
    matcher configured it perceptual-hashes the image and raises ModerationError on a known-CSAM
    match (real PhotoDNA/NCMEC-style integration is a hard prerequisite, not implemented here)."""
    if not csam_matcher_configured():
        raise ModerationError(
            "image output blocked: no CSAM hash matcher configured (CSAM_HASHER unset). "
            "Image models require a real hash-DB integration before they can serve."
        )
    # TODO: run the configured perceptual-hash matcher against known-CSAM hash lists here.
    raise ModerationError("CSAM matcher backend not implemented; refusing to release image output.")


def assert_can_serve(model) -> None:
    """Startup gate: refuse to serve a disallowed model, or any image-modality model without a
    configured CSAM matcher. Raises ModerationError; the host should fail loudly rather than serve."""
    if not is_model_allowed(model.name):
        raise ModerationError(f"model not on the configured allowlist: {model.name}")
    if getattr(model, "modality", "text") == "image" and not csam_matcher_configured():
        raise ModerationError(
            f"refusing to serve image model {model.name}: no CSAM hash matcher configured. "
            "Image models do not ship until the CSAM check is real."
        )
