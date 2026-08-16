"""File operations via OpenSandbox execd files API."""

from __future__ import annotations

import os
import posixpath
import shutil
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp

import click
from opensandbox.models.filesystem import DirectoryListEntry, WriteEntry

from allox.context import ClientContext
from allox.checkpoint import checkpoint_after_success
from allox.utils import handle_errors, output_option, prepare_output


@click.group("file", invoke_without_command=True)
@click.pass_context
def file_group(ctx: click.Context) -> None:
    """File operations via OpenSandbox execd (ops / non-AIO path)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@file_group.command("cat")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--encoding", default="utf-8", help="File encoding.")
@output_option("raw", "json")
@click.pass_obj
@handle_errors
def file_cat(
    obj: ClientContext,
    args: tuple[str, ...],
    encoding: str,
    output_format: str | None,
) -> None:
    """Read a file from the sandbox via execd."""
    prepare_output(obj, output_format, allowed=("raw", "json"), fallback="raw")
    sandbox_id, path = _split_file_path_args(args)
    resolved_id = obj.resolve_sandbox_id(sandbox_id)
    sandbox = obj.connect_sandbox(resolved_id)
    try:
        content = sandbox.files.read_file(path, encoding=encoding)
        if obj.output.fmt == "json":
            from allox.utils import emit_json

            emit_json({"sandbox_id": resolved_id, "path": path, "content": content})
            return
        click.echo(content, nl=False)
    finally:
        sandbox.close()


@file_group.command("write")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--content", "-c", default=None, help="Content to write. Reads stdin if omitted.")
@click.option("--encoding", default="utf-8", help="File encoding.")
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def file_write(
    obj: ClientContext,
    args: tuple[str, ...],
    content: str | None,
    encoding: str,
    output_format: str | None,
) -> None:
    """Write content to a file in the sandbox via execd."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    sandbox_id, path = _split_file_path_args(args)
    resolved_id = obj.resolve_sandbox_id(sandbox_id)
    file_content = content
    if file_content is None:
        if sys.stdin.isatty():
            raise click.ClickException("Provide --content or pipe content via stdin.")
        file_content = sys.stdin.read()

    sandbox = obj.connect_sandbox(resolved_id)
    try:
        sandbox.files.write_file(path, file_content, encoding=encoding)
        obj.output.success_panel(
            {"sandbox_id": resolved_id, "path": path, "bytes": len(file_content.encode(encoding))},
            title="File Written",
        )
        checkpoint_after_success(obj, resolved_id, "file.write")
    finally:
        sandbox.close()


def _split_file_path_args(args: tuple[str, ...]) -> tuple[str | None, str]:
    """Parse ``[SANDBOX_ID] PATH`` without Click's optional-argument ambiguity."""
    if len(args) == 1:
        return None, args[0]
    if len(args) == 2:
        return args[0], args[1]
    raise click.ClickException("Expected PATH with an optional leading SANDBOX_ID.")


def _split_transfer_args(
    args: tuple[str, ...],
    *,
    source_name: str,
    destination_name: str,
) -> tuple[str | None, str, str]:
    """Parse ``[SANDBOX_ID] SOURCE DESTINATION`` transfer arguments."""
    if len(args) == 2:
        return None, args[0], args[1]
    if len(args) == 3:
        return args[0], args[1], args[2]
    raise click.ClickException(
        f"Expected [{source_name}] [{destination_name}] with an optional leading SANDBOX_ID."
    )


def _parse_remote_mode(value: str) -> int:
    """Normalize a Unix permission such as 644, 0644, or 0o644 for execd."""
    normalized = value.lower().removeprefix("0o").lstrip("0") or "0"
    if len(normalized) > 4 or any(digit not in "01234567" for digit in normalized):
        raise click.BadParameter("must be an octal permission such as 644, 0755, or 0o600")
    return int(normalized)


def _remote_join(root: str, relative: Path) -> str:
    """Join a trusted local relative path onto a POSIX sandbox path."""
    normalized_root = posixpath.normpath(root)
    if not relative.parts:
        return normalized_root
    return posixpath.join(normalized_root, *relative.parts)


def _local_tree(root: Path) -> tuple[list[Path], list[Path]]:
    """Return relative directories and files, rejecting every symlink."""
    directories: list[Path] = [Path(".")]
    files: list[Path] = []
    for current, dir_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root)
        for name in sorted(dir_names):
            path = current_path / name
            if path.is_symlink():
                raise click.ClickException(f"Symbolic links are not supported: {path}")
            directories.append(relative_dir / name)
        for name in sorted(file_names):
            path = current_path / name
            if path.is_symlink():
                raise click.ClickException(f"Symbolic links are not supported: {path}")
            if not path.is_file():
                raise click.ClickException(f"Unsupported local entry type: {path}")
            files.append(relative_dir / name)
    return directories, files


def _safe_remote_relative(root: str, entry_path: str) -> Path:
    """Map an execd POSIX path below root to a safe local relative path."""
    normalized_root = posixpath.normpath(root)
    normalized_entry = posixpath.normpath(entry_path)
    try:
        common = posixpath.commonpath([normalized_root, normalized_entry])
    except ValueError as exc:
        raise click.ClickException(f"Invalid remote path returned by execd: {entry_path}") from exc
    if common != normalized_root or normalized_entry == normalized_root:
        raise click.ClickException(f"Remote entry escapes source directory: {entry_path}")
    relative = posixpath.relpath(normalized_entry, normalized_root)
    parts = tuple(part for part in relative.split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise click.ClickException(f"Unsafe remote entry path: {entry_path}")
    return Path(*parts)


def _remote_tree(files, root: str) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Breadth-first enumerate a remote tree without following symlinks."""
    directories: list[tuple[str, Path]] = []
    regular_files: list[tuple[str, Path]] = []
    pending = [posixpath.normpath(root)]
    seen = set(pending)
    while pending:
        current = pending.pop(0)
        entries = files.list_directory(DirectoryListEntry(path=current, depth=1))
        for entry in entries:
            entry_path = posixpath.normpath(entry.path)
            if entry_path == current:
                continue
            relative = _safe_remote_relative(root, entry_path)
            if posixpath.dirname(entry_path) != current:
                continue
            entry_type = (entry.entry_type or "").lower()
            if entry_type == "directory":
                if entry_path not in seen:
                    seen.add(entry_path)
                    pending.append(entry_path)
                    directories.append((entry_path, relative))
            elif entry_type == "file":
                regular_files.append((entry_path, relative))
            elif entry_type == "symlink":
                raise click.ClickException(f"Symbolic links are not supported: {entry_path}")
            else:
                raise click.ClickException(
                    f"Unsupported remote entry type '{entry.entry_type}': {entry_path}"
                )
    return directories, regular_files


@file_group.command("upload")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--recursive", "-r", is_flag=True, help="Upload a directory recursively.")
@click.option(
    "--mode",
    type=_parse_remote_mode,
    metavar="MODE",
    default="644",
    show_default=True,
    help="Remote Unix mode (for example: 644, 0644, or 0o644).",
)
@click.option(
    "--directory-mode",
    type=_parse_remote_mode,
    metavar="MODE",
    default="755",
    show_default=True,
    help="Remote directory Unix mode (for example: 755 or 0755).",
)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def file_upload(
    obj: ClientContext,
    args: tuple[str, ...],
    recursive: bool,
    mode: int,
    directory_mode: int,
    output_format: str | None,
) -> None:
    """Upload a local file or recursive directory to the sandbox."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    sandbox_id, local_raw, remote_path = _split_transfer_args(
        args,
        source_name="LOCAL_PATH",
        destination_name="REMOTE_PATH",
    )
    local_path = Path(local_raw)
    if not local_path.exists():
        raise click.ClickException(f"Local file not found: {local_path}")
    if local_path.is_symlink():
        raise click.ClickException(f"Symbolic links are not supported: {local_path}")
    if local_path.is_dir() and not recursive:
        raise click.ClickException(
            f"Local path is a directory: {local_path}. Use --recursive to upload it."
        )
    if recursive and not local_path.is_dir():
        raise click.ClickException(f"Recursive upload requires a directory: {local_path}")
    if not recursive and not local_path.is_file():
        raise click.ClickException(f"Local path is not a regular file: {local_path}")

    resolved_id = obj.resolve_sandbox_id(sandbox_id)
    sandbox = obj.connect_sandbox(resolved_id)
    try:
        if recursive:
            directories, relative_files = _local_tree(local_path)
            sandbox.files.create_directories(
                [
                    WriteEntry(path=_remote_join(remote_path, relative), mode=directory_mode)
                    for relative in directories
                ]
            )
            total = 0
            for relative in relative_files:
                source_path = local_path / relative
                with source_path.open("rb") as source:
                    sandbox.files.write_file(
                        _remote_join(remote_path, relative), source, mode=mode
                    )
                total += source_path.stat().st_size
            file_count = len(relative_files)
            directory_count = len(directories)
        else:
            total = local_path.stat().st_size
            with local_path.open("rb") as source:
                sandbox.files.write_file(remote_path, source, mode=mode)
            file_count = 1
            directory_count = 0
        obj.output.success_panel(
            {
                "sandbox_id": resolved_id,
                "local_path": str(local_path.resolve()),
                "remote_path": remote_path,
                "bytes": total,
                "files": file_count,
                "directories": directory_count,
            },
            title="File Uploaded",
        )
        checkpoint_after_success(obj, resolved_id, "file.upload")
    finally:
        sandbox.close()


@file_group.command("download")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--force", is_flag=True, help="Overwrite an existing local file.")
@click.option("--recursive", "-r", is_flag=True, help="Download a directory recursively.")
@click.option(
    "--chunk-size",
    type=click.IntRange(1024, 16 * 1024 * 1024),
    default=64 * 1024,
    show_default=True,
    help="Download chunk size in bytes.",
)
@output_option("table", "json", "yaml")
@click.pass_obj
@handle_errors
def file_download(
    obj: ClientContext,
    args: tuple[str, ...],
    force: bool,
    recursive: bool,
    chunk_size: int,
    output_format: str | None,
) -> None:
    """Download a remote file or recursive directory to the local filesystem."""
    prepare_output(obj, output_format, allowed=("table", "json", "yaml", "raw"))
    sandbox_id, remote_path, local_raw = _split_transfer_args(
        args,
        source_name="REMOTE_PATH",
        destination_name="LOCAL_PATH",
    )
    local_path = Path(local_raw)
    if local_path.exists() and not force:
        raise click.ClickException(
            f"Local path already exists: {local_path}. Use --force to overwrite it."
        )
    if local_path.is_symlink():
        raise click.ClickException(f"Symbolic links are not supported: {local_path}")
    if not recursive and local_path.exists() and not local_path.is_file():
        raise click.ClickException(f"Local path is not a regular file: {local_path}")
    if recursive and local_path.exists() and not local_path.is_dir():
        raise click.ClickException(f"Recursive download target is not a directory: {local_path}")

    resolved_id = obj.resolve_sandbox_id(sandbox_id)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    sandbox = obj.connect_sandbox(resolved_id)
    temp_path: Path | None = None
    staging_path: Path | None = None
    total = 0
    try:
        if recursive:
            info_map = sandbox.files.get_file_info([remote_path])
            normalized_remote = posixpath.normpath(remote_path)
            info = next(
                (
                    value
                    for path, value in info_map.items()
                    if posixpath.normpath(path) == normalized_remote
                ),
                None,
            )
            if info is None or (info.entry_type or "").lower() != "directory":
                raise click.ClickException(f"Remote path is not a directory: {remote_path}")
            remote_directories, remote_files = _remote_tree(sandbox.files, remote_path)
            staging_path = Path(
                mkdtemp(prefix=f".{local_path.name}.", suffix=".part", dir=local_path.parent)
            )
            for _, relative in remote_directories:
                (staging_path / relative).mkdir(parents=True, exist_ok=True)
            for source_path, relative in remote_files:
                destination_path = staging_path / relative
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                with destination_path.open("wb") as destination:
                    for chunk in sandbox.files.read_bytes_stream(
                        source_path, chunk_size=chunk_size
                    ):
                        destination.write(chunk)
                        total += len(chunk)
            if local_path.exists():
                for staged_directory in sorted(
                    (path for path in staging_path.rglob("*") if path.is_dir()),
                    key=lambda path: len(path.parts),
                ):
                    (local_path / staged_directory.relative_to(staging_path)).mkdir(
                        parents=True, exist_ok=True
                    )
                for staged_file in (path for path in staging_path.rglob("*") if path.is_file()):
                    destination_path = local_path / staged_file.relative_to(staging_path)
                    destination_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged_file, destination_path)
                shutil.rmtree(staging_path)
            else:
                os.replace(staging_path, local_path)
            staging_path = None
            file_count = len(remote_files)
            directory_count = len(remote_directories) + 1
        else:
            with NamedTemporaryFile(
                mode="wb",
                prefix=f".{local_path.name}.",
                suffix=".part",
                dir=local_path.parent,
                delete=False,
            ) as destination:
                temp_path = Path(destination.name)
                for chunk in sandbox.files.read_bytes_stream(
                    remote_path, chunk_size=chunk_size
                ):
                    destination.write(chunk)
                    total += len(chunk)
            os.replace(temp_path, local_path)
            temp_path = None
            file_count = 1
            directory_count = 0
        obj.output.success_panel(
            {
                "sandbox_id": resolved_id,
                "remote_path": remote_path,
                "local_path": str(local_path.resolve()),
                "bytes": total,
                "files": file_count,
                "directories": directory_count,
            },
            title="File Downloaded",
        )
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if staging_path is not None:
            shutil.rmtree(staging_path, ignore_errors=True)
        sandbox.close()
