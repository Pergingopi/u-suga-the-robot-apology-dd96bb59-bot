#!/usr/bin/env python3
"""The Robot Apology — make.fun bounty u-suga-the-robot-apology-dd96bb59.

One job: write a single two-line robot apology to
``$MAKEFUN_OUTPUT_DIR/entry.txt`` (default ``/output``), then exit 0.
Python 3.13 standard library only, fully deterministic — no clock,
random, or network input anywhere in the pipeline, so two runs on the
same machine produce byte-identical output.

How the write is hardened
-------------------------
The output directory is created on demand (see
``resolve_output_dir``), so a fresh mount point or a brand-new temp
directory is not an error. The bytes themselves are written to a
freshly created temp file in the same directory (``tempfile.mkstemp``,
which opens with ``O_EXCL``), fsynced, read back and compared, and
only then moved into place with an atomic ``os.replace``; the
containing directory is fsynced afterwards and the finished file is
forced to mode 0644. A crashed earlier run can leave a ``.entry-*.tmp``
file behind; stale ones are swept up best-effort before the write,
while young ones — possibly a concurrent writer's — are left alone.

How this module is organised
----------------------------
* ``ENTRY_TEXT`` — the entry itself, a module constant, so the text
  under test is exactly the text that ships.
* ``validate_entry`` — the gate rules (non-empty, no leading/trailing
  whitespace, no forbidden control characters, 1–400 characters) with
  the character scan and the size check factored out into
  ``_find_forbidden_control_char`` and ``_check_entry_size``.
* ``resolve_output_dir`` — directory resolution: a blank or missing
  ``MAKEFUN_OUTPUT_DIR`` falls back to ``/output``; any other value
  is used (after stripping surrounding whitespace) and created with
  ``os.makedirs(..., exist_ok=True)`` via ``_ensure_directory``.
* ``cleanup_stale_tmp_files`` — the stale-temp sweep, with the
  per-file age test and the retrying removal in ``_is_stale_tmp_file``
  and ``_remove_with_retries``.
* ``write_entry_atomically`` — the temp-file write, read-back check,
  and atomic replace, split into ``_write_temp_file`` and
  ``_verify_temp_file`` so each step reads on its own.
* ``main`` — thin orchestration only: validate, resolve, write.
"""

import os
import stat
import sys
import tempfile
import time

ENTRY_TEXT = (
    "I'm sorry I deleted the voicemail from your late grandmother "
    "while clearing out old messages I thought were duplicates.\n"
    "I learned that old does not mean unwanted, so now I quarantine "
    "every message from a saved contact and ask before I delete anything."
)

MAX_CHARS = 400
ENTRY_FILENAME = "entry.txt"
DEFAULT_OUTPUT_DIR = "/output"
TMP_PREFIX = ".entry-"
TMP_SUFFIX = ".tmp"
FINAL_MODE = 0o644
# Stale temp files older than this are treated as leftovers from a crashed
# run and removed; younger ones may belong to a concurrent writer.
STALE_TMP_MAX_AGE_SECONDS = 3600.0
STALE_TMP_RETRIES = 3
STALE_TMP_RETRY_DELAY_SECONDS = 0.1

# Whitespace characters the gate tolerates inside the entry text.
_ALLOWED_CONTROL_WHITESPACE = ("\n", "\t")


def _find_forbidden_control_char(text: str) -> str | None:
    """Return the first forbidden control character in ``text``, or None.

    Control characters below U+0020 are forbidden, except for the
    whitespace the gate tolerates (newline and tab). Anything else down
    there — a NUL, a bell, an escape — would make the entry invalid
    UTF-8 text for the scorer, so it is rejected here first.
    """
    for ch in text:
        if ord(ch) < 0x20 and ch not in _ALLOWED_CONTROL_WHITESPACE:
            return ch
    return None


def _check_entry_size(text: str, data: bytes) -> None:
    """Reject ``text`` if it breaches the 400-character gate.

    Both measures are checked: the character count (what the rules
    state) and the UTF-8 byte count (what the scorer reads), because a
    text heavy in multi-byte characters could pass one and fail the
    other.
    """
    if len(text) > MAX_CHARS:
        raise ValueError(
            f"entry text is {len(text)} characters, over the "
            f"{MAX_CHARS}-character gate"
        )
    if len(data) > MAX_CHARS:
        raise ValueError(
            f"entry text is {len(data)} UTF-8 bytes, over the "
            f"{MAX_CHARS}-byte gate"
        )


def validate_entry(text: str) -> bytes:
    """Validate ``text`` against every rule of the competition gate.

    Returns the UTF-8 encoding of ``text`` so callers never re-encode
    (and risk silently changing) the validated string.

    Raises
    ------
    ValueError
        If the text is not a string, is empty or only whitespace,
        starts or ends with whitespace, contains a forbidden control
        character, or exceeds the 400-character gate.
    """
    if not isinstance(text, str):
        raise ValueError("entry text must be a str")
    if not text:
        raise ValueError("entry text must not be empty")
    if text != text.strip():
        raise ValueError("entry text must not start or end with whitespace")
    bad = _find_forbidden_control_char(text)
    if bad is not None:
        raise ValueError(
            "entry text contains a forbidden control character "
            f"(U+{ord(bad):04X})"
        )
    if not text.strip():
        raise ValueError("entry text must not be only whitespace")
    data = text.encode("utf-8")
    _check_entry_size(text, data)
    return data


def _normalise_env_dir() -> str:
    """Read ``MAKEFUN_OUTPUT_DIR`` and return its stripped value.

    Returns an empty string when the variable is unset, empty, or
    whitespace-only, so the caller can treat all three the same way
    and fall back to the default instead of failing on a blank path.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR")
    return raw.strip() if raw is not None else ""


def _ensure_directory(path: str) -> str:
    """Create ``path`` if needed and confirm it is a directory.

    ``os.makedirs`` with ``exist_ok=True`` creates every missing
    parent as well as the directory itself, and is a no-op when the
    directory already exists — which is the normal case when the
    grader has mounted the output directory for us. Afterwards the
    path is verified to really be a directory, so a regular file
    sitting where the directory should be is reported plainly instead
    of failing later, mid-write, with a confusing error.

    Raises
    ------
    NotADirectoryError
        If ``path`` exists but is not a directory.
    OSError
        If the directory cannot be created (for example, permission
        denied on a read-only filesystem).
    """
    try:
        os.makedirs(path, exist_ok=True)
    except FileExistsError:
        # A non-directory already occupies the path; makedirs only
        # suppresses the error when the existing path is a directory.
        raise NotADirectoryError(
            f"output path is not a directory: {path}"
        ) from None
    if not os.path.isdir(path):
        raise NotADirectoryError(
            f"output path is not a directory: {path}"
        )
    return path


def resolve_output_dir() -> str:
    """Resolve the directory ``entry.txt`` is written into.

    An unset, empty, or whitespace-only ``MAKEFUN_OUTPUT_DIR`` falls
    back to ``DEFAULT_OUTPUT_DIR``; any other value is used verbatim
    (after stripping surrounding whitespace, which cannot be part of
    a real path here). Either way the directory is created on demand
    by ``_ensure_directory`` — a fresh or not-yet-mounted output
    directory is a normal situation, not an error, and previously
    this function raised ``FileNotFoundError`` for it instead.

    Raises
    ------
    NotADirectoryError
        If the resolved path exists but is not a directory.
    OSError
        If the directory cannot be created.
    """
    out_dir = _normalise_env_dir() or DEFAULT_OUTPUT_DIR
    return _ensure_directory(out_dir)


def _is_stale_tmp_file(path: str, now: float) -> bool:
    """Tell whether ``path`` is a temp file old enough to be a leftover.

    A file younger than ``STALE_TMP_MAX_AGE_SECONDS`` may belong to a
    concurrent writer, so it is never treated as stale. A file whose
    metadata cannot be read is left alone too: deleting something we
    cannot even inspect would be guessing.
    """
    try:
        age = now - os.path.getmtime(path)
    except OSError:
        return False
    return age >= STALE_TMP_MAX_AGE_SECONDS


def _remove_with_retries(path: str) -> bool:
    """Remove ``path``, retrying briefly on transient failures.

    Returns True when the file is gone (or was already gone), False
    when it still cannot be removed after ``STALE_TMP_RETRIES``
    attempts. A leftover temp file is harmless, so a failure here
    must never abort the run — the caller simply leaves it in place.
    """
    for attempt in range(STALE_TMP_RETRIES):
        try:
            os.unlink(path)
        except FileNotFoundError:
            return True  # Already gone; nothing left to do.
        except OSError:
            if attempt < STALE_TMP_RETRIES - 1:
                time.sleep(STALE_TMP_RETRY_DELAY_SECONDS)
                continue
            return False
        else:
            return True
    return False


def cleanup_stale_tmp_files(out_dir: str) -> int:
    """Remove stale ``.entry-*.tmp`` leftovers from earlier crashed runs.

    Only files older than ``STALE_TMP_MAX_AGE_SECONDS`` are removed,
    so a temp file belonging to a concurrent writer is never touched,
    and a file that cannot be removed is left in place rather than
    failing the run. Returns the number of stale temp files removed.
    """
    removed = 0
    now = time.time()
    try:
        names = os.listdir(out_dir)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(TMP_PREFIX) or not name.endswith(TMP_SUFFIX):
            continue
        path = os.path.join(out_dir, name)
        if not _is_stale_tmp_file(path, now):
            continue
        if _remove_with_retries(path):
            removed += 1
    return removed


def _fsync_dir(out_dir: str) -> None:
    """fsync ``out_dir`` so a rename inside it is durable on disk."""
    dir_fd = os.open(out_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _write_temp_file(fd: int, tmp_path: str, data: bytes) -> None:
    """Write ``data`` to the open temp file and fsync it.

    The mode is set to 0644 before any bytes land, so the file is
    never briefly world-writable (or unreadable) on disk. ``fd`` is
    owned by this function: it is closed here, success or failure.
    """
    os.chmod(tmp_path, FINAL_MODE)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_path, FINAL_MODE)


def _verify_temp_file(tmp_path: str, data: bytes) -> None:
    """Read ``tmp_path`` back and confirm it holds exactly ``data``.

    This catches a short or corrupted write before the temp file is
    allowed to replace the destination — after ``os.replace`` there
    would be no clean way back.
    """
    with open(tmp_path, "rb") as f:
        if f.read() != data:
            raise IOError("read-back verification failed")


def _discard_temp_file(tmp_path: str) -> None:
    """Best-effort removal of a temp file that will not be used."""
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


def write_entry_atomically(out_dir: str, data: bytes) -> str:
    """Write ``data`` to ``entry.txt`` inside ``out_dir``, atomically.

    The bytes go to a temp file in the same directory, are fsynced and
    read back for verification, and only then replace the destination
    with ``os.replace``. The directory is fsynced afterwards and the
    final file mode is forced to 0644. On any failure the temp file is
    removed (best effort) and the original exception propagates.

    Returns the full path of the written ``entry.txt``.
    """
    cleanup_stale_tmp_files(out_dir)
    dest_path = os.path.join(out_dir, ENTRY_FILENAME)
    fd, tmp_path = tempfile.mkstemp(
        prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=out_dir
    )
    try:
        _write_temp_file(fd, tmp_path, data)
        _verify_temp_file(tmp_path, data)
        os.replace(tmp_path, dest_path)
        _fsync_dir(out_dir)
        # Ensure the final file mode is 0644 after the replace.
        os.chmod(dest_path, FINAL_MODE)
    except BaseException:
        _discard_temp_file(tmp_path)
        raise
    return dest_path


def main() -> int:
    """Validate the entry, resolve the output directory, write it."""
    data = validate_entry(ENTRY_TEXT)
    out_dir = resolve_output_dir()
    write_entry_atomically(out_dir, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
