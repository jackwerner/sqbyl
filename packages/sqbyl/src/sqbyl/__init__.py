"""sqbyl — the full text-to-SQL dev toolkit.

Build, evaluate, and iterate on a Claude-powered text-to-SQL agent over your own
database. Depends on ``sqbyl_runtime`` (never the reverse).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sqbyl")
except PackageNotFoundError:  # running from a source tree with no installed metadata
    __version__ = "0.0.0"
