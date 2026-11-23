"""Streaming encrypted snapshots of stopped local-path PVCs, independent of schemas."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    """Every file/link/directory; memory scales with directory entries, not data size."""
    digest = hashlib.sha256()

    def visit(path):
        info = path.lstat()
        kind = stat.S_IFMT(info.st_mode)
        if kind == stat.S_IFREG:
            content = file_hash(path)
        elif kind == stat.S_IFLNK:
            content = os.readlink(path)
        elif kind == stat.S_IFDIR:
            content = ""
        else:
            raise ValueError("Unsupported special file in persistent volume")
        record = [
            str(path.relative_to(root)),
            kind,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
            content,
        ]
        digest.update(json.dumps(record, ensure_ascii=True).encode() + b"\n")
        if kind == stat.S_IFDIR:
            for child in sorted(path.iterdir()):
                visit(child)

    visit(root)
    return digest.hexdigest()


def pipeline(first: list[str], second: list[str], *, output=None, timeout=1800) -> None:
    """Pipe without buffering the archive in Python or exposing stderr."""
    with subprocess.Popen(
        first, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as left:
        with subprocess.Popen(
            second,
            stdin=left.stdout,
            stdout=output or subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as right:
            left.stdout.close()
            try:
                right_code = right.wait(timeout=timeout)
                left_code = left.wait(timeout=30)
                if right_code or left_code:
                    raise RuntimeError("Backup pipeline failed")
            except BaseException:
                right.kill()
                left.kill()
                raise


class ColdBackup:
    def __init__(self, config: dict):
        self.volumes = {k: Path(v) for k, v in config["volumes"].items()}
        self.cert = config["backup_cert"]
        self.key = config["backup_key"]
        self.max_bytes = config.get("max_backup_bytes", 20 * 1024**3)

    def preflight(self, directory: Path) -> int:
        total = 0
        for path in self.volumes.values():
            if not path.is_dir() or path.is_symlink():
                raise ValueError("Persistent volume missing")
            for folder, dirs, files in os.walk(path, followlinks=False):
                for name in dirs + files:
                    total += (Path(folder) / name).lstat().st_size
        if total > self.max_bytes:
            raise ValueError("Backup size exceeds configured resource budget")
        if shutil.disk_usage(directory).free < total * 3 + 1024**3:
            raise ValueError("Insufficient space for snapshot, rehearsal and recovery")
        for path in (self.cert, self.key):
            if not Path(path).is_file():
                raise ValueError("Backup encryption credential missing")
        return total

    def decrypt(self, directory: Path) -> Path:
        evidence = json.loads((directory / "backup.json").read_text())
        archive = directory / "snapshot.cms"
        if file_hash(archive) != evidence["sha256"]:
            raise ValueError("Backup ciphertext changed")
        target = directory / "readback"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(mode=0o700)
        pipeline(
            [
                "openssl",
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-recip",
                self.cert,
                "-inkey",
                self.key,
                "-in",
                str(archive),
            ],
            ["tar", "-xzpf", "-", "--numeric-owner", "-C", str(target)],
        )
        for alias, source in self.volumes.items():
            if tree_hash(target / source.name) != evidence["trees"][alias]:
                raise ValueError("Backup full-tree verification failed")
        return target

    def create(self, directory: Path) -> Path:
        parents = {p.parent for p in self.volumes.values()}
        if len(parents) != 1 or len({p.name for p in self.volumes.values()}) != len(
            self.volumes
        ):
            raise ValueError("Expected distinct local-path volumes on one storage root")
        evidence = {
            "trees": {alias: tree_hash(path) for alias, path in self.volumes.items()}
        }
        partial = directory / "snapshot.partial"
        with partial.open("wb") as output:
            os.chmod(partial, 0o600)
            pipeline(
                [
                    "tar",
                    "-czpf",
                    "-",
                    "--numeric-owner",
                    "-C",
                    str(next(iter(parents))),
                    *[p.name for p in self.volumes.values()],
                ],
                [
                    "openssl",
                    "cms",
                    "-encrypt",
                    "-binary",
                    "-aes-256-cbc",
                    "-stream",
                    "-outform",
                    "DER",
                    self.cert,
                ],
                output=output,
            )
            output.flush()
            os.fsync(output.fileno())
        evidence["sha256"] = file_hash(partial)
        os.replace(partial, directory / "snapshot.cms")
        atomic_json(directory / "backup.json", evidence)
        return self.decrypt(directory)

    def restore(self, directory: Path) -> None:
        # Always re-read pristine ciphertext; rehearsal has modified its copy of PGDATA.
        source = self.decrypt(directory)
        for original in self.volumes.values():
            staging = original.with_name(original.name + ".restore")
            retired = original.with_name(original.name + ".retired")
            if staging.exists():
                shutil.rmtree(staging)
            # tar retains ownership, symlinks and modes.
            staging.mkdir()
            pipeline(
                [
                    "tar",
                    "-cpf",
                    "-",
                    "--numeric-owner",
                    "-C",
                    str(source / original.name),
                    ".",
                ],
                ["tar", "-xpf", "-", "--numeric-owner", "-C", str(staging)],
            )
            if (
                tree_hash(staging)
                != json.loads((directory / "backup.json").read_text())["trees"][
                    next(
                        alias
                        for alias, path in self.volumes.items()
                        if path == original
                    )
                ]
            ):
                raise ValueError("Restore staging differs from verified snapshot")
            if retired.exists():
                shutil.rmtree(retired)
            if original.exists():
                original.rename(retired)
            staging.rename(original)
            if retired.exists():
                shutil.rmtree(retired)
