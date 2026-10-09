"""Conso automation CLI — clean rebuild.

Public API lives in :mod:`conso.core` (economy / config / identity / constants)
and :mod:`conso.net` (transport / client). Browser automation and solver
subsystems are separate packages so this top-level import stays dependency-free
beyond stdlib + curl_cffi + dotenv + tiktoken.
"""

__version__ = "0.2.0"
