"""What cloud AI calls used, recorded where the app can read it.

A cloud provider charges per token, and the clipping engine makes the calls, so the engine records each answer's token counts
and the app (VilloMillo) reads them to count the spend against its limit. Nothing here holds a key or a prompt: only the provider,
the model and two numbers. With no sink set (the command line, tests) recording does nothing.
"""

import threading

_lock = threading.Lock()
_sink = None


def set_sink(fn) -> None:
    """`fn(provider, model, input_tokens, output_tokens)` stores one answer's usage. None turns recording off."""
    global _sink
    with _lock:
        _sink = fn


def record(provider: str, model: str, usage) -> None:
    """Note one answer's usage (a provider's `usage` object: input_tokens / output_tokens). Never raises: counting is not worth a failed clip."""
    sink = _sink
    if sink is None or not isinstance(usage, dict):
        return
    try:
        sink(provider, model, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))
    except Exception:
        pass
