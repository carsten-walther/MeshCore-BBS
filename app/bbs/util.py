"""Small shared helpers used by both the BBS and the admin CLI."""


def fmt_ago(secs: int) -> str:
    """Return a compact relative-time string: '5m', '2h', '3d'.

    Floors at '1m' so a freshly seen entry never reads as '0m'."""
    if secs < 3600:
        return f"{max(1, secs // 60)}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"
