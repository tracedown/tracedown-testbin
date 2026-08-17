"""Per-IP rate limiting with auto-ban, plus allow/deny lists.

Guards a public deployment from a single source hammering the API. The
counter is an in-memory fixed 1-second window (bans are ephemeral — no need
to survive restart), bounded to RATE_LIMIT_MAX_TRACKED IPs.

The allow/deny lists live in SQLite (runtime-mutable via the admin API,
shared across workers, restart-safe); this object holds a parsed in-memory
snapshot refreshed from the DB via set_rules().

A probing agent legitimately exceeds the threshold — whitelisting is the
only bypass, so its egress IP/CIDR must be added to the allow list (or the
limiter disabled on a trusted local network).
"""

import ipaddress
import threading
import time


def parse_nets(cidrs) -> list:
    """Parse an iterable of CIDR/IP strings, skipping malformed entries."""
    nets = []
    for item in cidrs:
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets


class RateLimiter:
    def __init__(self, *, rps: int, ban_seconds: int, max_tracked: int) -> None:
        self.rps = rps
        self.ban_seconds = ban_seconds
        self.max_tracked = max_tracked
        self.whitelist: list = []
        self.blacklist: list = []
        self._lock = threading.Lock()
        self._window: dict[str, list[float]] = {}  # ip -> [window_start_sec, count]
        self._bans: dict[str, float] = {}           # ip -> ban_expiry_epoch

    def set_rules(self, *, allow: list[str], deny: list[str]) -> None:
        """Swaps the in-memory allow/deny snapshot (from the SQLite source)."""
        wl, bl = parse_nets(allow), parse_nets(deny)
        with self._lock:
            self.whitelist, self.blacklist = wl, bl

    def _in(self, nets: list, ip: "ipaddress._BaseAddress") -> bool:
        return any(ip in net for net in nets)

    def check(self, ip_str: str) -> tuple[bool, str, int]:
        """Returns (allowed, reason, retry_after_seconds)."""
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True, "unparseable-ip", 0  # fail open on odd input

        if self._in(self.blacklist, ip):
            return False, "blacklisted", 0
        if self._in(self.whitelist, ip):
            return True, "whitelisted", 0

        now = time.time()
        second = int(now)
        with self._lock:
            ban_until = self._bans.get(ip_str)
            if ban_until is not None:
                if ban_until > now:
                    return False, "banned", int(ban_until - now) + 1
                del self._bans[ip_str]

            entry = self._window.get(ip_str)
            if entry is None or entry[0] != second:
                if len(self._window) >= self.max_tracked:
                    self._evict(second)
                self._window[ip_str] = [second, 1]
                return True, "ok", 0

            entry[1] += 1
            if entry[1] > self.rps:
                self._bans[ip_str] = now + self.ban_seconds
                del self._window[ip_str]
                return False, "banned", self.ban_seconds
            return True, "ok", 0

    def _evict(self, current_second: int) -> None:
        """Drop windows from past seconds; caller holds the lock."""
        stale = [ip for ip, e in self._window.items() if e[0] != current_second]
        for ip in stale:
            del self._window[ip]
        # Still full of same-second entries (a real flood): drop arbitrary ones.
        if len(self._window) >= self.max_tracked:
            for ip in list(self._window)[: self.max_tracked // 2]:
                del self._window[ip]

    def bans(self) -> dict[str, int]:
        """Active bans → seconds remaining (admin view)."""
        now = time.time()
        with self._lock:
            return {ip: int(exp - now) + 1 for ip, exp in self._bans.items() if exp > now}

    def unban(self, ip_str: str) -> bool:
        with self._lock:
            return self._bans.pop(ip_str, None) is not None
