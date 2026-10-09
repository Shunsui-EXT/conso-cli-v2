"""
Application layer for the Conso CLI: terminal UI, live monitor and the Typer
command surface.

This package is the only place that talks to the user. It sits on top of
``conso.ops`` (work), ``conso.net`` (HTTP), ``conso.core`` (config/economy) and
``conso.solver`` (captcha) and never the other way round: ops reports progress
through the ``conso.ops.events`` bus, which the monitor subscribes to.

Import submodules explicitly (``from conso.app import ui``) — this __init__
stays empty on purpose so importing the package never drags in Typer/Rich.
"""
