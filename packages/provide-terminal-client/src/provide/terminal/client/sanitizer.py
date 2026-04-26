import string

def sanitize_keystrokes(keys: str, max_bytes: int = 4096) -> str:
    """Sanitize keystrokes for AI agents: strip non-printable chars except common controls."""
    # Allowed: printable + CR, LF, TAB, Ctrl+C (\x03), ESC (\x1b)
    allowed = set(string.printable) | {"\r", "\n", "\t", "\x03", "\x1b"}
    
    # Filter printable
    filtered = "".join(c for c in keys if c in allowed)
    
    # Enforce byte limit
    encoded = filtered.encode("utf-8")
    if len(encoded) <= max_bytes:
        return filtered
    
    # Truncate at character boundary
    truncated = encoded[:max_bytes].decode("utf-8", "ignore")
    return truncated

