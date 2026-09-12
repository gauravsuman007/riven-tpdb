"""Installing add-ons from a git repository.

BE CLEAR ABOUT WHAT THIS IS. An add-on runs in the host's process with the
host's database and the host's credentials, so installing one is running
someone else's code as this application. There is no sandbox and this module
does not pretend to be one -- the endpoint is behind the API key, and that is
the whole of the trust model. Install add-ons you would give a shell to.

What this module *can* do is refuse the accidents: a URL that is not a
repository, a repository that contains no add-on, a manifest whose key would
escape the add-ons directory or collide with something already installed. All
of those are checked in a temporary clone, before anything is put where the
loader would find it -- so a bad install leaves nothing behind to explain.
"""

import base64
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from loguru import logger


#: Long enough for a cold clone of a repository with artwork in it, short
#: enough that a URL that merely hangs fails the request rather than the
#: worker.
TIMEOUT = 180

#: Deliberately narrow. The key becomes a Postgres schema name, a URL segment
#: and a settings key, and the schema name is interpolated into SQL.
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,38}$")

_ALLOWED_SCHEMES = ("https://", "http://", "git@", "ssh://")


class InstallError(Exception):
    """Anything that should be shown to the user as a reason, not a traceback."""


def _auth_args(token: str | None) -> list[str]:
    """Per-invocation credentials for a private repository.

    Passed as `-c http.extraHeader`, never baked into the remote URL. A token
    in the URL is written into `.git/config` by clone, which puts it on disk in
    the add-ons volume AND makes it come back out of `remote get-url` -- which
    the loader reports to the management API, so the token would be rendered
    on the settings page for anyone who could see it.
    """

    if not token:
        return []

    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]


def _redact(message: str, token: str | None) -> str:
    return message.replace(token, "***") if token else message


def _git(*args: str, cwd: Path | None = None, token: str | None = None) -> str:
    result = subprocess.run(
        ["git", *_auth_args(token), *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        cwd=str(cwd) if cwd else None,
        # Inherited, plus the one variable that matters: without it a
        # repository needing credentials blocks on a prompt nobody can answer
        # and the request hangs until the timeout instead of failing.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )

    if result.returncode != 0:
        # stderr, trimmed: git is wordy and the last line is nearly always the
        # one that says what went wrong.
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise InstallError(_redact(detail[-1] if detail else "git failed", token))

    return result.stdout.strip()


def _read_key(path: Path) -> str:
    """The add-on's key, read without importing it.

    Deliberately a text scan rather than an import: this runs on a clone of a
    repository the user has just named, and importing it to find out whether
    it is safe to install has the order of those two things backwards. The
    loader imports it later, once it is installed and the user has said so.
    """

    entry = path / "riven_addon.py"

    if not entry.exists():
        raise InstallError("That repository has no riven_addon.py at its root")

    sources = [entry]
    backend = path / "backend"

    if backend.is_dir():
        sources.extend(sorted(backend.glob("*.py")))

    for source in sources:
        match = re.search(
            r"""\bkey\s*=\s*["']([a-z][a-z0-9_]*)["']""",
            source.read_text("utf-8", "ignore"),
        )

        if match:
            return match.group(1)

    raise InstallError("Could not find the add-on's key in its manifest")


def install(
    url: str, directory: Path, *, ref: str | None = None, token: str | None = None
) -> str:
    """Clone an add-on into the add-ons directory. Returns its key."""

    url = (url or "").strip()

    if not url.startswith(_ALLOWED_SCHEMES):
        raise InstallError("Give an https:// or git@ repository URL")

    directory.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(directory)) as staging:
        checkout = Path(staging) / "clone"

        # Shallow: add-on history is of no interest to the host, and a full
        # clone of a repository with artwork in it is the difference between
        # seconds and minutes.
        args = ["clone", "--depth", "1", "--single-branch"]

        if ref:
            args += ["--branch", ref]

        logger.info(f"Addons: cloning {url}")
        _git(*args, url, str(checkout), token=token)

        key = _read_key(checkout)

        if not _KEY_RE.match(key):
            raise InstallError(
                f"{key!r} is not a usable add-on key: lowercase letters, "
                "digits and underscores, starting with a letter"
            )

        target = directory / key

        if target.exists():
            raise InstallError(
                f"{key} is already installed. Update it instead, or remove it first."
            )

        # Moved into place only once everything about it has checked out, so a
        # rejected install never leaves a folder the loader would try to run.
        shutil.move(str(checkout), str(target))

    logger.success(f"Addons: installed {key} from {url}")
    return key


def update(path: Path, *, token: str | None = None) -> str:
    """Fast-forward an installed add-on to its remote's current head."""

    if not (path / ".git").exists():
        raise InstallError("That add-on was not installed from git")

    _git("fetch", "--depth", "1", "origin", cwd=path, token=token)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)

    # Hard reset rather than pull: the working tree is not somewhere anyone
    # edits, and a merge conflict in a folder with no one to resolve it would
    # leave the add-on unloadable with no way to say why.
    _git("reset", "--hard", f"origin/{branch}", cwd=path)

    revision = _git("rev-parse", "--short", "HEAD", cwd=path)
    logger.success(f"Addons: updated {path.name} to {revision}")
    return revision


def uninstall(path: Path) -> None:
    """Delete the add-on's folder. Its data is `database.purge`'s business."""

    if not path.exists():
        return

    shutil.rmtree(path)
    logger.warning(f"Addons: removed {path.name}")
