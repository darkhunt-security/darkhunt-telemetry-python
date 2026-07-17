"""Single source of truth for the package version.

``pyproject.toml`` reads ``__version__`` from here at build time (hatchling
dynamic version). Also emitted as the OTel Resource ``service.version`` on every
span this SDK produces.

Release model is "main = release": on every push to main, CI publishes
``MAJOR.MINOR.<run_number>`` to PyPI. Only the MAJOR.MINOR below is authoritative
— bump it to start a new release series; CI owns the patch segment. The patch
value here is just a local-dev placeholder.
"""

__version__ = "0.5.5"
