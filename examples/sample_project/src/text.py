def slugify(s: str) -> str:
    return "-".join(s.lower().split())


def word_count(s: str) -> int:
    """Number of whitespace-separated words."""
    return len(s.split())


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
