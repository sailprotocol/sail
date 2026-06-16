"""
Config loading.

Every process loads its own .env file, chosen by the ENV_FILE env var (default ".env"):
the host uses ENV_FILE=.env.host, the client uses ENV_FILE=.env.client. Call load_env()
once at the top of each entrypoint, before any module-level os.getenv runs.

load_dotenv does NOT override variables already set in the real environment, so an explicit
`PAYMENTS=mock python smoke_test.py` still wins, and it is a no-op if the file is missing.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv


def load_env() -> None:
    load_dotenv(os.getenv("ENV_FILE", ".env"))
