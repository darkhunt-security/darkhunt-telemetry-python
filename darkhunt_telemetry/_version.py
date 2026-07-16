"""Single source of truth for the package version.

Kept in sync with ``pyproject.toml``. Emitted as the OTel Resource
``service.version`` on every span this SDK produces.
"""

__version__ = "0.5.5"
