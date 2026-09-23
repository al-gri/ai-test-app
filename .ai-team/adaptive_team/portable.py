"""Copy-once, integrity-checked team bundles. No remote writes or code execution.

The caller is a trusted local controller. Hashes establish integrity, not authorship;
importing a bundle never authorizes its Python code or its claimed lesson approvals.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import __version__

SCHEMA_VERSION = 1
MAX_FILES = 4096
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MANIFEST = "manifest.json"
_LESSONS = "memory/portable/lessons/"
_REQUIRED = {"adaptive_team/__init__.py", "adaptive_team/roles.json", "README.md",
             "memory/portable/README.md", "adaptive_team/prompts/chief-architect.md"}
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.I)
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}$")


class PortableError(ValueError):
    """Bundle cannot be copied safely or does not satisfy its declared integrity."""


def _json(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PortableError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PortableError("Invalid UTF-8 JSON") from exc
    if not isinstance(result, dict):
        raise PortableError("Expected a JSON object")
    return result


def _encode(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 240:
        raise PortableError("Invalid bundle member name")
    if name != unicodedata.normalize("NFC", name) or "\\" in name:
        raise PortableError("Non-canonical bundle member name")
    parts = name.split("/")
    for part in parts:
        if (not part or part in {".", ".."} or part[-1:] in {" ", "."}
                or any(ord(c) < 32 or ord(c) == 127 or c in '<>:"|?*' for c in part)
                or _RESERVED.fullmatch(part)):
            raise PortableError("Unsafe cross-platform bundle path")
    if str(PurePosixPath(name)) != name:
        raise PortableError("Non-canonical bundle path")
    return name


def _no_link(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise PortableError("Symlinks and Windows reparse points are not portable")


def _checked_path(value: str | Path, *, directory: bool = True) -> Path:
    path = Path(value).absolute()
    # Do not resolve first: doing so would hide a linked ancestor.
    for item in [*reversed(path.parents), path]:
        if item.exists() or item.is_symlink():
            _no_link(item)
    if directory and not path.is_dir():
        raise PortableError("Repository/output parent must already be a directory")
    return path


def _walk(directory: Path):
    _no_link(directory)
    for base, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            _no_link(Path(base) / name)
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            path = Path(base) / name
            if not path.is_file():
                raise PortableError("Only regular bundle files are allowed")
            yield path


def _read(path: Path) -> bytes:
    _no_link(path)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise PortableError("Non-regular or oversized bundle file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise PortableError("Non-regular bundle file")
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise PortableError("Oversized bundle file")
    _no_link(path)
    return data


def _is_payload(name: str) -> bool:
    if name in {"README.md", "memory/portable/README.md", "adaptive_team/roles.json"}:
        return True
    if name.startswith(_LESSONS):
        return "/" not in name[len(_LESSONS):] and name.endswith(".json")
    if name.startswith("adaptive_team/prompts/"):
        return PurePosixPath(name).suffix in {".md", ".txt", ".json"}
    return name.startswith("adaptive_team/") and (name.endswith(".py") or
        (name.startswith("adaptive_team/storage/") and name.endswith(".sql")) or
        (name.startswith("adaptive_team/control_plane/static/") and PurePosixPath(name).suffix in {'.html','.js','.css'})) and "__pycache__" not in name.split("/")


def _validate_lesson(name: str, data: bytes) -> None:
    lesson = _json(data)
    fields = {"schema_version", "id", "lesson", "evidence_refs", "scope", "sanitized", "approved_by", "approved_at"}
    if set(lesson) != fields or type(lesson["schema_version"]) is not int or lesson["schema_version"] != 1:
        raise PortableError("Portable lesson has an unsupported schema")
    if lesson["scope"] != "portable" or lesson["sanitized"] is not True:
        raise PortableError("Only explicitly sanitized portable lessons can transfer")
    if not isinstance(lesson["id"], str) or not _IDENTIFIER.fullmatch(lesson["id"]):
        raise PortableError("Invalid portable lesson identity")
    if name != _LESSONS + lesson["id"] + ".json":
        raise PortableError("Portable lesson filename/identity mismatch")
    for key, limit in (("lesson", 24000), ("approved_by", 200), ("approved_at", 100)):
        if not isinstance(lesson[key], str) or not lesson[key].strip() or len(lesson[key]) > limit:
            raise PortableError("Invalid portable lesson " + key)
    refs = lesson["evidence_refs"]
    if not isinstance(refs, list) or not 1 <= len(refs) <= 50 or any(
            not isinstance(ref, str) or not ref.strip() or len(ref) > 1024 for ref in refs):
        raise PortableError("Portable lesson requires bounded evidence references")
    try:
        approved_at = datetime.fromisoformat(lesson["approved_at"].replace("Z", "+00:00"))
        if approved_at.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise PortableError("Portable lesson approval timestamp requires ISO-8601 timezone") from exc


def _check_files(files: dict[str, bytes]) -> None:
    if not _REQUIRED <= files.keys() or len(files) > MAX_FILES:
        raise PortableError("Incomplete or oversized team bundle")
    seen = set()
    path_spellings = {}
    total = 0
    for name, data in files.items():
        _safe_name(name)
        if not _is_payload(name) or name.casefold() in seen:
            raise PortableError("Unexpected or case-colliding bundle member")
        seen.add(name.casefold())
        parts = name.split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            if path_spellings.setdefault(prefix.casefold(), prefix) != prefix:
                raise PortableError("Case-colliding directory spelling")
        for length in range(1, len(parts)):
            if "/".join(parts[:length]) in files:
                raise PortableError("Bundle file is also a parent directory")
        if len(data) > MAX_FILE_BYTES:
            raise PortableError("Oversized bundle member")
        total += len(data)
        if name.startswith(_LESSONS):
            _validate_lesson(name, data)
    if total > MAX_TOTAL_BYTES:
        raise PortableError("Bundle exceeds total uncompressed size limit")


def _manifest(files: dict[str, bytes], runtime_version: str) -> dict:
    _check_files(files)
    return {"schema_version": SCHEMA_VERSION, "bundle_type": "adaptive-ai-team-portable",
            "runtime_version": runtime_version,
            "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                      for name, data in sorted(files.items())}}


def _verify(manifest: dict, files: dict[str, bytes]) -> None:
    if (set(manifest) != {"schema_version", "bundle_type", "runtime_version", "files"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION
            or manifest["bundle_type"] != "adaptive-ai-team-portable"
            or not isinstance(manifest["runtime_version"], str)
            or not 1 <= len(manifest["runtime_version"]) <= 100
            or not isinstance(manifest["files"], dict)):
        raise PortableError("Invalid or unsupported bundle manifest")
    _check_files(files)
    if set(manifest["files"]) != set(files):
        raise PortableError("Manifest has missing or extra files")
    for name, data in files.items():
        item = manifest["files"][name]
        expected = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        if not isinstance(item, dict) or item != expected or type(item.get("size")) is not int:
            raise PortableError("Bundle hash/size mismatch: " + name)


_README = """# Repository-resident Adaptive AI Team

This directory is a portable control plane, role catalog and prompt library.
It does not install external coding sandboxes, GitHub credentials or release backends.
No code was executed and no authority was granted by copying this directory.

Read project/context.json and project/RECONNAISSANCE.md before planning. Approve a
new project brief and deployment policy independently. The project starts with no
approved brief, queue, budget, credentials or publication authority. An existing
repository requires read-only reconnaissance before code changes.

Python 3.11+ runtime: install cryptography==50.0.1 for encrypted journals and
httpx==0.28.1 for HTTP protocol adapters. Provision an external AI_TEAM_KEY_FILE
before journal operations and an independently supervised watchdog before Docker
execution; keys, operational state and services are not included in this bundle.
Add this .ai-team directory to PYTHONPATH,
then run `python -m adaptive_team --help` using a trusted interpreter. Review an
imported bundle's provenance before allowing its Python code to run. SHA256 checks
detect payload/manifest inconsistency, not a malicious author replacing both.

Keep project facts in memory/project and credentials outside this bundle. Only
explicitly reviewed, sanitized JSON lessons in memory/portable/lessons transfer.
Their recorded approval is provenance for lesson text, not authority in this project.
The trusted controller owns this directory; candidate agents must not write it.

Export requires the installed release's exact runtime/prompt hashes. Governed
updates require an independently approved release and a fresh reviewed install;
there is no automatic manifest reseal that trusts arbitrary local prompt changes.
Export/import copy the team; they never detach/delete the original or publish Git.
"""
_PORTABLE_README = """# Portable lessons

Only lessons/*.json with the documented version-1 schema transfer. An authorized
reviewer must check that text AND evidence references contain no credentials,
private repository identities, customer data or project-specific instructions.
The exporter validates schema, not the truth of a self-declared approval. Imported
lessons are advisory; they cannot authorize tools or replace local acceptance rules.
"""


def _source_files() -> dict[str, bytes]:
    package = _checked_path(Path(__file__).absolute().parent)
    files = {"README.md": _README.encode(), "memory/portable/README.md": _PORTABLE_README.encode()}
    for path in _walk(package):
        name = "adaptive_team/" + path.relative_to(package).as_posix()
        if _is_payload(name):
            files[name] = _read(path)
    _check_files(files)
    return files


def _context(project_id: str, mode: str) -> dict:
    if not isinstance(project_id, str) or not _IDENTIFIER.fullmatch(project_id):
        raise PortableError("project_id must be a portable identifier of 1-120 characters")
    if mode not in {"greenfield", "brownfield"}:
        raise PortableError("mode must be greenfield or brownfield")
    return {"schema_version": 1, "project_id": project_id, "mode": mode,
            "status": "discovery_required", "approved_brief_digest": None,
            "approvals": [], "queue": [], "budget": None,
            "execution_authorized": False, "publication_authorized": False,
            "reconnaissance_required_before_writes": mode == "brownfield"}


def _reconnaissance(mode: str) -> bytes:
    intro = ("Existing repository: read-only reconnaissance is required before product writes."
             if mode == "brownfield" else "New project: establish these facts before implementation.")
    return ("# Project reconnaissance\n\n" + intro + "\n\n"
            "- [ ] Read repository instructions; identify owner and authorized maintenance route.\n"
            "- [ ] Record exact Git revision, local changes and project boundaries.\n"
            "- [ ] Inventory product behavior, architecture, stack, dependencies and licenses.\n"
            "- [ ] Inspect CI, tests and deployment without executing untrusted repository code.\n"
            "- [ ] Map credentials/production boundaries; do not copy or publish secret values.\n"
            "- [ ] Distinguish verified facts, hypotheses and unavailable evidence.\n"
            "- [ ] Establish user goal, alternatives, market evidence and acceptance measures.\n"
            "- [ ] Present a concise brief/options for owner approval bound to its exact digest.\n"
            "- [ ] Set new budgets, role limits, sandbox/verifier/publisher access and stop rules.\n"
            "- [ ] Validate useful imported lessons locally; do not inherit old project approvals.\n"
            "- [ ] Authorize a bounded first change; preserve unrelated existing work.\n").encode()


def _cleanup_stage(stage: Path, parent: Path) -> None:
    if stage.parent != parent or not stage.name.startswith(".ai-team-stage-"):
        raise PortableError("Refusing cleanup outside owned staging directory")
    if stage.exists() or stage.is_symlink():
        _no_link(stage)
        shutil.rmtree(stage)


def _install(files: dict[str, bytes], manifest: dict, repository: str | Path,
             project_id: str, mode: str) -> dict:
    context = _context(project_id, mode)
    _verify(manifest, files)
    repo = _checked_path(repository)
    target = repo / ".ai-team"
    if target.exists() or target.is_symlink():
        raise PortableError("Refusing to overwrite existing .ai-team")
    # Serialize cooperating installers; the caller must control local repo writes.
    lock = repo / ".ai-team-install.lock"
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PortableError("Another installation or unresolved installation lock exists") from exc
    os.close(lock_fd)
    stage = None
    try:
        if target.exists() or target.is_symlink():
            raise PortableError("Refusing to overwrite existing .ai-team")
        stage = Path(tempfile.mkdtemp(prefix=".ai-team-stage-", dir=repo))
        payload = dict(files)
        payload[MANIFEST] = _encode(manifest)
        payload["project/context.json"] = _encode(context)
        payload["project/RECONNAISSANCE.md"] = _reconnaissance(mode)
        payload["memory/project/README.md"] = b"# Project memory\n\nNew project; no prior project facts or approvals were transferred.\n"
        payload[".gitignore"] = b"state/\n*.db\n*.db-*\n*.sqlite*\n.env*\nkeys/\n__pycache__/\n*.pyc\nmemory/project/\nproject/context.json\n"
        for name, data in sorted(payload.items()):
            output = stage.joinpath(*name.split("/"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        _checked_path(repo)
        if target.exists() or target.is_symlink():
            raise PortableError("Destination appeared during installation")
        os.rename(stage, target)
        stage = None
    finally:
        if stage is not None:
            _cleanup_stage(stage, repo)
        lock.unlink()
    return {"status": "installed", "path": str(target), "project_id": project_id, "mode": mode,
            "file_count": len(files), "runtime_version": manifest["runtime_version"],
            "manifest_sha256": hashlib.sha256(_encode(manifest)).hexdigest(),
            "execution_authorized": False, "external_backends_included": False}


def install_team(repository: str | Path, project_id: str, mode: str = "greenfield") -> dict:
    """Copy the running trusted package into a new .ai-team; leave repo files intact."""
    files = _source_files()
    return _install(files, _manifest(files, __version__), repository, project_id, mode)


def install_evolved_team(evolution, repository: str | Path, project_id: str,
                         attestation: dict, authorities: dict, mode="brownfield") -> dict:
    """Build a fresh release from approved active revisions, with sanitation review.

    Caller authenticates a maintainer's independent export review. No project DB,
    candidate, audit payload, old authority or arbitrary local file is copied.
    """
    from .evolution import verify
    from .models import digest, identifier
    from .prompting import prompt_bundle
    active = evolution.snapshot()["active"]
    _, decision = verify(attestation, authorities, "export_release")
    if decision.get("active_digest") != digest(active) or decision.get("sanitized") is not True or decision.get("decision") != "approve" or not decision.get("evidence_refs"):
        raise PortableError("Exact approved and sanitized active release required")
    files = _source_files()
    roles = []
    for role_id, bundle in active.items():
        identifier(role_id, "role")
        if bundle["role"]["id"] != role_id:
            raise PortableError("Role identity mismatch")
        # New roles do not have a packaged role file; compare shared instructions
        # against any packaged role without interpreting candidate text as code.
        original = _json(files["adaptive_team/roles.json"])["roles"][0]
        common = prompt_bundle(original)
        if any(bundle[key] != common[key] for key in ("contract", "implement", "review")):
            raise PortableError("Shared runtime contract differs: explicit runtime release required")
        roles.append(bundle["role"])
        files[f"adaptive_team/prompts/roles/{role_id}.md"] = bundle["instructions"].encode("utf-8")
    if not roles:
        raise PortableError("Empty active release")
    files["adaptive_team/roles.json"] = _encode({"schema_version": 2, "catalog_version": __version__, "roles": roles})
    # Remove unlisted role prompts from the fresh release, preserving chief and
    # common prompts elsewhere. This only removes entries in an in-memory map.
    files = {name: data for name, data in files.items() if not name.startswith("adaptive_team/prompts/roles/") or name.rsplit("/", 1)[1][:-3] in active}
    _check_files(files)
    return _install(files, _manifest(files, __version__), repository, project_id, mode)


def _installed(repository: str | Path) -> tuple[dict, dict[str, bytes]]:
    root = _checked_path(_checked_path(repository) / ".ai-team")
    manifest = _json(_read(root / MANIFEST))
    if not isinstance(manifest.get("files"), dict):
        raise PortableError("Invalid installed manifest")
    files = {}
    for name in manifest["files"]:
        _safe_name(name)
        if not _is_payload(name):
            raise PortableError("Manifest includes forbidden project/state payload")
        path = root.joinpath(*name.split("/"))
        _checked_path(path, directory=False)
        try:
            files[name] = _read(path)
        except FileNotFoundError as exc:
            raise PortableError("Manifest file missing: " + name) from exc
    _verify(manifest, files)
    # A locally added executable or prompt cannot silently become an approved release.
    for path in _walk(root / "adaptive_team"):
        name = path.relative_to(root).as_posix()
        if _is_payload(name) and name not in files:
            raise PortableError("Unmanifested runtime/prompt file: " + name)
    return manifest, files


def verify_team(repository: str | Path) -> dict:
    """Verify the installed release; return its manifest, without executing it."""
    return _installed(repository)[0]


def export_team(repository: str | Path, archive_path: str | Path) -> dict:
    """Export pinned team files and explicitly approved lessons, never project state."""
    manifest, files = _installed(repository)
    root = _checked_path(_checked_path(repository) / ".ai-team")
    lessons = root / "memory" / "portable" / "lessons"
    if lessons.exists() or lessons.is_symlink():
        _checked_path(lessons)
        for path in _walk(lessons):
            name = path.relative_to(root).as_posix()
            data = _read(path)
            _validate_lesson(name, data)
            files[name] = data
    exported_manifest = _manifest(files, manifest["runtime_version"])
    target = _checked_path(archive_path, directory=False)
    _checked_path(target.parent)
    if target.exists() or target.is_symlink():
        raise PortableError("Refusing to overwrite export archive")
    if target.is_relative_to(root):
        raise PortableError("Export archive must be outside the installed .ai-team")
    descriptor, temporary = tempfile.mkstemp(prefix=".ai-team-export-", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted({**files, MANIFEST: _encode(exported_manifest)}.items()):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        if temporary.stat().st_size > MAX_ARCHIVE_BYTES:
            raise PortableError("Export archive exceeds compressed size limit")
        # Atomic no-overwrite publication of the fully written regular file.
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "exported", "path": str(target), "file_count": len(files),
            "archive_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "project_state_included": False, "external_backends_included": False}


def _archive(archive_path: str | Path) -> tuple[dict, dict[str, bytes]]:
    path = _checked_path(archive_path, directory=False)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PortableError("Missing or oversized bundle archive")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES + 1:
                raise PortableError("Too many archive members")
            seen, files, total = set(), {}, 0
            for info in members:
                name = _safe_name(info.filename)
                mode = info.external_attr >> 16
                if (info.orig_filename != info.filename or name.casefold() in seen
                        or info.is_dir() or info.flag_bits & 1 or info.external_attr & 0x400
                        or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                        or info.file_size > MAX_FILE_BYTES
                        or info.file_size > max(1024 * 1024, info.compress_size * 1000)):
                    raise PortableError("Unsafe, duplicate or oversized archive member")
                seen.add(name.casefold())
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise PortableError("Archive exceeds uncompressed size limit")
                with archive.open(info) as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) != info.file_size or len(data) > MAX_FILE_BYTES:
                    raise PortableError("Archive member size mismatch")
                files[name] = data
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise PortableError("Invalid or unsupported bundle archive") from exc
    if MANIFEST not in files:
        raise PortableError("Archive has no manifest")
    manifest = _json(files.pop(MANIFEST))
    _verify(manifest, files)
    return manifest, files


def import_team(archive_path: str | Path, repository: str | Path,
                project_id: str, mode: str = "brownfield") -> dict:
    """Verify a portable archive and copy it into a fresh, unapproved project context."""
    manifest, files = _archive(archive_path)
    return _install(files, manifest, repository, project_id, mode)
