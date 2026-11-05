"""Git snapshots and reviewed patch integration without changing the user index."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess


class WorkerError(Exception):
    """An actionable execution or workspace error."""


def git(root, *args, env=None, data=None, check=True):
    process_env = os.environ.copy()
    # Do not inherit another command's alternate index or Git directory.
    for key in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        process_env.pop(key, None)
    if env:
        process_env.update(env)
    # check-ignore already takes literal paths and rejects Git's pathspec magic.
    literal = [] if args[0] == "check-ignore" else ["--literal-pathspecs"]
    result = subprocess.run(
        ["git", *literal, "-c", "core.hooksPath=/dev/null", *args],
        cwd=str(root), env=process_env, input=data, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise WorkerError("Git command failed: " + " ".join(args[:2]))
    return result


def split_paths(data):
    return [os.fsdecode(item) for item in data.split(b"\0") if item]


def repository(root):
    result = git(root, "rev-parse", "--show-toplevel", check=False)
    if result.returncode:
        return None
    return Path(os.fsdecode(result.stdout).strip()).resolve()


def head_exists(root):
    return git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def tracked_paths(root):
    names = set(split_paths(git(root, "ls-files", "-z", "--cached").stdout))
    if head_exists(root):
        names.update(split_paths(git(root, "ls-tree", "-rz", "--name-only", "HEAD").stdout))
    return names


def untracked_paths(root):
    return set(split_paths(git(root, "ls-files", "-z", "--others", "--exclude-standard").stdout))


def normalize_path(value):
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise WorkerError("Task paths must be nonempty, repository-relative POSIX paths.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts or str(path) == ".":
        raise WorkerError("Task paths cannot escape the workspace or include .git.")
    if str(path) != value:
        raise WorkerError("Use normalized task paths without ./ or trailing slashes.")
    return value


def validate_paths(root, paths, tracked=None):
    tracked = tracked if tracked is not None else set()
    is_git = repository(root) is not None
    for name in paths:
        normalize_path(name)
        path = root / name
        if path.resolve() != path.absolute():
            raise WorkerError("Declared paths cannot traverse symlinks: " + name)
        if path.exists() and not path.is_file():
            raise WorkerError("Declare individual files, not directories: " + name)
        if is_git and name not in tracked:
            ignored = git(root, "check-ignore", "-q", "--", name, check=False).returncode
            if ignored not in (0, 1):
                raise WorkerError("Cannot evaluate ignore rules for: " + name)
            if ignored == 0:
                raise WorkerError("Ignored files are not task inputs or outputs: " + name)


def fingerprint(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        content = os.fsencode(os.readlink(path))
        mode = "120000"
    elif stat.S_ISREG(info.st_mode):
        content = path.read_bytes()
        mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
    elif stat.S_ISDIR(info.st_mode):
        raise WorkerError("Submodules and directory outputs require an ordinary serial workflow.")
    else:
        raise WorkerError("Unsupported workspace file type: " + str(path))
    return mode + ":" + hashlib.sha256(content).hexdigest()


def scan(root, is_git):
    if is_git:
        names = tracked_paths(root) | untracked_paths(root)
    else:
        names = set()
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name != ".git"]
            for name in files + [name for name in dirs if (Path(base) / name).is_symlink()]:
                names.add((Path(base) / name).relative_to(root).as_posix())
    return {name: fingerprint(root / name) for name in sorted(names)}


def changed_paths(before, after):
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def snapshot(root, destination, extra_files, all_untracked=False):
    """Write a private tree/commit using an alternate index; never update refs."""
    index = destination / "snapshot.index"
    env = {"GIT_INDEX_FILE": str(index)}
    git(root, "read-tree", "HEAD" if head_exists(root) else "--empty", env=env)
    git(root, "add", "-u", env=env)
    names = tracked_paths(root) | set(extra_files)
    if all_untracked:
        names |= untracked_paths(root)
    existing = [name for name in sorted(names) if (root / name).exists() or (root / name).is_symlink()]
    if existing:
        data = b"".join(os.fsencode(name) + b"\0" for name in existing)
        git(root, "add", "--force", "--pathspec-from-file=-", "--pathspec-file-nul", env=env, data=data)
    tree = git(root, "write-tree", env=env).stdout.strip().decode("ascii")
    env.update({
        "GIT_AUTHOR_NAME": "CC Worker Snapshot", "GIT_AUTHOR_EMAIL": "snapshot@localhost",
        "GIT_COMMITTER_NAME": "CC Worker Snapshot", "GIT_COMMITTER_EMAIL": "snapshot@localhost",
    })
    parents = ["-p", "HEAD"] if head_exists(root) else []
    commit = git(root, "commit-tree", tree, *parents, env=env, data=b"Temporary CC Worker snapshot\n")
    index.unlink(missing_ok=True)
    return commit.stdout.strip().decode("ascii")


def create_worktree(source, path, commit):
    git(source, "worktree", "add", "--detach", str(path), commit)


def export_patch(root, run_dir, baseline, before, declared):
    # Include new observable files so scope violations cannot disappear from review.
    new_files = untracked_paths(root) - set(before)
    index = run_dir / "export.index"
    env = {"GIT_INDEX_FILE": str(index)}
    git(root, "read-tree", baseline, env=env)
    git(root, "add", "-u", env=env)
    names = new_files | set(declared)
    existing = [name for name in sorted(names) if (root / name).exists() or (root / name).is_symlink()]
    if existing:
        git(root, "add", "--force", "--pathspec-from-file=-", "--pathspec-file-nul", env=env,
            data=b"".join(os.fsencode(name) + b"\0" for name in existing))
    patch = git(root, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
                "--no-textconv", baseline, "--", env=env).stdout
    index.unlink(missing_ok=True)
    path = run_dir / "changes.patch"
    path.write_bytes(patch)
    return hashlib.sha256(patch).hexdigest()


def apply_patch(state, run_dir):
    if not state["isolated"]:
        raise WorkerError("This task ran in place; review its changes directly. No apply is needed.")
    if state["status"] == "applied":
        return
    if state["status"] != "ready_for_review" or state.get("cleaned"):
        raise WorkerError("Only a retained, successful task can be applied after review.")
    source = Path(state["source"])
    worktree = Path(state["workdir"])
    names = set(state["changed_files"]) | set(state["task"]["read_files"])
    validate_paths(source, names, tracked_paths(source))
    for name in names:
        if fingerprint(source / name) != state["source_before"].get(name):
            raise WorkerError("Source changed since dispatch; review and reconcile: " + name)
    current = scan(worktree, True)
    if current != state["after"]:
        raise WorkerError("Worker files changed after validation; do not apply stale results.")
    patch = (run_dir / "changes.patch").read_bytes()
    if hashlib.sha256(patch).hexdigest() != state["patch_sha256"]:
        raise WorkerError("The reviewed patch was modified after validation.")
    if patch:
        result = git(source, "apply", "--check", "--binary", "-", data=patch, check=False)
        if result.returncode:
            raise WorkerError("Patch preflight failed; source files and index were left unchanged.")
        git(source, "apply", "--binary", "-", data=patch)
    state["status"] = "applied"


def remove_worktree(state, run_dir):
    if state.get("cleaned"):
        return
    if not state["isolated"]:
        state["cleaned"] = True
        return
    expected = run_dir / "worktree"
    if Path(state["workdir"]) != expected or expected.is_symlink():
        raise WorkerError("Refusing to remove a directory not owned by this run.")
    git(Path(state["source"]), "worktree", "remove", "--force", str(expected))
    state["cleaned"] = True
