"""NKT Store Operations current package.

Business implementation is grouped under ``features/``. ``compat_aliases`` keeps
pre-consolidation dotted API/import paths working for existing callers.
"""
from . import compat_aliases as _compat_aliases

_compat_aliases.install()
