"""Shared pytest setup.

The host daemon starts a second (operator) uvicorn listener on a real localhost port from its
startup event. Under tests we drive the apps with TestClient, which fires those startup events —
so disable the operator autostart here to avoid binding a real port during the suite. Set before
any test imports host.daemon.
"""
import os

os.environ.setdefault("SAIL_OPERATOR_AUTOSTART", "0")
