# gmdoc

Pull and push Google Docs as Markdown, using the Drive API's built-in
`text/markdown` conversion.

`gmdoc` borrows your access token from `gcloud`, so you don't need a Google Cloud
project, OAuth client, or config file. It talks to the Drive REST API with the
standard library, so its only dependency is `cyclopts`.

## Install

```sh
brew install --cask gcloud-cli
gcloud auth login --enable-gdrive-access   # browser login, click Allow

uv tool install gmdoc
# or: pipx install gmdoc
```

Running the script directly also works — `gmdoc.py` declares its own dependencies
in a PEP 723 header, so `uv run gmdoc.py ...` (or `./gmdoc.py ...`) works anywhere,
even if you copy just that one file.

## Usage

```sh
gmdoc ls [name]                    # list recent docs
gmdoc pull <doc-url|id> [out.md]   # download (default name: <title>.md)
gmdoc pull notes.md                # re-pull an already-linked file
gmdoc push notes.md                # upload edits (replaces the doc body)
gmdoc new draft.md -t "My Doc"     # create a new doc from markdown
gmdoc status [files...]            # in sync / local / remote / CONFLICT
gmdoc account [email] [--login]    # list/check/switch Google accounts
```

Each pulled file gets front matter linking it to the doc:

```yaml
---
gmdoc_id: 1AbC...
gmdoc_title: My Doc
gmdoc_modified: 2026-01-01T12:00:00.000Z
gmdoc_hash: 3f2a...
gmdoc_account: me@gmail.com
---
```

## Multiple accounts

- `gmdoc account` lists your gcloud accounts, marks the active one with `*`, and
  checks that each can reach Drive.
- `gmdoc account other@gmail.com` switches to that account, logging in first if
  needed. Add `--login` to redo the login (fixes "no Drive access").
- Each file remembers the account it was synced with (`gmdoc_account`), and
  `pull`, `push` and `status` use that account whichever one is active.
- New docs and `ls` use the active account. `--account/-a <email>` overrides it
  on any command.

## Safety checks

- `push` refuses if the doc was edited in Google after your last sync. It detects
  this by exporting the doc and comparing it to `gmdoc_hash`, the hash of the text
  at last sync.
- `pull` refuses if the local file has unpushed edits.
- `--force` skips both checks.

## Troubleshooting

If Drive complains that the API isn't enabled for the project, set
`GMDOC_QUOTA_PROJECT=<your-project-id>` (a project with the Drive API enabled).

## Caveats

- A push **replaces the whole doc body**. Comments become detached, and
  suggestions and some formatting (colors, complex tables, images) may be lost.
- After a push the file is re-exported, so it matches Google's version of the
  markdown.
- The `drive` scope is used so it can edit any doc you have access to.

## License

MIT
