"""Race-safe file mutation beneath an explicitly opened root.

The containment decision and every filesystem mutation share directory handles.
Path components are never followed through symlinks or Windows reparse points,
New-file publication is exclusive and replacement is atomic.

The boundary rejects links/reparse points and parent substitutions encountered
while opening the path. On POSIX, an already-open directory descriptor remains
valid if a local peer relocates that directory afterward; mutations through
that descriptor then follow the relocated directory. Preventing that kernel
semantic requires OS-specific namespace isolation and is outside this module's
portable threat model. Do not describe these primitives as defending against a
peer that can concurrently rename already-open profile directories.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
import sys
from contextlib import suppress
from pathlib import Path
from typing import Final

from latent_compass.errors import ContractViolation

_TEMP_PREFIX: Final = ".lc-"
_MAX_ENUMERATION_DEPTH: Final = 32
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    | {f"COM{index}" for index in "¹²³"}
    | {f"LPT{index}" for index in "¹²³"}
)


def _is_windows_runtime() -> bool:
    return sys.platform == "win32"


def _validate_windows_components(path: Path, *, what: str) -> None:
    text = str(path)
    components = path.parts[1:] if path.anchor else path.parts
    for component in components:
        if component in {".", ".."}:
            continue
        reason: str | None = None
        if any(ord(character) < 32 for character in component):
            reason = "control_character"
        elif any(character in '<>:"|?*' for character in component):
            reason = "invalid_or_stream_character"
        elif component.endswith((".", " ")):
            reason = "trailing_dot_or_space"
        elif component.split(".", 1)[0].rstrip(" .").upper() in _WINDOWS_RESERVED_NAMES:
            reason = "reserved_dos_name"
        if reason is not None:
            raise ContractViolation(
                f"{what} contains a Windows path component that is not safe for local output",
                detail={"what": what, "path": text, "component": component, "reason": reason},
            )


def _validate_windows_local_path(path: Path, *, what: str) -> None:
    text = str(path)
    if text.startswith(("\\\\", "//")) or len(path.drive) != 2 or path.drive[1] != ":":
        raise ContractViolation(
            f"{what} must use a local Windows drive path",
            detail={"what": what, "path": text, "reason": "unc_or_device_namespace"},
        )
    _validate_windows_components(path, what=what)


def plan_confined_target(root: Path, target: Path, *, what: str) -> Path:
    """Return the lexical absolute target after proving it is below ``root``.

    This deliberately does not resolve target components: resolution would
    follow an attacker-controlled link before the handle-relative writer gets
    a chance to reject it.
    """
    if os.name == "nt":
        # Validate before ``abspath`` because Win32 normalisation silently strips
        # trailing dots/spaces and would erase the ambiguity we must refuse.
        _validate_windows_components(root, what=what)
        _validate_windows_components(target, what=what)
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100 - must not follow links
    absolute_target = Path(os.path.abspath(target))  # noqa: PTH100 - must not follow links
    if os.name == "nt":
        _validate_windows_local_path(absolute_root, what=what)
        _validate_windows_local_path(absolute_target, what=what)
    try:
        common = Path(os.path.commonpath((absolute_root, absolute_target)))
    except ValueError as exc:
        raise ContractViolation(
            f"{what} must be located inside the root named on the command line",
            detail={"what": what, "root": str(absolute_root), "requested": str(absolute_target)},
        ) from exc
    if os.path.normcase(common) != os.path.normcase(absolute_root) or os.path.normcase(
        absolute_target
    ) == os.path.normcase(absolute_root):
        raise ContractViolation(
            f"{what} must be located inside the root named on the command line",
            detail={"what": what, "root": str(absolute_root), "requested": str(absolute_target)},
        )
    return absolute_target


def write_new_file(root: Path, target: Path, data: bytes, *, what: str) -> Path:
    """Durably publish ``data`` at a new confined target and return its path."""
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100 - must not follow links
    absolute_target = plan_confined_target(absolute_root, target, what=what)
    relative = Path(os.path.relpath(absolute_target, absolute_root))
    if sys.platform == "win32":
        _write_windows(absolute_root, relative, data, what=what, replace=False)
    else:
        _write_posix(absolute_root, relative, data, what=what, replace=False)
    return absolute_target


def replace_file(root: Path, target: Path, data: bytes, *, what: str) -> Path:
    """Atomically publish ``data`` at a confined target and return its path.

    Parent containment is handle-bound, but this is an unconditional leaf
    replacement. Portable POSIX and Windows APIs do not expose a shared
    compare-bytes-and-swap operation: a caller that protects third-party leaf
    content must validate immediately before this call and retain a durable
    backup/recovery journal. A non-cooperating process can still replace the
    leaf in the final interval between that validation and the rename syscall.
    """
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100 - must not follow links
    absolute_target = plan_confined_target(absolute_root, target, what=what)
    relative = Path(os.path.relpath(absolute_target, absolute_root))
    if sys.platform == "win32":
        _write_windows(absolute_root, relative, data, what=what, replace=True)
    else:
        _write_posix(absolute_root, relative, data, what=what, replace=True)
    return absolute_target


def remove_file(root: Path, target: Path, *, what: str) -> Path:
    """Remove one confined regular file without following redirected parents."""
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100 - must not follow links
    absolute_target = plan_confined_target(absolute_root, target, what=what)
    relative = Path(os.path.relpath(absolute_target, absolute_root))
    if sys.platform == "win32":
        _remove_windows(absolute_root, relative, what=what)
    else:
        _remove_posix(absolute_root, relative, what=what)
    return absolute_target


#: Hard ceiling on a single read syscall, independent of any caller-supplied
#: byte budget. Bounds peak memory for one chunk regardless of file size.
_READ_CHUNK_BYTES: Final = 1 << 20


def _require_positive_read_limit(value: object, *, what: str) -> int:
    """Validate a caller-supplied byte limit whose static type may lie.

    Accepting ``object`` (rather than trusting the public ``int`` annotation)
    is what keeps the ``isinstance`` checks meaningful instead of tautological
    under strict type-checking, while still refusing at runtime whatever a
    caller who ignores the annotation actually passes.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractViolation(
            f"{what} read limit must be a positive number of bytes",
            detail={"what": what, "max_bytes": repr(value)},
        )
    return value


def read_confined_file(root: Path, target: Path, *, max_bytes: int, what: str) -> bytes:
    """Read at most ``max_bytes`` from an existing confined regular file.

    Mirrors :func:`write_new_file`'s non-following, handle-relative
    containment: every directory component between the root and the target is
    opened without following symlinks or Windows reparse points, and the final
    component must open as an existing regular file, opened without following
    a symlink or reparse point either. A file larger than ``max_bytes`` is
    refused outright rather than silently truncated. Never creates a directory
    or a file.

    Every read is bound to the opened handle's own size and modification time,
    checked immediately before and after the read: a file observably grown,
    shrunk or modified in place during the read is refused rather than
    silently returned. This cannot detect a path-level replacement that
    leaves the already-open handle's underlying file untouched (ordinary
    rename/unlink semantics on both platforms), and it does not make several
    separate reads atomic with each other; it only proves that *this* read's
    own bytes were not observably disturbed while they were being read.
    """
    checked_max_bytes = _require_positive_read_limit(max_bytes, what=what)
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100 - must not follow links
    absolute_target = plan_confined_target(absolute_root, target, what=what)
    relative = Path(os.path.relpath(absolute_target, absolute_root))
    if sys.platform == "win32":
        data = _read_windows(absolute_root, relative, max_bytes=checked_max_bytes, what=what)
    else:
        data = _read_posix(absolute_root, relative, max_bytes=checked_max_bytes, what=what)
    return data


def list_confined_json_files(
    root: Path,
    target: Path,
    *,
    max_entries: object,
    what: str,
) -> tuple[list[Path], bool]:
    """Enumerate regular ``.json`` files below a confined directory.

    Every directory is opened without following links before enumeration. The
    returned boolean is true only when the entry budget truncates traversal;
    unsafe or unstable directory entries raise :class:`ContractViolation`.
    """
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
        raise ContractViolation(
            f"{what} entry limit must be a positive integer",
            detail={"what": what, "max_entries": repr(max_entries)},
        )
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100 - must not follow links
    absolute_target = plan_confined_target(absolute_root, target, what=what)
    relative = Path(os.path.relpath(absolute_target, absolute_root))
    if _is_windows_runtime():
        raise ContractViolation(
            f"{what} cannot be enumerated with handle-bound semantics on Windows",
            detail={
                "what": what,
                "path": str(absolute_target),
                "reason": "windows_handle_bound_enumeration_unavailable",
            },
        )
    paths, truncated = _list_posix_json(absolute_root, relative, max_entries=max_entries, what=what)
    return sorted(paths, key=lambda item: item.as_posix()), truncated


def confined_directory_exists(root: Path, target: Path, *, what: str) -> bool:
    """Check a confined directory through non-following directory handles."""
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100 - must not follow links
    absolute_target = plan_confined_target(absolute_root, target, what=what)
    relative = Path(os.path.relpath(absolute_target, absolute_root))
    if _is_windows_runtime():
        return _directory_exists_windows(absolute_root, relative, what=what)
    return _directory_exists_posix(absolute_root, relative, what=what)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written == 0:  # pragma: no cover - defensive kernel boundary
            raise OSError("zero-length filesystem write")
        view = view[written:]


def _open_or_create_directory_posix(
    parent: int,
    component: str,
    path: Path,
    *,
    what: str,
    directory_flags: int,
) -> int:
    try:
        return os.open(component, directory_flags, dir_fd=parent)
    except FileNotFoundError:
        try:
            os.mkdir(component, mode=0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        try:
            return os.open(component, directory_flags, dir_fd=parent)
        except OSError as exc:
            raise ContractViolation(
                f"{what} contains a directory component that cannot be opened safely",
                detail={"what": what, "path": str(path)},
            ) from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ContractViolation(
                f"{what} contains a directory component that cannot be opened safely",
                detail={"what": what, "path": str(path)},
            ) from exc
        raise


def _open_directory_posix_readonly(
    parent: int,
    component: str,
    path: Path,
    *,
    what: str,
    directory_flags: int,
) -> int:
    try:
        return os.open(component, directory_flags, dir_fd=parent)
    except FileNotFoundError as exc:
        raise ContractViolation(
            f"{what} does not exist",
            detail={"what": what, "path": str(path)},
        ) from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ContractViolation(
                f"{what} traverses a symbolic link or a non-directory component; refusing the read",
                detail={"what": what, "path": str(path)},
            ) from exc
        raise


def _read_posix(root: Path, relative: Path, *, max_bytes: int, what: str) -> bytes:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    # ``O_NONBLOCK`` is what stops opening a FIFO with no writer from hanging;
    # it has no effect on a regular file open.
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptors: list[int] = []
    file_descriptor: int | None = None
    final_path = root / relative
    try:
        anchor = Path(root.anchor)
        current = os.open(anchor, directory_flags)
        descriptors.append(current)
        traversed = anchor
        directory_components = (*root.parts[1:], *relative.parts[:-1])
        for component in directory_components:
            traversed /= component
            child = _open_directory_posix_readonly(
                current,
                component,
                traversed,
                what=what,
                directory_flags=directory_flags,
            )
            descriptors.append(child)
            current = child

        final_name = relative.name
        try:
            file_descriptor = os.open(final_name, file_flags, dir_fd=current)
        except FileNotFoundError as exc:
            raise ContractViolation(
                f"{what} does not exist",
                detail={"what": what, "path": str(final_path)},
            ) from exc
        status = os.fstat(file_descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ContractViolation(
                f"{what} is not a regular file; refusing the read",
                detail={"what": what, "path": str(final_path)},
            )
        if status.st_size > max_bytes:
            raise ContractViolation(
                f"{what} exceeds the declared byte limit",
                detail={
                    "what": what,
                    "path": str(final_path),
                    "size": status.st_size,
                    "limit": max_bytes,
                },
            )
        declared_size = status.st_size
        remaining = declared_size
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(file_descriptor, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                raise ContractViolation(
                    f"{what} ended before its declared length; refusing the read",
                    detail={
                        "what": what,
                        "path": str(final_path),
                        "declared_size": declared_size,
                        "bytes_read": declared_size - remaining,
                    },
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(file_descriptor)
        if after.st_size != declared_size or after.st_mtime_ns != status.st_mtime_ns:
            raise ContractViolation(
                f"{what} was modified while being read; refusing the read",
                detail={"what": what, "path": str(final_path)},
            )
        return b"".join(chunks)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ContractViolation(
                f"{what} traverses a symbolic link; refusing the read",
                detail={"what": what, "path": str(final_path)},
            ) from exc
        raise
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_posix(root: Path, relative: Path, data: bytes, *, what: str, replace: bool) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    temporary_name: str | None = None
    try:
        anchor = Path(root.anchor)
        current = os.open(anchor, directory_flags)
        descriptors.append(current)
        traversed = anchor
        directory_components = (*root.parts[1:], *relative.parts[:-1])
        for component in directory_components:
            traversed /= component
            child = _open_or_create_directory_posix(
                current,
                component,
                traversed,
                what=what,
                directory_flags=directory_flags,
            )
            descriptors.append(child)
            current = child

        final_name = relative.name
        temporary_name = f"{_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=current,
        )
        try:
            _write_all(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if replace:
            os.replace(temporary_name, final_name, src_dir_fd=current, dst_dir_fd=current)
            temporary_name = None
        else:
            try:
                os.link(
                    temporary_name,
                    final_name,
                    src_dir_fd=current,
                    dst_dir_fd=current,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise ContractViolation(
                    f"{what} already exists; refusing to overwrite it",
                    detail={"what": what, "path": str(root / relative)},
                ) from exc
            os.unlink(temporary_name, dir_fd=current)
            temporary_name = None
        os.fsync(current)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ContractViolation(
                f"{what} traverses a symbolic link; refusing the write",
                detail={"what": what, "path": str(root / relative)},
            ) from exc
        raise
    finally:
        if temporary_name is not None and descriptors:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=descriptors[-1])
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _remove_posix(root: Path, relative: Path, *, what: str) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        anchor = Path(root.anchor)
        current = os.open(anchor, directory_flags)
        descriptors.append(current)
        traversed = anchor
        directory_components = (*root.parts[1:], *relative.parts[:-1])
        for component in directory_components:
            traversed /= component
            child = _open_directory_posix_readonly(
                current,
                component,
                traversed,
                what=what,
                directory_flags=directory_flags,
            )
            descriptors.append(child)
            current = child
        status = os.stat(relative.name, dir_fd=current, follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode):
            raise ContractViolation(
                f"{what} is not a regular file; refusing the removal",
                detail={"what": what, "path": str(root / relative)},
            )
        os.unlink(relative.name, dir_fd=current)
        os.fsync(current)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ContractViolation(
                f"{what} traverses a symbolic link; refusing the removal",
                detail={"what": what, "path": str(root / relative)},
            ) from exc
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _list_posix_json(
    root: Path, relative: Path, *, max_entries: int, what: str
) -> tuple[list[Path], bool]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    paths: list[Path] = []
    visited = 0

    def walk(directory: int, logical: Path, *, depth: int) -> bool:
        nonlocal visited
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > max_entries:
                        return True
                    info = entry.stat(follow_symlinks=False)
                    child_path = logical / entry.name
                    if stat.S_ISLNK(info.st_mode):
                        raise ContractViolation(
                            f"{what} contains a symbolic link; refusing enumeration",
                            detail={"what": what, "path": str(child_path)},
                        )
                    if stat.S_ISDIR(info.st_mode):
                        if depth >= _MAX_ENUMERATION_DEPTH:
                            raise ContractViolation(
                                f"{what} exceeds the safe directory depth",
                                detail={"what": what, "path": str(child_path)},
                            )
                        child = _open_directory_posix_readonly(
                            directory,
                            entry.name,
                            child_path,
                            what=what,
                            directory_flags=directory_flags,
                        )
                        try:
                            if walk(child, child_path, depth=depth + 1):
                                return True
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and entry.name.endswith(".json"):
                        paths.append(child_path)
        except ContractViolation:
            raise
        except OSError as exc:
            raise ContractViolation(
                f"{what} changed or could not be enumerated safely",
                detail={"what": what, "path": str(logical)},
            ) from exc
        return False

    try:
        anchor = Path(root.anchor)
        current = os.open(anchor, directory_flags)
        descriptors.append(current)
        traversed = anchor
        for component in (*root.parts[1:], *relative.parts):
            traversed /= component
            current = _open_directory_posix_readonly(
                current,
                component,
                traversed,
                what=what,
                directory_flags=directory_flags,
            )
            descriptors.append(current)
        return paths, walk(current, root / relative, depth=0)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _directory_exists_posix(root: Path, relative: Path, *, what: str) -> bool:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        anchor = Path(root.anchor)
        current = os.open(anchor, directory_flags)
        descriptors.append(current)
        traversed = anchor
        for component in (*root.parts[1:], *relative.parts):
            traversed /= component
            try:
                child = os.open(component, directory_flags, dir_fd=current)
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise ContractViolation(
                    f"{what} contains a directory component that cannot be opened safely",
                    detail={"what": what, "path": str(traversed)},
                ) from exc
            descriptors.append(child)
            current = child
        return True
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


if sys.platform != "win32":

    def _directory_exists_windows(root: Path, relative: Path, *, what: str) -> bool:
        del root, relative, what
        raise RuntimeError("Windows directory handles are unavailable on this platform")


if sys.platform == "win32":
    from ctypes import wintypes

    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_ADD_FILE = 0x0002
    _FILE_ADD_SUBDIRECTORY = 0x0004
    _FILE_TRAVERSE = 0x0020
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_WRITE_DATA = 0x0002
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000
    _DIRECTORY_ACCESS = (
        _FILE_LIST_DIRECTORY
        | _FILE_ADD_FILE
        | _FILE_ADD_SUBDIRECTORY
        | _FILE_TRAVERSE
        | _FILE_READ_ATTRIBUTES
        | _SYNCHRONIZE
    )
    _DIRECTORY_TRAVERSE_ACCESS = (
        _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    )
    _FILE_ACCESS = _FILE_WRITE_DATA | _FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE
    _FILE_READ_DATA = 0x0001
    _FILE_READ_ACCESS = _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    # Denying FILE_SHARE_DELETE pins every opened directory in the namespace
    # until publication completes. An attacker cannot rename a parent out of
    # the confined root after we have validated and opened it.
    _SHARE_READ_WRITE = 0x00000001 | 0x00000002
    _FILE_OPEN = 1
    _FILE_CREATE = 2
    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_DELETE_ON_CLOSE = 0x00001000
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    class _FileDispositionInformation(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    class _FileStandardInfo(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.ULONG),
            ("DeletePending", wintypes.BOOLEAN),
            ("Directory", wintypes.BOOLEAN),
        ]

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    _ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
    _ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _ntdll.NtCreateFile.restype = ctypes.c_long
    _ntdll.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    _ntdll.NtSetInformationFile.restype = ctypes.c_long

    def _raise_windows_error(status: int, path: Path) -> None:
        error = int(_ntdll.RtlNtStatusToDosError(status))
        if error in {2, 3}:
            raise FileNotFoundError(error, ctypes.FormatError(error), str(path))
        if error == 5:
            raise PermissionError(error, ctypes.FormatError(error), str(path))
        if error in {80, 183}:
            raise FileExistsError(error, ctypes.FormatError(error), str(path))
        raise OSError(error, ctypes.FormatError(error), str(path))

    def _reject_reparse(handle: int, path: Path, *, what: str) -> None:
        info = _FileAttributeTagInfo()
        if not _kernel32.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(path))
        if info.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ContractViolation(
                f"{what} traverses a Windows reparse point; refusing the operation",
                detail={"what": what, "path": str(path)},
            )

    def _open_windows_anchor(anchor: Path, *, what: str) -> int:
        handle = _kernel32.CreateFileW(
            str(anchor),
            _DIRECTORY_TRAVERSE_ACCESS,
            _SHARE_READ_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(anchor))
        try:
            _reject_reparse(handle, anchor, what=what)
        except BaseException:
            _kernel32.CloseHandle(handle)
            raise
        return int(handle)

    def _nt_create_relative(
        parent: int,
        name: str,
        *,
        access: int,
        disposition: int,
        options: int,
        path: Path,
    ) -> int:
        name_buffer = ctypes.create_unicode_buffer(name)
        encoded_length = len(name.encode("utf-16-le"))
        unicode_name = _UnicodeString(
            encoded_length, encoded_length + 2, ctypes.cast(name_buffer, wintypes.LPWSTR)
        )
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            parent,
            ctypes.pointer(unicode_name),
            _OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        io_status = _IoStatusBlock()
        handle = wintypes.HANDLE()
        status = int(
            _ntdll.NtCreateFile(
                ctypes.byref(handle),
                access,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                _FILE_ATTRIBUTE_NORMAL,
                _SHARE_READ_WRITE,
                disposition,
                options | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
                None,
                0,
            )
        )
        if status < 0:
            _raise_windows_error(status, path)
        if handle.value is None:  # pragma: no cover - successful NT call invariant
            raise OSError("NtCreateFile returned an empty handle")
        return int(handle.value)

    def _open_or_create_directory_windows(parent: int, name: str, path: Path, *, what: str) -> int:
        try:
            handle = _nt_create_relative(
                parent,
                name,
                access=_DIRECTORY_ACCESS,
                disposition=_FILE_OPEN,
                options=_FILE_DIRECTORY_FILE,
                path=path,
            )
        except FileNotFoundError:
            try:
                handle = _nt_create_relative(
                    parent,
                    name,
                    access=_DIRECTORY_ACCESS,
                    disposition=_FILE_CREATE,
                    options=_FILE_DIRECTORY_FILE,
                    path=path,
                )
            except FileExistsError:
                handle = _nt_create_relative(
                    parent,
                    name,
                    access=_DIRECTORY_ACCESS,
                    disposition=_FILE_OPEN,
                    options=_FILE_DIRECTORY_FILE,
                    path=path,
                )
        except PermissionError:
            try:
                handle = _nt_create_relative(
                    parent,
                    name,
                    access=_DIRECTORY_TRAVERSE_ACCESS,
                    disposition=_FILE_OPEN,
                    options=_FILE_DIRECTORY_FILE,
                    path=path,
                )
            except OSError as exc:
                raise ContractViolation(
                    f"{what} contains a directory component that cannot be opened safely",
                    detail={"what": what, "path": str(path)},
                ) from exc
        except OSError as exc:
            raise ContractViolation(
                f"{what} contains a directory component that cannot be opened safely",
                detail={"what": what, "path": str(path)},
            ) from exc
        try:
            _reject_reparse(handle, path, what=what)
        except BaseException:
            _kernel32.CloseHandle(handle)
            raise
        return handle

    def _set_windows_disposition(handle: int, *, delete: bool) -> None:
        disposition = _FileDispositionInformation(delete)
        io_status = _IoStatusBlock()
        status = int(
            _ntdll.NtSetInformationFile(
                handle,
                ctypes.byref(io_status),
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
                13,
            )
        )
        if status < 0:
            _raise_windows_error(status, Path("<temporary-output-handle>"))

    def _dispose_windows_file(handle: int) -> None:
        _set_windows_disposition(handle, delete=True)

    def _link_windows_file(handle: int, parent: int, final_name: str, path: Path) -> None:
        class _FileLinkInformation(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.BOOLEAN),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.ULONG),
                ("FileName", wintypes.WCHAR * len(final_name)),
            ]

        link = _FileLinkInformation(False, parent, len(final_name.encode("utf-16-le")), final_name)
        io_status = _IoStatusBlock()
        status = int(
            _ntdll.NtSetInformationFile(
                handle,
                ctypes.byref(io_status),
                ctypes.byref(link),
                ctypes.sizeof(link),
                11,
            )
        )
        if status < 0:
            _raise_windows_error(status, path)

    def _rename_windows_file(handle: int, parent: int, final_name: str, path: Path) -> None:
        class _FileRenameInformation(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.BOOLEAN),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.ULONG),
                ("FileName", wintypes.WCHAR * len(final_name)),
            ]

        rename = _FileRenameInformation(
            True, parent, len(final_name.encode("utf-16-le")), final_name
        )
        io_status = _IoStatusBlock()
        status = int(
            _ntdll.NtSetInformationFile(
                handle,
                ctypes.byref(io_status),
                ctypes.byref(rename),
                ctypes.sizeof(rename),
                10,
            )
        )
        if status < 0:
            _raise_windows_error(status, path)

    def _open_directory_windows_readonly(parent: int, name: str, path: Path, *, what: str) -> int:
        try:
            handle = _nt_create_relative(
                parent,
                name,
                access=_DIRECTORY_TRAVERSE_ACCESS,
                disposition=_FILE_OPEN,
                options=_FILE_DIRECTORY_FILE,
                path=path,
            )
        except FileNotFoundError as exc:
            raise ContractViolation(
                f"{what} does not exist",
                detail={"what": what, "path": str(path)},
            ) from exc
        except OSError as exc:
            raise ContractViolation(
                f"{what} contains a directory component that cannot be opened safely",
                detail={"what": what, "path": str(path)},
            ) from exc
        try:
            _reject_reparse(handle, path, what=what)
        except BaseException:
            _kernel32.CloseHandle(handle)
            raise
        return handle

    def _open_file_windows_readonly(parent: int, name: str, path: Path, *, what: str) -> int:
        try:
            handle = _nt_create_relative(
                parent,
                name,
                access=_FILE_READ_ACCESS,
                disposition=_FILE_OPEN,
                # FILE_NON_DIRECTORY_FILE is what makes NtCreateFile refuse a
                # directory here instead of silently handing back a handle to it.
                options=_FILE_NON_DIRECTORY_FILE,
                path=path,
            )
        except FileNotFoundError as exc:
            raise ContractViolation(
                f"{what} does not exist",
                detail={"what": what, "path": str(path)},
            ) from exc
        except OSError as exc:
            raise ContractViolation(
                f"{what} cannot be opened as a regular file",
                detail={"what": what, "path": str(path)},
            ) from exc
        try:
            _reject_reparse(handle, path, what=what)
        except BaseException:
            _kernel32.CloseHandle(handle)
            raise
        return handle

    def _file_size_windows(handle: int, path: Path) -> int:
        info = _FileStandardInfo()
        if not _kernel32.GetFileInformationByHandleEx(
            handle, 1, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(path))
        if info.EndOfFile < 0:
            raise OSError(f"negative file size reported for {path}")
        return int(info.EndOfFile)

    def _file_last_write_time_windows(handle: int, path: Path) -> int:
        info = _FileBasicInfo()
        if not _kernel32.GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(path))
        return int(info.LastWriteTime)

    def _read_all_windows(handle: int, size: int, path: Path, *, what: str) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk_size = min(remaining, _READ_CHUNK_BYTES)
            buffer = ctypes.create_string_buffer(chunk_size)
            read_count = wintypes.DWORD()
            if not _kernel32.ReadFile(handle, buffer, chunk_size, ctypes.byref(read_count), None):
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error), str(path))
            count = read_count.value
            if count == 0:
                raise ContractViolation(
                    f"{what} ended before its declared length; refusing the read",
                    detail={
                        "what": what,
                        "path": str(path),
                        "declared_size": size,
                        "bytes_read": size - remaining,
                    },
                )
            chunks.append(buffer.raw[:count])
            remaining -= count
        return b"".join(chunks)

    def _read_windows(root: Path, relative: Path, *, max_bytes: int, what: str) -> bytes:
        handles: list[int] = []
        file_handle: int | None = None
        final_path = root / relative
        try:
            anchor = Path(root.anchor)
            current = _open_windows_anchor(anchor, what=what)
            handles.append(current)
            traversed = anchor
            directory_components = (*root.parts[1:], *relative.parts[:-1])
            for component in directory_components:
                traversed /= component
                current = _open_directory_windows_readonly(current, component, traversed, what=what)
                handles.append(current)
            file_handle = _open_file_windows_readonly(current, relative.name, final_path, what=what)
            size = _file_size_windows(file_handle, final_path)
            if size > max_bytes:
                raise ContractViolation(
                    f"{what} exceeds the declared byte limit",
                    detail={
                        "what": what,
                        "path": str(final_path),
                        "size": size,
                        "limit": max_bytes,
                    },
                )
            write_time_before = _file_last_write_time_windows(file_handle, final_path)
            data = _read_all_windows(file_handle, size, final_path, what=what)
            write_time_after = _file_last_write_time_windows(file_handle, final_path)
            if (
                write_time_after != write_time_before
                or _file_size_windows(file_handle, final_path) != size
            ):
                raise ContractViolation(
                    f"{what} was modified while being read; refusing the read",
                    detail={"what": what, "path": str(final_path)},
                )
            return data
        finally:
            if file_handle is not None:
                _kernel32.CloseHandle(file_handle)
            for handle in reversed(handles):
                _kernel32.CloseHandle(handle)

    def _write_windows(
        root: Path, relative: Path, data: bytes, *, what: str, replace: bool
    ) -> None:
        handles: list[int] = []
        temporary: int | None = None
        final_path = root / relative
        try:
            anchor = Path(root.anchor)
            current = _open_windows_anchor(anchor, what=what)
            handles.append(current)
            traversed = anchor
            directory_components = (*root.parts[1:], *relative.parts[:-1])
            for component in directory_components:
                traversed /= component
                current = _open_or_create_directory_windows(
                    current, component, traversed, what=what
                )
                handles.append(current)
            temporary_name = f"{_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
            temporary = _nt_create_relative(
                current,
                temporary_name,
                access=_FILE_ACCESS,
                disposition=_FILE_CREATE,
                options=(_FILE_NON_DIRECTORY_FILE | (0 if replace else _FILE_DELETE_ON_CLOSE)),
                path=final_path.parent / temporary_name,
            )
            if data:
                buffer = ctypes.create_string_buffer(data)
                written = wintypes.DWORD()
                if not _kernel32.WriteFile(
                    temporary, buffer, len(data), ctypes.byref(written), None
                ):
                    error = ctypes.get_last_error()
                    raise OSError(error, ctypes.FormatError(error), str(final_path))
                if written.value != len(data):
                    raise OSError("short filesystem write")
            if not _kernel32.FlushFileBuffers(temporary):
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error), str(final_path))
            if replace:
                _rename_windows_file(temporary, current, relative.name, final_path)
            else:
                try:
                    _link_windows_file(temporary, current, relative.name, final_path)
                except FileExistsError as exc:
                    raise ContractViolation(
                        f"{what} already exists; refusing to overwrite it",
                        detail={"what": what, "path": str(final_path)},
                    ) from exc
            if not _kernel32.FlushFileBuffers(temporary):
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error), str(final_path))
            _kernel32.CloseHandle(temporary)
            temporary = None
        except BaseException as primary:
            if temporary is not None:
                try:
                    _dispose_windows_file(temporary)
                except BaseException as cleanup_error:
                    primary.add_note(
                        "temporary cleanup could not be reaffirmed; "
                        f"FILE_DELETE_ON_CLOSE remains active: {cleanup_error}"
                    )
            raise
        finally:
            if temporary is not None:
                _kernel32.CloseHandle(temporary)
            for handle in reversed(handles):
                _kernel32.CloseHandle(handle)

    def _remove_windows(root: Path, relative: Path, *, what: str) -> None:
        handles: list[int] = []
        file_handle: int | None = None
        final_path = root / relative
        try:
            anchor = Path(root.anchor)
            current = _open_windows_anchor(anchor, what=what)
            handles.append(current)
            traversed = anchor
            directory_components = (*root.parts[1:], *relative.parts[:-1])
            for component in directory_components:
                traversed /= component
                current = _open_directory_windows_readonly(current, component, traversed, what=what)
                handles.append(current)
            file_handle = _nt_create_relative(
                current,
                relative.name,
                access=_FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE,
                disposition=_FILE_OPEN,
                options=_FILE_NON_DIRECTORY_FILE,
                path=final_path,
            )
            _reject_reparse(file_handle, final_path, what=what)
            _set_windows_disposition(file_handle, delete=True)
        finally:
            if file_handle is not None:
                _kernel32.CloseHandle(file_handle)
            for handle in reversed(handles):
                _kernel32.CloseHandle(handle)

    def _directory_exists_windows(root: Path, relative: Path, *, what: str) -> bool:
        handles: list[int] = []
        try:
            anchor = Path(root.anchor)
            current = _open_windows_anchor(anchor, what=what)
            handles.append(current)
            traversed = anchor
            for component in (*root.parts[1:], *relative.parts):
                traversed /= component
                try:
                    child = _nt_create_relative(
                        current,
                        component,
                        access=_DIRECTORY_TRAVERSE_ACCESS,
                        disposition=_FILE_OPEN,
                        options=_FILE_DIRECTORY_FILE,
                        path=traversed,
                    )
                except FileNotFoundError:
                    return False
                except OSError as exc:
                    raise ContractViolation(
                        f"{what} contains a directory component that cannot be opened safely",
                        detail={"what": what, "path": str(traversed)},
                    ) from exc
                try:
                    _reject_reparse(child, traversed, what=what)
                except BaseException:
                    _kernel32.CloseHandle(child)
                    raise
                handles.append(child)
                current = child
            return True
        finally:
            for handle in reversed(handles):
                _kernel32.CloseHandle(handle)
