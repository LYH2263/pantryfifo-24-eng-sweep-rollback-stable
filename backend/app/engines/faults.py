"""Failure-injection helper, kept separate from the sweep engine.

The engine only knows "give me a callable that may raise at a named
point"; HOW injection is requested (HTTP header, env var, test code)
lives here so the entrypoint and the engine never parse fault specs.
"""
import os

POINTS = ("begin", "before_commit")
ENV_KEY = "SWEEP_FAULT"


class InjectedFault(RuntimeError):
    def __init__(self, point: str):
        self.point = point
        super().__init__(f"injected failure at {point}")


def normalize_point(point: str | None) -> str | None:
    if point is None or point == "":
        return None
    if point not in POINTS:
        raise ValueError(f"unknown fault point: {point!r} (allowed: {','.join(POINTS)})")
    return point


def make_injector(point: str | None):
    """Return an injector callable for the given point, or None."""
    point = normalize_point(point)
    if point is None:
        return None

    def inject(at: str):
        if at == point:
            raise InjectedFault(at)

    return inject


def injector_from_header(value: str | None, *, enabled: bool):
    """Build injector from an X-Fault-Inject header.

    Returns (injector, error). When injection is requested while disabled
    (ENABLE_FAULT_INJECTION!=1) an error is returned instead of silently
    ignoring it, so tests can't pass against a no-op.
    """
    point = normalize_step(value)
    if point is None:
        return None, None
    if not enabled:
        return None, "fault_injection_disabled"
    return make_injector(point), None


def normalize_step(value):
    if value is None or value == "":
        return None
    return normalize_point(value.strip())


def injector_from_env(env: dict | None = None):
    """Build injector from SWEEP_FAULT, for the CLI entrypoint."""
    env = os.environ if env is None else env
    return make_injector(normalize_point(env.get(ENV_KEY)))
