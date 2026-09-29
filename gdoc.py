#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "cyclopts>=4.10.0",
# ]
# ///
"""gdoc: pull/push Google Docs as Markdown via the Drive API.

Pull and push Google Docs as Markdown, using the Drive API's built-in
`text/markdown` conversion.

## Setup (once)

```sh
brew install --cask gcloud-cli
gcloud auth login --enable-gdrive-access   # browser login, click Allow
uv tool install ~/p/gdoc
```

Or skip installing: `gdoc.py` lists its own dependencies in the comment block at the
top, so `uv run gdoc.py ...` or `./gdoc.py ...` works anywhere, even if you copy just
this file.

`gdoc` borrows gcloud's access token, so you don't need a Google Cloud project. It talks
to the Drive REST API with the standard library, so its only dependency is `cyclopts`.

If Drive complains that the API isn't enabled for the project, set
`GDOC_QUOTA_PROJECT=<your-project-id>` (a project with the Drive API enabled).

## Usage

```sh
gdoc ls [name]                    # list recent docs
gdoc pull <doc-url|id> [out.md]   # download (default name: <title>.md)
gdoc pull notes.md                # re-pull an already-linked file
gdoc push notes.md                # upload edits (replaces the doc body)
gdoc new draft.md -t "My Doc"     # create a new doc from markdown
gdoc status [files...]            # in sync / local / remote / CONFLICT
gdoc account [email] [--login]    # list/check/switch Google accounts
```

Each pulled file gets front matter linking it to the doc:

```yaml
---
gdoc_id: 1AbC...
gdoc_title: My Doc
gdoc_modified: 2026-01-01T12:00:00.000Z
gdoc_hash: 3f2a...
gdoc_account: me@gmail.com
---
```

## Multiple accounts

- `gdoc account` lists your gcloud accounts, marks the active one with `*`, and checks
  that each can reach Drive.
- `gdoc account other@gmail.com` switches to that account, logging in first if needed.
  Add `--login` to redo the login (fixes "no Drive access").
- Each file remembers the account it was synced with (`gdoc_account`), and `pull`,
  `push` and `status` use that account whichever one is active.
- New docs and `ls` use the active account. `--account/-a <email>` overrides it on
  any command.

## Safety checks

- `push` refuses if the doc was edited in Google after your last sync. It detects this by
  exporting the doc and comparing it to `gdoc_hash`, the hash of the text at last sync.
- `pull` refuses if the local file has unpushed edits.
- `--force` skips both checks.

## Caveats

- A push **replaces the whole doc body**. Comments become detached, and suggestions and
  some formatting (colors, complex tables, images) may be lost.
- After a push the file is re-exported, so it matches Google's version of the markdown.
- The `drive` scope is used so it can edit any doc you have access to.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn
from urllib.parse import urlencode

from cyclopts import App, Parameter

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
MD = "text/markdown"
GDOC = "application/vnd.google-apps.document"
FIELDS = "id,name,mimeType,modifiedTime"

# Name to show in hints: "gdoc", or e.g. "b gdoc" when run as a bhtool subcommand.
_argv0 = Path(sys.argv[0]).name
PROG = f"{_argv0} gdoc" if _argv0 in ("b", "bhtool") else "gdoc"
LOGIN_HINT = PROG + " account {} --login"

FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


# ---------------------------------------------------------------- auth / api


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Drive API error {status}: {message}")
        self.status = status


@dataclass
class Drive:
    token: str
    account: str

    def call(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        raise_errors: bool = False,
    ) -> bytes:
        """Make a Drive REST call. On HTTP errors, exit (or raise HttpError if asked)."""
        headers = {"Authorization": f"Bearer {self.token}"}
        if quota := os.environ.get("GDOC_QUOTA_PROJECT"):
            headers["x-goog-user-project"] = quota
        if content_type:
            headers["Content-Type"] = content_type
        if params:
            url += "?" + urlencode(params)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                message = json.loads(raw)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = raw.strip() or str(e.reason)
            err = HttpError(e.code, message)
            if raise_errors:
                raise err from None
            die(str(err))
        except urllib.error.URLError as e:
            die(f"can't reach Google: {e.reason}")

    def call_json(self, method: str, url: str, **kw) -> dict:
        return json.loads(self.call(method, url, **kw))

    def meta(self, doc_id: str) -> dict:
        try:
            info = self.call_json(
                "GET",
                f"{API}/files/{doc_id}",
                params={"fields": FIELDS, "supportsAllDrives": "true"},
                raise_errors=True,
            )
        except HttpError as e:
            if e.status == 404:
                die(
                    f"doc {doc_id} not found, or not shared with {self.account}.",
                    "Try another account with --account <email>. To list your accounts:",
                    f"  {PROG} account",
                )
            die(str(e))
        if info["mimeType"] != GDOC:
            die(f"{info['name']} is not a Google Doc ({info['mimeType']})")
        return info

    def export(self, doc_id: str) -> str:
        data = self.call("GET", f"{API}/files/{doc_id}/export", params={"mimeType": MD})
        return data.decode("utf-8")

    def update(self, doc_id: str, text: str) -> dict:
        """Replace the doc body with markdown (Drive converts it)."""
        return self.call_json(
            "PATCH",
            f"{UPLOAD}/files/{doc_id}",
            params={"uploadType": "media", "fields": FIELDS, "supportsAllDrives": "true"},
            data=text.encode("utf-8"),
            content_type=MD,
        )

    def create(self, meta: dict, text: str) -> dict:
        """Create a file from metadata + markdown content (multipart upload)."""
        boundary = "gdoc-" + secrets.token_hex(16)
        data = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: {MD}; charset=UTF-8\r\n\r\n"
            f"{text}\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        return self.call_json(
            "POST",
            f"{UPLOAD}/files",
            params={"uploadType": "multipart", "fields": FIELDS, "supportsAllDrives": "true"},
            data=data,
            content_type=f"multipart/related; boundary={boundary}",
        )

    def files(self, **params) -> list[dict]:
        return self.call_json("GET", f"{API}/files", params=params).get("files", [])


def connect(account: str | None = None) -> Drive:
    """Drive client for `account` (default: gcloud's active account)."""
    account = account or active_account()
    return Drive(gcloud_token(account), account)


class Connections(dict):
    """One Drive client per account, created on demand."""

    def __missing__(self, account: str | None) -> Drive:  # None = active account
        self[account] = connect(account)
        return self[account]


GCLOUD_LOCATIONS = [  # common installs that may not be on PATH
    "/opt/homebrew/share/google-cloud-sdk/bin/gcloud",
    "/usr/local/share/google-cloud-sdk/bin/gcloud",
    "~/google-cloud-sdk/bin/gcloud",
    "/usr/lib/google-cloud-sdk/bin/gcloud",
]


@functools.cache
def gcloud_bin() -> str:
    """Path to gcloud, or exit with install instructions."""
    found = shutil.which("gcloud")
    if found:
        return found
    for loc in GCLOUD_LOCATIONS:
        p = Path(loc).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    die(
        "gcloud is not installed. gdoc uses it to log in to Google.",
        "Install it:",
        "  brew install --cask gcloud-cli",
        "  (other systems: https://cloud.google.com/sdk/docs/install)",
        "Then log in:",
        "  gcloud auth login --enable-gdrive-access",
    )


def gcloud(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([gcloud_bin(), *args], capture_output=capture, text=True)


def gcloud_accounts() -> list[dict]:
    r = gcloud("auth", "list", "--format=json")
    return json.loads(r.stdout or "[]") if r.returncode == 0 else []


def active_account() -> str:
    for a in gcloud_accounts():
        if a.get("status") == "ACTIVE":
            return a["account"]
    die("no active gcloud account.", "Log in / pick one:", f"  {PROG} account <email>")


def gcloud_token(account: str) -> str:
    """Borrow an access token from `gcloud auth login --enable-gdrive-access`."""
    r = gcloud("auth", "print-access-token", account)
    if r.returncode != 0:
        die(
            f"{account} is not logged in to gcloud.",
            "Log in:",
            f"  {LOGIN_HINT.format(account)}",
        )
    return r.stdout.strip()


# ------------------------------------------------------------ front matter


def read(path: Path) -> tuple[dict, str]:
    if not path.is_file():
        die(f"no such file: {path}")
    return parse(path.read_text(encoding="utf-8"))


def parse(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    if "gdoc_id" not in fm:  # someone else's front matter; leave it in the body
        return {}, text
    return fm, text[m.end():]


def render(fm: dict, body: str) -> str:
    head = "\n".join(f"{k}: {v}" for k, v in fm.items())
    return f"---\n{head}\n---\n\n{body.strip(chr(10))}\n"


def digest(body: str) -> str:
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]


def write_synced(path: Path, drive: Drive, info: dict, body: str) -> None:
    fm = {
        "gdoc_id": info["id"],
        "gdoc_title": info["name"],
        "gdoc_modified": info["modifiedTime"],
        "gdoc_hash": digest(body),
    }
    if drive.account:
        fm["gdoc_account"] = drive.account
    path.write_text(render(fm, body), encoding="utf-8")


# ------------------------------------------------------------------ helpers


def die(msg: str, *hints: str) -> NoReturn:
    """Print an error and exit. Each hint goes on its own indented line after a blank line."""
    text = f"{PROG}: {msg}"
    if hints:
        text += "\n\n" + "\n".join(f"  {h}" if h else "" for h in hints)
    print(text, file=sys.stderr)
    sys.exit(1)


ID_RE = re.compile(r"[A-Za-z0-9_-]{20,}")


def doc_id_from(s: str) -> str:
    """Extract a Drive file id from an id or any Docs/Drive URL.

    Handles .../d/<id>/edit?tab=t.0#heading=..., /u/1/d/<id>, /view, /copy,
    open?id=<id>, uc?id=<id>&export=..., and ids with stray ?/#/quotes.
    """
    from urllib.parse import parse_qs, urlsplit

    s = s.strip().strip("'\"<>")
    url = urlsplit(s)
    parts = [p for p in url.path.split("/") if p]
    if "d" in parts[:-1]:  # .../d/<id>/...
        cand = parts[parts.index("d") + 1]
    else:
        query = parse_qs(url.query) or parse_qs(url.fragment.lstrip("?"))
        # ?id=<id>, else the last path segment (folders/<id>, or a bare id)
        cand = (query.get("id") or [""])[0] or (parts[-1] if parts else "")
    m = ID_RE.search(cand)
    if not m:
        die(f"can't find a doc id in {s!r}")
    return m.group(0)


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "untitled"


def looks_like_file(s: str) -> bool:
    return s.endswith(".md") or Path(s).exists()


def drive_ok(drive: Drive) -> str:
    """Return '' if the account can use Drive, else a short reason."""
    try:
        drive.call("GET", f"{API}/files", params={"pageSize": 1, "fields": "files(id)"}, raise_errors=True)
        return ""
    except HttpError as e:
        return "no Drive access" if e.status in (401, 403) else f"error {e.status}"


# ----------------------------------------------------------------- commands

Force = Annotated[bool, Parameter(name=["--force", "-f"], negative="")]
Account = Annotated[str | None, Parameter(name=["--account", "-a"])]

app = App(name="gdoc", help="Pull/push Google Docs as Markdown.", version_flags=[])


@app.command
def pull(
    target: str, out: Path | None = None, *, account: Account = None, force: Force = False
) -> None:
    """Download a doc as markdown.

    Parameters
    ----------
    target
        Doc id, doc URL, or an already-linked .md file.
    out
        Output file (default: the doc title, slugified, + .md).
    account
        Google account (default: the file's gdoc_account, else gcloud's active account).
    force
        Overwrite local changes.
    """
    path = None
    if looks_like_file(target):
        fm, _ = read(Path(target))
        if "gdoc_id" not in fm:
            die(f"{target} has no gdoc_id front matter.", "Pass a doc id or URL instead.")
        doc_id = fm["gdoc_id"]
        account = account or fm.get("gdoc_account")
        path = out or Path(target)
    else:
        doc_id = doc_id_from(target)

    drive = connect(account)
    info = drive.meta(doc_id)
    path = path or out or Path(f"{slug(info['name'])}.md")

    if path.exists() and not force:
        fm, body = read(path)
        if fm.get("gdoc_id") and fm["gdoc_id"] != doc_id:
            die(
                f"{path} is linked to a different doc ({fm['gdoc_id']}).",
                "Use --force to overwrite it.",
            )
        if not fm or fm.get("gdoc_hash") != digest(body):
            die(
                f"{path} has local changes that would be overwritten.",
                "Push them first, or use --force to discard them.",
            )

    write_synced(path, drive, info, drive.export(doc_id))
    print(f"pulled '{info['name']}' -> {path}")


@app.command
def push(
    file: Path, doc: str | None = None, *, account: Account = None, force: Force = False
) -> None:
    """Replace a doc's content with a markdown file.

    Parameters
    ----------
    file
        Linked .md file.
    doc
        Doc id/URL (default: from front matter).
    account
        Google account (default: the file's gdoc_account, else gcloud's active account).
    force
        Overwrite remote changes.
    """
    fm, body = read(file)
    doc_id = doc_id_from(doc) if doc else fm.get("gdoc_id")
    if not doc_id:
        die(
            f"{file} has no gdoc_id front matter.",
            "Pass a doc id or URL, or create a new doc:",
            f"  {PROG} new {file}",
        )

    drive = connect(account or fm.get("gdoc_account"))
    info = drive.meta(doc_id)
    if not force:
        if fm.get("gdoc_id") != doc_id:
            die(
                f"{file} was not pulled from this doc.",
                "Pushing replaces the doc's whole body. Use --force to do it anyway.",
            )
        if fm.get("gdoc_hash") == digest(body):
            print("no local changes; nothing to push")
            return
        if digest(drive.export(doc_id)) != fm.get("gdoc_hash"):
            die(
                f"'{info['name']}' was edited in Google Docs since your last sync.",
                "Save your edits elsewhere and pull, or use --force to overwrite.",
            )

    info = drive.update(doc_id, body)
    # Re-export so the local file matches Google's normalized markdown.
    write_synced(file, drive, info, drive.export(doc_id))
    print(f"pushed {file} -> '{info['name']}'")


@app.command
def new(
    file: Path,
    *,
    title: Annotated[str | None, Parameter(name=["--title", "-t"])] = None,
    folder: str | None = None,
    account: Account = None,
    force: Force = False,
) -> None:
    """Create a new doc from a markdown file.

    Parameters
    ----------
    file
        Markdown file to upload.
    title
        Doc title (default: file name).
    folder
        Drive folder id/URL.
    account
        Google account that will own the doc (default: gcloud's active account).
    force
        Create even if the file is already linked to a doc.
    """
    fm, body = read(file)
    if fm.get("gdoc_id") and not force:
        die(
            f"{file} is already linked to {fm['gdoc_id']}.",
            f"Use `{PROG} push`, or --force to create another doc.",
        )
    body_meta: dict[str, object] = {"name": title or file.stem, "mimeType": GDOC}
    if folder:
        body_meta["parents"] = [doc_id_from(folder)]
    drive = connect(account)
    info = drive.create(body_meta, body)
    write_synced(file, drive, info, drive.export(info["id"]))
    print(f"created '{info['name']}' https://docs.google.com/document/d/{info['id']}/edit")


@app.command
def status(*files: Path) -> None:
    """Show sync state (and account) of linked .md files.

    Parameters
    ----------
    files
        Files to check (default: *.md in the current directory).
    """
    drives = Connections()
    for path in files or sorted(Path(".").glob("*.md")):
        fm, body = read(path)
        if "gdoc_id" not in fm:
            continue
        drive = drives[fm.get("gdoc_account")]
        local = fm.get("gdoc_hash") != digest(body)
        remote = fm.get("gdoc_hash") != digest(drive.export(fm["gdoc_id"]))
        state = {
            (False, False): "in sync",
            (True, False): "local changes (push)",
            (False, True): "remote changes (pull)",
            (True, True): "CONFLICT (both changed)",
        }[(local, remote)]
        who = f"  [{drive.account}]" if drive.account else ""
        print(f"{path}: {state}{who}")


@app.command
def ls(
    query: str | None = None,
    *,
    limit: Annotated[int, Parameter(name=["--limit", "-n"])] = 20,
    account: Account = None,
) -> None:
    """List recent Google Docs.

    Parameters
    ----------
    query
        Filter by name.
    limit
        Max docs to show.
    account
        Google account (default: gcloud's active account).
    """
    q = f"mimeType='{GDOC}' and trashed=false"
    if query:
        q += " and name contains '{}'".format(query.replace("'", "\\'"))
    files = connect(account).files(
        q=q,
        orderBy="modifiedTime desc",
        pageSize=limit,
        fields="files(id,name,modifiedTime)",
        includeItemsFromAllDrives="true",
        supportsAllDrives="true",
    )
    for f in files:
        print(f"{f['id']}  {f['modifiedTime'][:10]}  {f['name']}")


@app.command
def account(
    email: str | None = None,
    *,
    login: Annotated[bool, Parameter(negative="")] = False,
) -> None:
    """List Google accounts and their Drive access, or switch the active one.

    With EMAIL: switch gcloud's active account to it, logging in first
    (with Drive access) if it isn't logged in yet.

    Parameters
    ----------
    email
        Account to switch to.
    login
        Force a fresh browser login with Drive access (fixes "no Drive access").
    """
    if email:
        known = {a["account"] for a in gcloud_accounts()}
        if login or email not in known:
            # Interactive: opens a browser, so don't capture output.
            r = gcloud("auth", "login", email, "--enable-gdrive-access", capture=False)
            if r.returncode != 0:
                die(f"login failed for {email}.")
        r = gcloud("config", "set", "account", email)
        if r.returncode != 0:
            die(r.stderr.strip())
        print(f"switched to {email}")
    elif login:
        die("--login needs an EMAIL")

    accounts = gcloud_accounts()
    if not accounts:
        die("no gcloud accounts.", "Log in:", f"  {PROG} account <email>")
    width = max(len(a["account"]) for a in accounts)
    broken = []
    for a in accounts:
        mark = "*" if a.get("status") == "ACTIVE" else " "
        problem = drive_ok(connect(a["account"]))
        if problem:
            broken.append(a["account"])
        print(f"{mark} {a['account']:<{width}}  {problem or 'Drive ok'}")
    if broken:
        print("\nTo fix, log in again:")
        for acct in broken:
            print(f"  {LOGIN_HINT.format(acct)}")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
