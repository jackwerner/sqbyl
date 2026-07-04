# Publishing sqbyl to PyPI

A step-by-step guide for cutting a release. Written for a first-time publisher —
if you've done this before, the short version is: *register two Trusted Publishers,
then push a `v*` tag.*

sqbyl publishes **two packages together**: `sqbyl-runtime` (the shippable runtime)
and `sqbyl` (the dev toolkit, which pins the exact runtime). A tag builds both and
uploads them via **Trusted Publishing** — GitHub proves the release's identity to
PyPI with a short-lived token, so there are **no PyPI passwords or API tokens stored
anywhere**. The whole flow lives in [`.github/workflows/release.yml`](.github/workflows/release.yml).

---

## One-time setup

You do this once. Budget ~15 minutes.

### 1. Create a PyPI account

- Sign up at <https://pypi.org/account/register/>.
- **Enable two-factor authentication** (PyPI requires it for publishing). Account
  settings → *Add 2FA with an authenticator app*.

### 2. Register a Trusted Publisher for **each** package

This tells PyPI "GitHub Actions runs from *this* repo, on *this* workflow, are allowed
to publish *this* project." Because the projects don't exist on PyPI yet, you add them
as **pending publishers** — the first successful publish creates the project.

Go to <https://pypi.org/manage/account/publishing/> and add **two** pending publishers,
one per package. For each, fill in exactly:

| Field | Value for `sqbyl-runtime` | Value for `sqbyl` |
|---|---|---|
| PyPI Project Name | `sqbyl-runtime` | `sqbyl` |
| Owner | `jackwerner` | `jackwerner` |
| Repository name | `sqbyl` | `sqbyl` |
| Workflow name | `release.yml` | `release.yml` |
| Environment name | `pypi` | `pypi` |

> The **Environment name** must be `pypi` — it matches the `environment: pypi` in
> `release.yml`. Getting any of these fields wrong is the #1 cause of a failed publish
> (you'll see a "not a trusted publisher" error); double-check them.

### 3. Create the `pypi` GitHub environment

- GitHub → repo **Settings** → **Environments** → **New environment** → name it `pypi`.
- You can optionally add yourself as a **required reviewer**, which makes every publish
  pause for your one-click approval. Recommended for a first release.

That's it for setup. You never touch PyPI credentials again.

---

## Cutting a release

### 4. Pre-flight (once per release)

- **Make sure `main` is green.** The tag should point at a commit whose CI passed
  (lint, types, tests, import boundaries, dep audit, live-Postgres).
- **Smoke-test the real API.** CI never calls Anthropic (it uses mocks), so do this
  once by hand to confirm the live SDK path works — especially after an `anthropic`
  version bump:
  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  export DATABASE_URL=duckdb:///$(pwd)/fixtures/orders.duckdb
  uv run sqbyl ask "how many orders are there?" --budget 1
  ```
  You should get SQL + rows back. (This spends a few cents.)
- **Confirm the versions.** The release version is set in three places and they must
  match the tag. For `v0.1.0` they should all read `0.1.0`:
  - `packages/sqbyl-runtime/pyproject.toml` → `version`
  - `packages/sqbyl/pyproject.toml` → `version` **and** the `sqbyl-runtime==0.1.0` pin
  - the `## [0.1.0]` heading in [`CHANGELOG.md`](CHANGELOG.md)

### 5. Tag and push

```bash
git checkout main && git pull
git tag v0.1.0
git push origin v0.1.0
```

Pushing the tag triggers `release.yml`, which:
1. builds both packages,
2. publishes `sqbyl-runtime` to PyPI, then `sqbyl` (runtime first — the toolkit pins it),
3. creates a GitHub Release for the tag.

If you set a required reviewer on the `pypi` environment, the run pauses at the publish
step until you approve it in the Actions tab.

### 6. Verify

```bash
pip install sqbyl            # pulls sqbyl + sqbyl-runtime
python -c "import sqbyl_runtime, sqbyl; print('ok')"
```

Check the project pages: <https://pypi.org/project/sqbyl/> and
<https://pypi.org/project/sqbyl-runtime/>.

---

## Cutting later releases

PyPI **will not let you re-upload a version that already exists** — every release needs
a new version number. For the next release (say `0.1.1` or `0.2.0`):

1. Bump the version in the three places listed in step 4 (keep both packages equal, and
   update the `sqbyl-runtime==` pin to match).
2. Move the CHANGELOG's `[Unreleased]` notes under a new `## [x.y.z]` heading.
3. Commit, merge to `main`, confirm CI is green.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`.

Use [SemVer](https://semver.org): patch (`0.1.1`) for fixes, minor (`0.2.0`) for
backwards-compatible features. While pre-`1.0`, breaking changes bump the minor.

---

## Troubleshooting

- **"not a trusted publisher" / OIDC error** — a field in the pending publisher doesn't
  match. Re-check owner/repo/workflow/environment against the table in step 2. The
  workflow filename must be exactly `release.yml`, the environment exactly `pypi`.
- **"File already exists"** — you're trying to publish a version that's already on PyPI.
  Bump the version and tag again; you can't overwrite.
- **Only one package published** — the two publish jobs are independent; check that
  *both* pending publishers exist. Re-running the failed job from the Actions tab is safe.
- **Wrong version published** — yank it on PyPI (project → *Manage* → *Yank*), fix the
  version numbers, and cut a new tag. Yanking hides a release from new installs without
  breaking anyone who already pinned it.

For test runs, you can point the publish at <https://test.pypi.org> first (register a
separate Trusted Publisher there and add `repository-url: https://test.pypi.org/legacy/`
to the publish steps) — optional, but a safe way to rehearse.
