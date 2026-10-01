"""Keep imported SQLite tables and auxiliary YAML on the same source tree."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
import sqlite3
from contextvars import ContextVar
from contextlib import contextmanager,closing

_BOUND_DATABASE=ContextVar('gkms_explicit_master_database',default=None)


@contextmanager
def bind_master_database(database:Path,*,fingerprint:str|None=None):
    """Explicit worker/thread-local binding for source-free pure predicates."""
    token=_BOUND_DATABASE.set((Path(database),fingerprint))
    try:yield
    finally:_BOUND_DATABASE.reset(token)


def resolve_bound_default_database(database:Path,*,default_database:Path)->Path:
    """Use an explicitly installed context only for the legacy default path."""
    bound=_BOUND_DATABASE.get()
    if bound is not None and Path(database).resolve()==Path(default_database).resolve():
        return bound[0]
    return Path(database)


def bound_master_fingerprint(database:Path):
    bound=_BOUND_DATABASE.get()
    return bound[1] if bound is not None and Path(database).resolve()==bound[0].resolve() else None


@lru_cache(maxsize=32)
def _import_source(database:Path,modified_ns:int,size:int)->str|None:
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as connection:
        exists=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
        if not exists:return None
        row=connection.execute("SELECT value FROM metadata WHERE key='source_dir'").fetchone()
    return row[0] if row and isinstance(row[0],str) and row[0] else None


def resolve_imported_master_directory(database:Path,master_dir:Path,*,default_master_dir:Path)->Path:
    """Honor an explicit YAML directory; resolve the default from DB provenance.

    Existing fixture DBs without importer metadata keep their explicit/default
    directory. A real imported DB records its own source_dir, so using a
    historical DB can no longer silently reopen the current default YAML.
    """
    requested=Path(master_dir)
    if requested.resolve()!=Path(default_master_dir).resolve():return requested
    stat=Path(database).stat()
    source=_import_source(Path(database),stat.st_mtime_ns,stat.st_size)
    return Path(source) if source is not None else requested
