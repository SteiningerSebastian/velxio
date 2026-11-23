"""Install a board's declared libraries on THIS server right before it compiles.

Why: on a multi-replica self-host the Arduino user-libraries directory is
local to a node (or a pod). The Library Manager's install request and the
later compile request can be answered by different replicas, and the compile
then fails with "<Header.h>: No such file or directory" although the library
shows as installed in the UI. The board's manifest (the `libraries` list the
client sends with every compile) says what the sketch needs, so the compiling
replica installs whatever of it is missing before the build starts.

Only active without a pro overlay (no warm/materialize hooks): with an overlay
the libraries come from its content-addressed cache, not the global dir.
Disable with VELXIO_AUTO_INSTALL_LIBRARIES=0.

Replicas on one node may share the libraries dir (hostPath), so installs are
serialised by an flock on a file inside that dir as well as an in-process
asyncio lock; the installed set is re-read after the lock is taken so a
library another process just installed is not installed twice.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()


def _norm(name: str) -> str:
    """Same normalisation as EspIdfCompiler._norm_lib_name: base name before
    '@', lowercased, alphanumerics only."""
    return ''.join(ch for ch in (name or '').split('@', 1)[0].lower() if ch.isalnum())


def _enabled() -> bool:
    if os.environ.get('VELXIO_AUTO_INSTALL_LIBRARIES', '1').strip().lower() in ('0', 'false', 'no', 'off'):
        return False
    from app.core import hooks
    return (
        getattr(hooks, '_warm_library_hook', None) is None
        and getattr(hooks, '_materialize_library_scope_hook', None) is None
    )


def user_libraries_dir() -> Path:
    """arduino-cli's user libraries dir (directories.user/libraries)."""
    base = os.environ.get('ARDUINO_DIRECTORIES_USER')
    return Path(base) / 'libraries' if base else Path.home() / 'Arduino' / 'libraries'


def installed_names(libs_dir: Path) -> set[str]:
    """Normalised names of every library in `libs_dir`: folder name AND the
    library.properties `name=` (they differ by separators, sometimes more)."""
    out: set[str] = set()
    try:
        entries = list(libs_dir.iterdir())
    except OSError:
        return out
    for d in entries:
        if not d.is_dir() or d.name.startswith('.'):
            continue
        out.add(_norm(d.name))
        props = d / 'library.properties'
        try:
            with props.open(encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if line.startswith('name='):
                        out.add(_norm(line[5:].strip()))
                        break
        except OSError:
            pass
    return out


def missing_specs(specs: list[str] | None, libs_dir: Path) -> list[str]:
    if not specs:
        return []
    have = installed_names(libs_dir)
    seen: set[str] = set()
    out: list[str] = []
    for spec in specs:
        spec = (spec or '').strip()
        key = _norm(spec)
        if not key or key in seen:
            continue
        seen.add(key)
        if key not in have:
            out.append(spec)
    return out


def _flock(path: Path):
    """Blocking exclusive flock; returns the fd (or None where unsupported)."""
    try:
        import fcntl
    except ImportError:  # Windows dev box
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _funlock(fd) -> None:
    if fd is None:
        return
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


async def ensure_manifest_libraries(
    specs: list[str] | None,
    arduino_cli: Any,
    progress_callback: Callable[[str], None] | None = None,
) -> list[str]:
    """Install the manifest entries this server lacks. Returns the log lines
    (empty when nothing was missing). Never raises: a failed install is left
    for the compile to report as the missing header it causes."""
    if not specs or not _enabled():
        return []
    libs_dir = user_libraries_dir()
    if not missing_specs(specs, libs_dir):
        return []  # fast path, no locking

    log: list[str] = []

    def note(text: str) -> None:
        log.append(text)
        logger.info('[libs] %s', text)
        if progress_callback is not None:
            try:
                progress_callback(f'[velxio] {text}\n')
            except Exception:  # noqa: BLE001 - progress is best effort
                pass

    async with _lock:
        fd = await asyncio.to_thread(_flock, libs_dir / '.velxio-install.lock')
        try:
            todo = missing_specs(specs, libs_dir)
            for spec in todo:
                # A dependency installed with an earlier entry may cover this one.
                if _norm(spec) in installed_names(libs_dir):
                    continue
                note(f'Installing library "{spec}" on this server...')
                try:
                    res = await arduino_cli.install_library(spec)
                except Exception as exc:  # noqa: BLE001
                    res = {'success': False, 'error': str(exc)}
                if res.get('success'):
                    note(f'Library "{spec}" installed.')
                else:
                    note(f'Could not install library "{spec}": {res.get("error") or "unknown error"}')
        finally:
            await asyncio.to_thread(_funlock, fd)
    return log
