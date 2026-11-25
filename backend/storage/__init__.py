"""Storage modules whose sole job is SQL.

Why a subpackage rather than more files in `core/`: every module here is a thin,
statement-per-function layer over one database, and keeping them together makes
"which tables exist" answerable by listing one directory. Importing one of these
from `core` is normal; putting business rules here is not.

This `__init__.py` exists for a packaging reason as much as a structural one:
without it the directory is a **namespace package**, which works when running from
source and is invisible to PyInstaller's static import analysis unless a hook
happens to collect it. The symptom would be a packaged backend that starts and then
fails on the first scheduled-task request with `ModuleNotFoundError` — the kind of
defect that never appears in the test suite. `test_packaging_contracts.py` pins the
file's presence.
"""
