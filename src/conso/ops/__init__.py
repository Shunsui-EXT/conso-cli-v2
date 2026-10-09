"""
Operations layer: concurrency, limits, verifiers, earnings and the event bus.

Deliberately dependency-light — this package's __init__ imports NOTHING so that
`import conso.ops.events` stays cheap and import-cycle-free (ops must never
pull in app/monitor; see `conso.ops.events` for the one-way dependency rule).
Import submodules explicitly, e.g. `from conso.ops import limits`.
"""
