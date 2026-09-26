"""One qualification scope: hash immutable sources once, watch actual changes.

No process-global trust or persistent cache is created. Only SHA/stat evidence
and at most 8 MiB of small document bytes survive within the owning call.
Mutable state, progress, checkpoints and training reports are never retained.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path

_ACTIVE = ContextVar('gkms_qualification_verification_scope', default=None)


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes
    from ctypes import wintypes
    class BasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in
            ('CreationTime', 'LastAccessTime', 'LastWriteTime', 'ChangeTime')] + [
            ('FileAttributes', wintypes.DWORD)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    info.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    return ctypes, wintypes, BasicInfo, create, info, close


def source_stamp(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Qualification source must be an existing unlinked file: ' + str(path))
    value = path.stat()
    stamp = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if os.name != 'nt':
        return stamp
    ctypes, wintypes, BasicInfo, create, info, close = _windows_api()
    handle = create(str(path.resolve()), 0x80, 7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), 'Cannot inspect qualification source ChangeTime')
    try:
        basic = BasicInfo()
        if not info(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise OSError(ctypes.get_last_error(), 'Cannot inspect qualification source ChangeTime')
    finally:
        close(handle)
    after = path.stat()
    if stamp != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('Qualification source changed during identity inspection')
    return (*stamp, basic.ChangeTime)


class VerificationScope:
    def __init__(self, *, cache_bytes=8*1024**2, document_bytes=256*1024):
        self.proofs = {}
        self.watches = {}
        self.small = OrderedDict()
        self.small_bytes = 0
        self.cache_bytes, self.document_bytes = cache_bytes, document_bytes
        self.qualified = {}
        self.excluded = set()
        self.hash_reads = self.hash_bytes = self.hits = 0

    def eligible(self, path):
        path = Path(path).resolve()
        name = path.name.lower()
        if path in self.excluded or name in ('state.json','progress.json','launch.json','owner.json','session.json'):
            return False
        if 'progress' in name or 'checkpoint' in name or path.suffix.lower() in ('.pt','.npz','.log'):
            return False
        if name.endswith('report.json') and any(part.lower().startswith(
                ('training','shared_training','offline_training')) for part in path.parts):
            return False
        return True

    def _remember(self, path, raw):
        if len(raw) > self.document_bytes or len(raw) > self.cache_bytes:
            return
        previous = self.small.pop(path, None)
        if previous is not None:self.small_bytes -= len(previous)
        while self.small and self.small_bytes + len(raw) > self.cache_bytes:
            _, removed = self.small.popitem(last=False)
            self.small_bytes -= len(removed)
        self.small[path] = raw
        self.small_bytes += len(raw)

    def _observe(self,path,stamp):
        if path in self.watches and self.watches[path] != stamp:
            raise ValueError('Qualification source changed after verification: ' + str(path))
        self.watches[path]=stamp

    def _check_known(self, path, stamp):
        known = self.proofs.get(path)
        if known is not None and known[1] != stamp:
            raise ValueError('Qualification source changed after verification: ' + str(path))
        return known

    def reference(self, path):
        path = Path(path)
        before = source_stamp(path)
        path = path.resolve()
        eligible = self.eligible(path)
        known = self._check_known(path, before) if eligible else None
        if known is not None:
            self.hits += 1
            return dict(known[0])
        checksum = hashlib.sha256()
        chunks = [] if eligible and before[2] <= self.document_bytes else None
        with path.open('rb') as stream:
            while block := stream.read(1024*1024):
                checksum.update(block)
                if chunks is not None:chunks.append(block)
        if source_stamp(path) != before:
            raise ValueError('Qualification source changed during verification: ' + str(path))
        ref = {'path':str(path),'sha256':checksum.hexdigest(),'bytes':before[2]}
        self.hash_reads += 1;self.hash_bytes += before[2]
        self._observe(path,before)
        if eligible:
            self.proofs[path] = (ref,before)
            if chunks is not None:self._remember(path,b''.join(chunks))
        return dict(ref)

    def pin(self, expected):
        actual = self.reference(expected['path'])
        if actual['sha256'] != expected['sha256'] or ('bytes' in expected and actual['bytes'] != expected['bytes']):
            raise ValueError('Qualification source hash/bytes differs: ' + actual['path'])
        return actual

    def read(self, reference_or_path, *, limit=256*1024**2):
        expected = reference_or_path if isinstance(reference_or_path,dict) else None
        path = Path(expected['path'] if expected is not None else reference_or_path)
        before = source_stamp(path)
        if before[2] > limit:raise ValueError('Qualification document exceeds bounded read limit')
        path = path.resolve();eligible = self.eligible(path)
        known = self._check_known(path,before) if eligible else None
        raw = self.small.get(path) if known is not None else None
        if raw is None:
            raw = path.read_bytes()
            if source_stamp(path) != before or len(raw) != before[2]:
                raise ValueError('Qualification document changed during read: ' + str(path))
            if known is None:
                actual = {'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
                self.hash_reads += 1;self.hash_bytes += len(raw)
            else:
                # Large metadata is decoded on demand, never retained wholesale.
                # Its exact file identity/ChangeTime stayed pinned across the read.
                actual = known[0]
                self.hits += 1
            if eligible:
                self.proofs[path]=(actual,before)
                self._remember(path,raw)
        else:
            actual=known[0];self.hits += 1
            self.small.move_to_end(path)
        if expected is not None and any(actual[k] != expected[k] for k in ('sha256','bytes') if k in expected):
            raise ValueError('Qualification source hash/bytes differs: ' + str(path))
        value=json.loads(raw.decode('utf-8-sig'))
        if type(value) is not dict:raise ValueError('Qualification document must be an object')
        if (value.get('status') in ('running','pending','launching','stopping')
                or 'training' in str(value.get('schema',''))):
            self.excluded.add(path);self.proofs.pop(path,None)
            removed=self.small.pop(path,None)
            if removed is not None:self.small_bytes-=len(removed)
        if source_stamp(path) != before:
            raise ValueError('Qualification document changed during decode: ' + str(path))
        self._observe(path,before)
        return value

    def guard(self):
        for path,stamp in self.watches.items():
            if source_stamp(path) != stamp:
                raise ValueError('Qualification source changed after verification: ' + str(path))


def active_scope():
    return _ACTIVE.get()


@contextmanager
def verification_scope(*, enabled=True, guard_on_exit=True):
    current = _ACTIVE.get()
    if enabled and current is not None:
        yield current
        return
    scope = VerificationScope() if enabled else None
    token = _ACTIVE.set(scope)
    try:
        yield scope
        if scope is not None and guard_on_exit:scope.guard()
    finally:
        _ACTIVE.reset(token)
