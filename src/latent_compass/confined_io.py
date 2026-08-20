"""Race-safe publication of a new file beneath an explicitly opened root.

The containment decision and every filesystem mutation share directory handles.
Path components are never followed through symlinks or Windows reparse points,
and publication is exclusive: a concurrent winner is never replaced.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Final

from latent_compass.errors import ContractViolation

_TEMP_PREFIX: Final = ".lc-"
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    | {f"COM{index}" for index in "¹²³"}
    | {f"LPT{index}" for index in "¹²³"}
)


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
            f"{what} must be written inside the root named on the command line",
            detail={"what": what, "root": str(absolute_root), "requested": str(absolute_target)},
        ) from exc
    if os.path.normcase(common) != os.path.normcase(absolute_root) or os.path.normcase(
        absolute_target
    ) == os.path.normcase(absolute_root):
        raise ContractViolation(
            f"{what} must be written inside the root named on the command line",
            detail={"what": what, "root": str(absolute_root), "requested": str(absolute_target)},
        )
    return absolute_target


def write_new_file(root: Path, target: Path, data: bytes, *, what: str) -> Path:
    """Durably publish ``data`` at a new confined target and return its path."""
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100 - must not follow links
    absolute_target = plan_confined_target(absolute_root, target, what=what)
    relative = Path(os.path.relpath(absolute_target, absolute_root))
    if os.name == "nt":
        _write_windows(absolute_root, relative, data, what=what)
    else:
        _write_posix(absolute_root, relative, data, what=what)
    return absolute_target


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


def _write_posix(root: Path, relative: Path, data: bytes, *, what: str) -> None:
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


if os.name == "nt":
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
            f"{what} traverses a Windows reparse point; refusing the write",
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


def _write_windows(root: Path, relative: Path, data: bytes, *, what: str) -> None:
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
            current = _open_or_create_directory_windows(current, component, traversed, what=what)
            handles.append(current)
        temporary_name = f"{_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
        temporary = _nt_create_relative(
            current,
            temporary_name,
            access=_FILE_ACCESS,
            disposition=_FILE_CREATE,
            options=_FILE_NON_DIRECTORY_FILE | _FILE_DELETE_ON_CLOSE,
            path=final_path.parent / temporary_name,
        )
        if data:
            buffer = ctypes.create_string_buffer(data)
            written = wintypes.DWORD()
            if not _kernel32.WriteFile(temporary, buffer, len(data), ctypes.byref(written), None):
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error), str(final_path))
            if written.value != len(data):
                raise OSError("short filesystem write")
        if not _kernel32.FlushFileBuffers(temporary):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(final_path))
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
