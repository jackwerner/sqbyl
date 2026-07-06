# Publishing sqbyl to PyPI

How to cut a release. sqbyl publishes **two packages together**: `sqbyl-runtime` (the
shippable runtime) and `sqbyl` (the dev toolkit, which pins the exact runtime). Pushing a
`v*` tag builds both and uploads them via **Trusted Publishing** — GitHub proves the
release's identity to PyPI with a short-lived OIDC token, so there are **no PyPI passwords
or API tokens stored anywhere**. The whole flow lives in
[`.github/workflows/release.yml`](.github/workflows/release.yml).

> **Trusted Publishing is already configured** — a Trusted Publisher is registered on PyPI
> for each package, and the matching GitHub environments (`pypi` for `sqbyl`, `pypi-runtime`
> for `sqbyl-runtime`) exist. You never touch PyPI credentials. If you ever need to
> reconstruct it, the publisher fields are `owner: jackwerner`, `repo: sqbyl`,
> `workflow: release.yml`, and those two distinct environment names (they must differ — PyPI
> won't let two projects share one owner/repo/workflow/environment tuple).

---

## Cutting a release

### 1. Pre-flight

- **Make sure `main` is green.** The tag should point at a commit whose CI passed
  (lint, types, tests, import boundaries, dep audit, live-Postgres).
- **Smoke-test the real API.** CI never calls a provider (it uses mocks), so do this once by
  hand to confirm the live SDK path works — especially after a provider SDK version bump:
  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...          # or OPENAI_API_KEY for an OpenAI project
  export DATABASE_URL=duckdb:///$(pwd)/fixtures/orders.duckdb
  uv run sqbyl ask "how many orders are there?" --budget 1
  ```
  You should get SQL + rows back. (This spends a few cents.)
- **Confirm the versions.** The release version is set in three places and they must
  match the tag. For `v0.1.0` they should all read `0.1.0`:
  - `packages/sqbyl-runtime/pyproject.toml` → `version`
  - `packages/sqbyl/pyproject.toml` → `version` **and** the `sqbyl-runtime==0.1.0` pin
  - the `## [0.1.0]` heading in [`CHANGELOG.md`](CHANGELOG.md)

### 2. Tag and push

```bash
git checkout main && git pull
git tag v0.1.0
git push origin v0.1.0
```

Pushing the tag triggers `release.yml`, which:
1. builds both packages,
2. publishes `sqbyl-runtime` to PyPI, then `sqbyl` (runtime first — the toolkit pins it),
3. creates a GitHub Release for the tag.

If you set a required reviewer on the `pypi` / `pypi-runtime` environments, the run pauses at
each publish step until you approve it in the Actions tab.

### 3. Verify

```bash
pip install sqbyl            # pulls sqbyl + sqbyl-runtime
python -c "import sqbyl_runtime, sqbyl; print('ok')"
```

Check the project pages: <https://pypi.org/project/sqbyl/> and
<https://pypi.org/project/sqbyl-runtime/>.

---

## Later releases

PyPI **will not let you re-upload a version that already exists** — every release needs
a new version number. For the next release (say `0.1.1` or `0.2.0`):

1. Bump the version in the three places listed in pre-flight (keep both packages equal, and
   update the `sqbyl-runtime==` pin to match).
2. Move the CHANGELOG's `[Unreleased]` notes under a new `## [x.y.z]` heading.
3. Commit, merge to `main`, confirm CI is green.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`.

Use [SemVer](https://semver.org): patch (`0.1.1`) for fixes, minor (`0.2.0`) for
backwards-compatible features. While pre-`1.0`, breaking changes bump the minor.

---

## Troubleshooting

- **"not a trusted publisher" / OIDC error** — a field on the Trusted Publisher doesn't
  match the workflow. The workflow filename must be exactly `release.yml`; the environment
  must be `pypi-runtime` for the runtime and `pypi` for the toolkit.
- **"File already exists"** — you're trying to publish a version that's already on PyPI.
  Bump the version and tag again; you can't overwrite.
- **Only one package published** — the two publish jobs are independent. Re-running the
  failed job from the Actions tab is safe.
- **Wrong version published** — yank it on PyPI (project → *Manage* → *Yank*), fix the
  version numbers, and cut a new tag. Yanking hides a release from new installs without
  breaking anyone who already pinned it.

To rehearse against <https://test.pypi.org> first, register a separate Trusted Publisher
there and add `repository-url: https://test.pypi.org/legacy/` to the publish steps.
