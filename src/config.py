"""Environment-tunable settings.

The API is designed to be publicly hostable (multi-tenant): every expensive
knob has a hard cap, keyed state expires, and destructive/global operations
can be gated behind an admin token.
"""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


HTTP_PORT = _int("PORT", 20780)
TLS_PORT = _int("TLS_PORT", 20781)
CHAOS_PORT = _int("CHAOS_PORT", 20782)

TLS_ENABLED = os.environ.get("TLS_ENABLED", "true").lower() != "false"
CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "true").lower() != "false"

# Optional bearer token for global state listing/reset. Unset = open
# (local/e2e); set it on public deployments.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# Abuse caps
MAX_DELAY_MS = _int("MAX_DELAY_MS", 120_000)
MAX_BYTES = _int("MAX_BYTES", 25 * 1024 * 1024)
MAX_REDIRECT_HOPS = _int("MAX_REDIRECT_HOPS", 20)
MAX_DRIP_CHUNKS = _int("MAX_DRIP_CHUNKS", 100)
HANG_SECONDS = _int("HANG_SECONDS", 300)

# Keyed-state retention
STATE_TTL_SECONDS = _int("STATE_TTL_SECONDS", 3600)
STATE_MAX_KEYS = _int("STATE_MAX_KEYS", 10_000)

# Per-IP rate limiting / auto-ban. An IP exceeding RATE_LIMIT_RPS in any
# 1-second window is banned for RATE_LIMIT_BAN_SECONDS (429 with Retry-After).
# WARNING: a probing agent exceeds this. Whitelisting is the only bypass —
# add the agents' egress IP/CIDR to the allow list, or disable the limiter on
# a trusted local network (RATE_LIMIT_ENABLED=false).
#
# The allow/deny lists live in SQLite (runtime-mutable via the admin API).
# RATE_LIMIT_WHITELIST / RATE_LIMIT_BLACKLIST are SEED values inserted on
# startup if absent — a convenience so a deploy can ship with an allow list.
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() != "false"
RATE_LIMIT_RPS = _int("RATE_LIMIT_RPS", 100)
RATE_LIMIT_BAN_SECONDS = _int("RATE_LIMIT_BAN_SECONDS", 3600)
RATE_LIMIT_WHITELIST = os.environ.get("RATE_LIMIT_WHITELIST", "")  # CSV seed of allow IPs/CIDRs
RATE_LIMIT_BLACKLIST = os.environ.get("RATE_LIMIT_BLACKLIST", "")  # CSV seed of deny IPs/CIDRs
RATE_LIMIT_MAX_TRACKED = _int("RATE_LIMIT_MAX_TRACKED", 50_000)
# How often each worker reloads the allow/deny snapshot from SQLite (so admin
# changes on one worker propagate to the others).
RATE_LIMIT_RELOAD_SECONDS = _int("RATE_LIMIT_RELOAD_SECONDS", 10)
# Read the client IP from X-Forwarded-For (leftmost) — required behind a PaaS
# proxy. Spoofable; acceptable for a throwaway test API.
TRUST_FORWARDED_FOR = os.environ.get("TRUST_FORWARDED_FOR", "true").lower() != "false"
