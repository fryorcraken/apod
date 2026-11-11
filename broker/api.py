"""The broker's API for extensions. Import from here (`from api import check, provider, ...`):
these names are the contract, documented in EXTENSIONS.md, and API_VERSION changes when one of
them changes in a way that breaks an extension. Anything else in core is the broker's own.

An extension can check what it gets:

    import api
    if api.API_VERSION != 1:
        raise RuntimeError("written for the broker's extension API 1")
"""
from core import (  # noqa: F401 (re-exported)
    AGENT, ASK_TIMEOUT, BDIR, BODY_LIMIT, GIT_BODY_LIMIT, HARD_DENY, LOCK, POD_DIR, ROOT, SHIM,
    BrokerError, Provider, Request,
    allowed, check, evaluate, expand, live_session, need_key, now, provider, pubkey_of, read_body,
    recently_allowed, rejected_msg, remote_target, run_quiet, scope_kind, send_text, unix_listen,
    waiting_msg,
)

API_VERSION = 1

__all__ = [
    "API_VERSION",
    # registering
    "Provider", "provider", "scope_kind", "remote_target", "HARD_DENY",
    # requests
    "Request", "check", "evaluate", "allowed", "recently_allowed", "rejected_msg", "waiting_msg",
    # the pod's HTTP requests
    "read_body", "send_text", "BODY_LIMIT", "GIT_BODY_LIMIT",
    # keys, sockets, files
    "need_key", "pubkey_of", "unix_listen", "live_session", "AGENT", "BDIR", "POD_DIR", "ROOT", "SHIM",
    # sessions
    "LOCK",
    # utilities
    "BrokerError", "ASK_TIMEOUT", "expand", "now", "run_quiet",
]
