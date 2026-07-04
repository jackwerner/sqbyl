# sqbyl — post-implementation enhancements

Phases 0–9 of [`sqbyl-implementation-plan.md`](sqbyl-implementation-plan.md) are built, reviewed, and merged. This document is the running backlog of what comes *after* the plan: docs, packaging, enterprise-readiness, and the productization work that turns "the plan is complete" into "an enterprise can adopt this."

Each item notes **current state** (grounded in the code as it stands), rough **effort** (S / M / L), and **priority**. Nothing here is committed scope yet — it's a decision surface.

Legend: effort **S** ≈ hours, **M** ≈ a day or two, **L** ≈ a week+. Priority **P0** = do before any external adoption, **P1** = do before a real "1.0", **P2** = nice-to-have.

---

## Progress log

Newest first. Items marked ✅ below are done; ◐ = partially landed.

- ✅ **§1.1 README status table** — flipped to shipped; top caveat narrowed to "not yet on PyPI" ([PR #11](https://github.com/jackwerner/sqbyl/pull/11)).
- ✅ **§1.4 Databricks tidy** — two metric-attribution code comments reworded vendor-neutral; competitive framing in the spec kept ([PR #11](https://github.com/jackwerner/sqbyl/pull/11)).
- ✅ **§1.5 spec/plan cleanup** — internal second-person voice stripped from the spec; "all phases complete" banner on the plan; README docs section framed ([PR #11](https://github.com/jackwerner/sqbyl/pull/11)).
- ◐ **§5.1 dependency & vulnerability management** — `pip-audit` (blocking) + Dependabot shipped ([PR #12](https://github.com/jackwerner/sqbyl/pull/12)). CodeQL held (private repo needs Advanced Security; free once public); `bandit`, SBOM, SHA-pinning still open.

- ✅ **Pre-public secret/history safety scan** — full 43-commit history scanned: no real API keys (only `sk-ant-...` doc placeholders), no hardcoded passwords/secrets/tokens, no credentials in connection strings, cassettes carry no auth material, `.sqbyl/` local state was never committed, and the one committed data file (`fixtures/orders.duckdb`) is synthetic seeded data (`random.Random(1729)`). `.gitignore` covers secrets/state going forward. **Repo is clean to make public** whenever you decide to; doing so also lights up CodeQL for free (§5.1).
- ✅ **Dependabot triage** — merged the two GitHub Actions bumps and the two dependency-floor bumps (`anthropic`→0.116, `fastapi`→0.139, `import-linter`→2.13, `psycopg`→3.3.4); full gate stayed green.
- ◐ **§3.1 versioning + PyPI (in-repo work)** — both packages at `0.1.0`, lockstep pin, `py.typed`, PyPI metadata, `CHANGELOG.md`, and a Trusted-Publishing `release.yml`. Remaining is PyPI-side setup + a live-API smoke test before tagging `v0.1.0` (see §3.1).
- ✅ **§5.8 OSS governance** — SECURITY / CONTRIBUTING / CODE_OF_CONDUCT / PR template + a CI license-compat gate ([PR #19](https://github.com/jackwerner/sqbyl/pull/19)).
- ✅ **§5.2 serve hardening** — reviewed: already satisfied (loopback default + non-local warning + test). Deliberately *no* `--auth` — dev models shouldn't be served publicly; production is runtime-embedding. No change needed.
- ✅ **§5.4 live-Postgres CI** — service-container job + integration tests; **found & fixed a real read-only-enforcement bug** in the Postgres adapter (uncommitted session `SET`). Verified against live PG.
- ✅ **§4.1 `sqbyl reset`** — clear local `.sqbyl/` state (keeps cost history + judge calibration unless `--all`).
- ✅ **§1.2 README enterprise overhaul** — audience reframe + Architecture + Security sections; hand-holding trimmed ([PR #22](https://github.com/jackwerner/sqbyl/pull/22)).
- ✅ **PyPI publishing guide** — [`PUBLISHING.md`](PUBLISHING.md), a first-timer walkthrough ([PR #24](https://github.com/jackwerner/sqbyl/pull/24)).
- ◐ **§2.2 `base_url` passthrough** — route Claude through a proxy/gateway via `model.base_url` or `load(base_url=)`.
- ◐ **§5.1 supply-chain polish** — SBOM (CycloneDX) attached to releases + all GitHub Actions SHA-pinned. Bandit deferred (low ROI; the one real finding — unquoted identifiers in the profiler — is tracked as a quoting follow-up).
- ✅ **§2.3 concurrency-safe runtime** — made `TraceWriter` + lazy client init thread-safe; verified one `Agent` serves concurrent `ask()`s; README documents the async/threadpool pattern.

**Next up (candidates):** the public flip + PyPI reservation/Trusted-Publisher setup + `v0.1.0` tag (your call / needs your accounts); `base_url` provider passthrough (§2.2, small — bring-your-own-Claude-endpoint/gateway); §5.1 polish (bandit / SBOM in release / pin actions by SHA); a GitHub Pages docs site (§1.3, larger).

---

## 1. Documentation & positioning

### 1.1 Update the README "Project status" table — **S, P0** — ✅ done (PR #11)
The status table still reads "🔜 in progress / planned / later" for every capability, but Phases 0–9 are done. It now undersells the project to anyone who lands on it.
- Flip the table to reflect reality: engine, eval harness, coach + judges, guided `init`, orchestrator, cost machinery, release + runtime + optimizer, more dialects, serve, exports, importers → **shipped**.
- Drop the "some commands below are not built yet" caveat at the top, or narrow it to the genuinely-unbuilt (large-schema *LLM* selection tuning, warehouse dialects unverified against live warehouses — see §5.4).
- Keep the "expect shapes to change until a first tagged release" line until we actually tag (see §3).

### 1.2 Overhaul the README for enterprise professionals — **M, P1** — ✅ done (PR #22)
> **Status:** shipped — audience reframe, an Architecture-at-a-glance section (two-package split + dependency arrow), a Security & data-handling section (which also covers most of §5.7's threat-model ask and mentions §5.6's OTel export), hand-holding trimmed, stale dialect line fixed. Deeper docs site (§1.3) is still separate.
The current README is written for an individual cloning-and-hacking. Retarget it at an engineer evaluating sqbyl for a team, without dumbing anything down.
- **Cut the obvious.** Remove hand-holding a professional doesn't need: the `# Credentials never do — use env: indirection` comment in the `sqbyl.yaml` example, the "you're comfortable with a CLI" reassurance, over-explained `env:` mechanics. Assume the reader knows what a read-only role and an env var are.
- **Lead with the evaluation story, not the walkthrough.** The "[Built for defensible ML systems](README.md#built-for-defensible-ml-systems)" section is the actual differentiator for an enterprise buyer (held-out gate, deterministic headline, provenance-stamped scorecard). Consider promoting it above the quickstart.
- **Add an architecture-at-a-glance** — the two-package split (`sqbyl` vs `sqbyl-runtime`), the one-way dependency arrow, "the runtime is what you embed; the toolkit stays in dev/CI." Enterprises care what lands in their production image.
- **Add a security & data-handling section** (links to §6.1–6.4 below): read-only by default, credentials via `env:`/secret manager, no row data in committed files or traces, local-first traces exportable to OTel, CI never spends tokens. This is the section a security reviewer will grep for.
- Keep the honest-uncertainty tone; that *is* the brand. "Enterprise" here means precise, not corporate-bland.

### 1.3 Publish docs / tutorials (GitHub Pages) — **L, P1** — ⏸ deferred to post-public (by decision)
Three long `.md` files at the repo root (design spec, user journey, implementation plan) are great references but aren't navigable docs.
- Stand up a docs site — **MkDocs (Material)** or **Docusaurus**. MkDocs is lighter and Python-native (fits the `uv` toolchain); Docusaurus if we want versioned docs + search out of the box.
- Structure: *Getting started* → *Concepts* (the loop, dev/test discipline, the context hierarchy) → *CLI reference* (generated from the `argparse`/command table where possible) → *Runtime embedding guide* → *Configuration reference* (generated from the pydantic models — see invariant 2, we already have the schemas) → *Tutorials*.
- **Tutorials to write:** (1) zero-to-release on the bundled DuckDB dogfood fixture — this already exists as the CI smoke test, so it's a guaranteed-correct tutorial; (2) embedding a release in a FastAPI app; (3) importing an existing dbt project / query log; (4) reading and acting on a coach report.
- Auto-publish on tag via GitHub Actions → `gh-pages`. Generate the config reference from the pydantic models so docs can't drift from the schema (leverage invariant 2).

### 1.4 Scrub direct Databricks references? — **S, P1 (mostly no, but tidy two)** — ✅ done (PR #11)
You flagged this for lawsuit/patent caution. Assessment: **low risk, but worth a targeted cleanup.**
- **Naming a competitor and describing its public product is legal and normal** (comparative positioning). "Databricks Genie + Agent Bricks, unbundled" in the design spec is a positioning statement, not IP misuse. There is no patent exposure from *describing* what a public product does, and no trademark issue in nominative comparison. Keep the spec's competitive framing — it's the clearest explanation of what sqbyl is.
- **Do reconsider two things:**
  1. **Product-facing surfaces vs. internal spec.** The design spec is an internal/repo doc; heavy "unbundled Databricks" framing there is fine. But the *public README / docs site* should stand on its own value ("defensible text-to-SQL you can version and audit"), not primarily as "the open Databricks." Positioning purely as a competitor's shadow is a marketing weakness more than a legal one.
  2. **Two code comments** reference Databricks as the source of a *metric*: `models/runs.py:344` and `calibration_io.py:7` ("the metric Databricks reports for its own judges"). The judge↔human agreement rate is a generic, well-understood evaluation metric — reword these to describe the metric on its own terms (e.g. "inter-rater agreement between the LLM judge and human reviewers") rather than attributing it to a vendor. Cleaner and removes any implication we copied a proprietary method.
- **Do not** copy any Databricks prompt text, benchmark question sets, or scoring rubrics verbatim — build ours from the spec. (No evidence we have; just the standing rule.)
- Net: no legal scrub required; do a light editorial pass so the *public* story is self-standing and the two metric-attribution comments are vendor-neutral.

### 1.5 Clean up the design spec & implementation plan before public — **S–M, P0** — ✅ done (PR #11)
Both root docs are strong references but were written as internal working documents; three artifacts read awkwardly to an outside visitor (and the README links all three under "Documentation"):
- **Spec: strip the second-person "requirements-capture" voice.** Two passages address the reader as "the user": `sqbyl-design-spec.md:408` (*"The thing the user specifically wants: Databricks recommends…"*) and `:550` (*"you explicitly want to stay free to change the model"*). A public reader can't tell who this "user" is — it reads like notes from a client interview. Convert to plain product statements ("sqbyl's Coach recommends…", "the brain/body split keeps you free to change the model"). ~30 min.
- **Plan: fix the tense — it's a build log in future tense.** [`sqbyl-implementation-plan.md`](sqbyl-implementation-plan.md) is written imperative-to-self (*"don't build it yet," "do not skip or compress it," "deferred to Phase 9," "Done when:"*). Every "don't build yet / later phase" line now describes shipped work. Options: (a) top-and-tail with a "this was the build plan; Phases 0–9 complete" banner and keep it as build history, (b) convert to a forward roadmap + CHANGELOG, or (c) move it under `docs/design/` so it isn't the first deep doc a visitor hits. Recommendation: **(a) + (c)** — cheapest and honest.
- **User journey: spot-check the transcripts.** [`sqbyl-user-journey.md`](sqbyl-user-journey.md) is the most publish-ready (narrated UX), but its CLI output blocks should be verified against what the built commands actually print before it's cited as documentation.
- **README framing:** the "Documentation" section links all three flatly — add a line of framing ("the spec is the *why*, the plan is *how it was built*, the journey is a *narrated first run*") so expectations are set.
- Ties to §1.4 (the Databricks competitive framing lives in these same docs — do both editorial passes together).

---

## 2. Model & provider abstraction

### 2.1 What model is the judge vs. the response model? — **(answer, no work)**
Both default to **`claude-opus-4-8`**, but they're already independently pinnable. See `ModelConfig` in [`models/manifest.py`](packages/sqbyl/src/sqbyl/models/manifest.py:36): there are per-role fields — `agent_model`, `judge_model`, `selection_model`, `orchestrator_model`, `synth_model`, `coach_model` — each falling back to `default` via `for_role()`. So out of the box the agent (response) and the judge are the *same* model; you can pin them apart in `sqbyl.yaml`:
```yaml
model:
  default: claude-opus-4-8
  agent_model: claude-opus-4-8      # the response model
  judge_model: claude-sonnet-4-6    # a cheaper/different judge, if you want independence
```
- **Worth documenting** in the config reference — the per-role pinning is a real feature that's currently invisible.
- **Worth considering (ML-systems):** using the *same* model as agent and judge risks correlated blind spots (the judge shares the agent's failure modes). The scorecard already stamps model-per-role for provenance; we should *document the recommendation* to pin a different judge model where independence matters, and note that the headline accuracy is deterministic anyway so this only affects the advisory judge layer. — **S, P1**

### 2.2 Abstracting the LLM to OpenAI / Azure OpenAI / Bedrock / Vertex — **difficulty assessment** — ◐ base_url done
> **Status:** the `base_url` passthrough (the first tier below) shipped — `model.base_url` in the manifest (plain or `env:`) and `base_url=` on the runtime `load()` route the Claude client through a corporate proxy / AI gateway with no other change. Bedrock/Vertex Claude clients and non-Claude providers (OpenAI/Azure) remain as scoped below.
The seam is already clean, which makes this **much easier than a from-scratch retrofit.** Everything goes through the `LLMClient` ABC ([`llm/base.py`](packages/sqbyl-runtime/src/sqbyl_runtime/llm/base.py:120)) with a single `complete(LLMRequest) -> LLMResponse`; structured output, caching, and usage accounting live *inside* the implementation, not in callers. `ModelConfig` even already carries a `provider: str = "anthropic"` field that is currently unused — the intended dispatch point.

Difficulty, tiered by target:

- **Anthropic via AWS Bedrock / GCP Vertex — S–M, P1.** These serve *Claude models* over a different endpoint/auth. The `anthropic` SDK ships `AnthropicBedrock` / `AnthropicVertex` clients with the *same* message shape, tool-use, and `cache_control`. A `BedrockLLMClient` / `VertexLLMClient` is mostly `_ensure_client()` swapping the constructor + auth (IAM / ADC) — the `complete()` body barely changes. **This is the high-value one for enterprise** (data residency, existing AWS/GCP contracts, no new vendor). Do this first.
- **Custom base URL / gateway (LiteLLM, Cloudflare AI Gateway, a corporate proxy) — S.** Add an optional `base_url` to `ModelConfig` / the client constructor and pass it to `anthropic.Anthropic(base_url=...)`. Trivial, and covers a lot of "route through our gateway" enterprise asks.
- **OpenAI / Azure OpenAI — M–L, P2.** A genuinely different provider. New `OpenAILLMClient` implementing the ABC. The non-trivial parts:
  - **Structured output** is different: we currently force strict JSON via a single forced *tool* (`emit_result` in [`anthropic_client.py`](packages/sqbyl-runtime/src/sqbyl_runtime/llm/anthropic_client.py:74)). OpenAI has native JSON-schema `response_format` (structured outputs) — different mechanism, similar guarantee. Azure OpenAI adds deployment-name-vs-model-name indirection and its own endpoint/key auth.
  - **Prompt caching** semantics differ (OpenAI auto-caches by prefix; no explicit `cache_control`). Our `cache_system` flag would become a no-op there — fine, but the cost estimator's cache-token accounting assumes Anthropic's read/write split.
  - **Pricing table.** [`cost.py`](packages/sqbyl-runtime/src/sqbyl_runtime/cost.py:45) hard-codes Anthropic list prices. A provider abstraction needs a per-provider price table (or a pluggable pricing seam) or every estimate/meter is wrong. This is the sneaky-big part of the work — invariant 5 (cost gating) runs through the whole app.
  - **Model-role defaults** (`claude-opus-4-8` etc.) would need provider-aware defaults.
- **Recommended sequencing:** (1) `base_url` passthrough (S) → (2) Bedrock/Vertex Claude clients (S–M) → (3) generalize the pricing seam per-provider (M) → (4) only then OpenAI/Azure if there's real demand. The seam design means each step is additive and testable with the existing mock/replay harness (invariant 4) — write a mock-backed unit test + one record-replay cassette per new client, same as every other LLM path.
- **One caveat worth stating up front:** sqbyl's whole pitch is "one Anthropic key powers everything." Multi-provider is a real feature but it *dilutes that story*. Frame it as "bring your own Claude endpoint (Bedrock/Vertex/gateway)" first; treat non-Claude providers as a separate, later decision.

### 2.3 Async & concurrency for enterprise APIs — **S** — ✅ done
> **Status:** the shipped runtime is now verified safe under concurrent load. `agent.ask()` is synchronous/blocking (LLM round-trip + DB), but one loaded `Agent` can be called from many threads at once — the DB engine pools per-thread connections, the Anthropic client is thread-safe, and the two shared-mutable pieces were locked (**`TraceWriter` appends** and **lazy SDK-client construction**). An end-to-end concurrent-`ask()` test plus targeted thread-safety tests cover it. README's "Async & concurrency" note documents the threadpool pattern (sync endpoint auto-threadpooled, or `run_in_threadpool`/`asyncio.to_thread` from an `async def`) and the footgun (calling `ask()` bare inside `async def` blocks the loop). A native-async runtime (`AsyncAnthropic` + async DB) remains a large, demand-driven follow-up — the threadpool path is the supported one.

---

## 3. Packaging, versioning & distribution

### 3.1 When to start versioning + how to get onto PyPI / `uv` — **M, P0-for-1.0** — ◐ in-repo work done
> **Status (in-repo, done):** both packages bumped to `0.1.0`; `sqbyl` pins `sqbyl-runtime==0.1.0` (lockstep, verified in wheel metadata); `py.typed` markers added so downstream type-checkers see the packages' types; PyPI metadata (keywords, classifiers, project URLs); `CHANGELOG.md` (Keep-a-Changelog); a Trusted-Publishing release workflow (`.github/workflows/release.yml`) that builds both and publishes on a `v*` tag. Both dists pass `twine check`.
> **Still needs a human (PyPI-side):** register a Trusted Publisher per package + tag `v0.1.0`. Full step-by-step written up in [`PUBLISHING.md`](PUBLISHING.md) (accounts → pending publishers → `pypi` environment → live-API smoke test → tag → verify → later releases → troubleshooting).

Both packages sit at `version = "0.0.0"` and the README says "not yet published." Recommendation:
- **Start versioning now, at `0.1.0`**, using SemVer, the moment we're willing to let anyone `pip install` it. Phases 0–9 being complete is the natural trigger — the CLI surface and file formats are stable enough to name.
- **The two packages must version in lockstep at first.** `sqbyl` depends on `sqbyl-runtime`; pin `sqbyl-runtime==<same>` (or a compatible-release `~=`) so a `sqbyl` install can't pull an incompatible runtime. Publish both from one release process.
- **Two version numbers to keep distinct:** the *package* version (SemVer, code) and the release-artifact **`schema_version`** (an int in [`models/release.py`](packages/sqbyl-runtime/src/sqbyl_runtime/models/release.py:95)). The runtime already warns on schema mismatch at load; document the compatibility policy — which `sqbyl-runtime` versions read which `schema_version`s — because that's the contract an embedder actually depends on.
- **Publishing mechanics:** `uv build` produces wheels/sdists for each workspace member; `uv publish` (or `twine`) pushes to PyPI. Use **PyPI Trusted Publishing (OIDC from GitHub Actions)** so no long-lived PyPI token lives in secrets. A tagged release (`vX.Y.Z`) → CI builds both wheels → publishes → cuts a GitHub Release + docs. Reserve the `sqbyl` and `sqbyl-runtime` names on PyPI *now* (a 0.1.0 placeholder) before someone squats them.
- **Pre-1.0 (`0.x`) buys us room** to change CLI/file shapes with minor-version bumps while signaling "not yet frozen," which matches the current README caveat honestly.
- **Changelog + release notes** from that first tag (Keep-a-Changelog or towncrier). Enterprises diff changelogs before upgrading.

---

## 4. Multi-project & lifecycle UX

### 4.1 Start over / clear if the user doesn't like it — **S–M, P1** — ✅ done (`sqbyl reset`)
> **Status:** `sqbyl reset [DIR] [--all] [--yes]` shipped. Default clears derived `.sqbyl/` scratch (runs, traces, coach proposals, caches, candidates, feedback) but **preserves the two audit trails** — `usage.db` (cost history) and `calibration.jsonl` (human judge review) — so a reset can't silently erase the cost record or reviewed data. `--all` wipes the whole `.sqbyl/`. Confirmation required unless `--yes`. Authored files (`sqbyl.yaml`, `semantics/`, `benchmarks/`) are deliberately **not** touched — git reverts those (coach edits are git-tracked, so `git revert` already undoes an apply). The `--hard` project-file reset the original note floated is intentionally *not* built: deleting human-authored semantics via a flag is riskier than `git`.
Today "starting over" means manually deleting files. Two levels:
- **Reset generated state, keep the project:** everything paid/derived lives under `.sqbyl/` (runs, traces, usage, caches — gitignored). A `sqbyl reset` (or `clean`) that wipes `.sqbyl/` gives a clean slate without losing the semantics/benchmarks you authored. Add a `--hard` that also clears generated `semantics/`, `benchmarks/dev.yaml`, etc. back to a bare `init`, with a confirm prompt (it deletes human-reviewed work).
- **Undo the last coach apply:** coach edits are git-tracked, so `git revert` already works — but document it, and consider a `sqbyl coach undo` that reverses the most recent applied diff set (we know exactly which files each `apply N` touched).
- **Cost/usage reset:** clearing `.sqbyl/usage.db` resets the spend meter — make sure `reset` is explicit that it zeroes recorded spend (don't let someone accidentally erase their cost audit trail; maybe keep usage.db unless `--hard`).

### 4.2 Two repos / two CLIs / directory-based? — **(answer, mostly works today) + S doc/hardening**
- **Yes, it's directory-based and always has been.** A sqbyl "project" *is* the directory (`sqbyl.yaml` + `semantics/` + `benchmarks/` + `.sqbyl/`). Run `sqbyl` from repo A and it operates on A; from repo B, on B. There's no global daemon or shared state — everything is rooted at the project dir (the `SqbylPaths(root)` layout). So **two repos in two terminals work independently** today, the same way `git` does.
- **Worth verifying/documenting:**
  - How does sqbyl find the project root — cwd only, or does it walk up to find `sqbyl.yaml` (like git finds `.git`)? If it doesn't walk up, running from a subdirectory fails; walking up is the expected ergonomics. **Confirm and, if needed, add root discovery.** — S
  - `.sqbyl/usage.db` and traces are per-project, so cost budgets don't bleed across repos — good, state that explicitly.
  - `ANTHROPIC_API_KEY` / `DATABASE_URL` are env-scoped, so two projects in the same shell share them unless overridden. Document `.env`-per-project or `sqbyl.yaml`'s `env:` indirection pointing at different var names per project.
- Net: the model is already right; this is a **documentation item** ("sqbyl projects are directories; work on as many as you like, they don't share state") plus possibly root-discovery hardening.

---

## 5. Enterprise-readiness audit

A structured pass on "what would a security/platform team require before adopting this." Grouped by theme; each is a candidate work item.

### 5.1 Dependency & vulnerability management in CI — **M, P0** — *partially landed*
> **Status:** `pip-audit` (blocking CI job) and Dependabot (weekly, grouped Python deps + Actions) shipped. **CodeQL** is written and runs clean (0 alerts across 130 files) but **can't upload results while the repo is private** — GitHub code scanning needs Advanced Security (paid) on private repos, and is free once the repo is public. Re-add the `codeql.yml` workflow the moment the repo goes public. **SBOM** (CycloneDX, generated in `release.yml` and attached to the GitHub Release) and **action SHA-pinning** (every workflow `uses:` locked to a commit SHA with a `# vN` comment Dependabot still tracks) are now shipped.
> **Deferred — `bandit` as a gate:** a full pass found *no exploitable issues*, so it's low ROI right now (its value is guarding future contributions). The findings were B101 (asserts-for-type-narrowing), B311 (backoff jitter), B506 (a `SafeLoader` subclass — false positive), a fixed-argv `git status` subprocess, and **B608 in `profile.py`/`introspect.py`** — the one legitimate smell: profiling SQL interpolates table/column identifiers **unquoted**. It's mitigated (identifiers come from the DB's own catalog, and the read-only guard refuses any injected non-SELECT/multi-statement), but the proper hardening is to **quote identifiers** in the profiler — a real, self-contained follow-up worth doing before adding the bandit gate. Artifact signing (Sigstore/SLSA) also still open.

You asked directly: yes, we want this, especially as a package enterprises embed.
- **Dependency vulnerability scanning:** add `pip-audit` (or `uv`'s emerging audit, or `osv-scanner`) to CI against the resolved `uv.lock`. Fail (or warn-then-fail) on known CVEs in the dependency tree.
- **Automated dependency updates:** enable **Dependabot** or **Renovate** on `uv.lock` / `pyproject.toml` so version bumps arrive as reviewable PRs (CI already runs lint → type → test → import-linter, so bumps are gated). Renovate handles `uv` lockfiles well and can group/schedule.
- **Static security analysis:** add **`bandit`** (Python security linter) and enable **GitHub CodeQL** — cheap, high signal for a security-sensitive tool that executes SQL and shells out.
- **Keep it $0-token:** all of the above are static/deterministic — they respect invariant 4 (CI never spends API tokens). Good.
- **Supply-chain provenance (P1):** generate an **SBOM** (CycloneDX via `cyclonedx-py`) per release, and sign artifacts / attach build provenance (**Sigstore / SLSA** attestations from the GitHub Actions publish job). Enterprises increasingly require an SBOM before ingesting a package. Trusted Publishing (§3.1) already gives us keyless signing hooks.
- **Pin the toolchain:** pin the `uv` and Python versions in CI; pin GitHub Actions by SHA (not floating tags) — a common enterprise audit finding.

### 5.2 `sqbyl serve` / `run` hardening for real deployment — **M, P1**
The README is already honest that `serve` is "intentionally not hardened — don't put it on the open internet." For enterprise, either harden it or double down on "embed the runtime." Concretely:
- **The runtime embedding path is the sanctioned one** — `load()` + `agent.ask()` in the user's own FastAPI/Flask app, inheriting their auth, TLS, pooling, observability. This is the right story; **make the FastAPI embedding tutorial first-class** (§1.3) so nobody reaches for `serve` in prod.
- **If `serve` stays a dev convenience:** keep it stdlib, keep the "dev only" banner, but add at minimum an optional bearer-token/`--auth` flag and bind to `127.0.0.1` by default so an accidental `0.0.0.0` exposure requires intent. Consider printing a loud warning if bound to a non-loopback interface without auth.
- **Do NOT quietly turn `serve` into a production server.** If we ever want a hardened server, it should be an explicit, separately-documented mode (or just "put the runtime behind uvicorn yourself"). The current `ThreadingHTTPServer` is fine for local; it is not a production web server.

### 5.3 Secrets & configuration for enterprise — **S–M, P1**
- Today: credentials via `env:` indirection in `sqbyl.yaml`. Good baseline.
- Enterprise wants **secret-manager integration** — support resolving `env:` to more than process env: e.g. a `vault:`, `awssm:`, `gcpsm:` scheme, or just document the pattern "inject via your orchestrator's secret mount into env, then `env:`." Lowest-effort win: document the env-injection pattern clearly; higher-effort: pluggable resolver.
- **Credential privilege check is already there** (read-only warning on connect, invariant 6) — surface it in the enterprise security section as a feature.

### 5.4 Real-database CI (beyond the DuckDB fixture) — **M, P1** — ◐ Postgres done
> **Status:** a `postgres` CI job (service container, `postgres:16`) now runs live integration tests (`tests/test_postgres_integration.py`) covering session read-only enforcement, privilege introspection (superuser warns, SELECT-only role doesn't), and schema introspect — all $0/no-LLM. **This immediately caught a real bug:** the Postgres adapter set `SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` in the connect listener but never committed it, so SQLAlchemy's reset-on-checkout rolled it back and writes were silently allowed — read-only was *not enforced* on Postgres. Fixed by committing the SET in the listener; verified against a live server. **Follow-up:** the MySQL adapter uses the same SET-without-commit pattern — likely fine (MySQL `SET` isn't transactional), but unverified; check when adding live-MySQL coverage. Warehouse engines (Snowflake/BigQuery) still want optional credentialed jobs.
- Today CI exercises everything against the checked-in DuckDB fixture under record-replay ($0, no external deps) — excellent for correctness and cost discipline.
- But the **warehouse dialects (Snowflake, BigQuery, MySQL, Postgres) are not verified against a live engine.** The adapters fail-safe (`can_write=True` when they can't prove otherwise) but their read-only enforcement and privilege introspection are untested end-to-end. Before claiming enterprise Postgres/Snowflake support:
  - Add a **Postgres** service-container job in CI (real, free, dockerized) that runs the connection/read-only/privilege tests against live Postgres. This is the highest-value one — Postgres is a first-class M0 dialect.
  - Warehouse engines (Snowflake/BigQuery) can't easily run in CI; gate them behind an **optional, credentialed integration job** (manual/nightly, skipped by default) and be explicit in docs that they're "supported, community-verified" until we have that.
- Keep these jobs **separate** from the default $0 CI so invariant 4 holds for the main pipeline.

### 5.5 Enterprise-framework compatibility (langchain / pydantic / fastapi …) — **S audit, then per-item**
You listed langchain/pydantic/fastapi. Status:
- **pydantic v2** — already the schema backbone everywhere (invariant 2). ✅ No work; document the version floor.
- **FastAPI** — no dependency, by design. The runtime is framework-agnostic; you embed `agent.ask()` in *any* framework. The right move is a **first-class FastAPI example + tutorial** (§1.3), not a dependency. ✅ direction, doc work.
- **LangChain** — we ship an *optional* `langchain_tool` export ([`export.py`](packages/sqbyl-runtime/src/sqbyl_runtime/export.py)) behind the `[langchain]` extra, lazily imported. ✅ Verify it tracks current `langchain_core` tool interfaces; add a LangChain usage snippet to docs.
- **Broader audit to run:** confirm the runtime's *hard* dependency set is genuinely minimal (that's the whole point of the two-package split) and that every heavyweight integration (warehouse drivers, langchain) is an optional extra with a friendly install hint — this pattern is already established; the item is to **audit that nothing crept into `sqbyl-runtime`'s required deps** and to publish a "dependency footprint" table for the runtime (enterprises vet what lands in prod).
- **MCP** — we ship a stdlib JSON-RPC MCP server (no `mcp` dep). Worth documenting as an integration surface; consider whether to track the official MCP SDK later.

### 5.6 Observability export for enterprise backends — **S, P1**
- Traces are OTel-GenAI-shaped and local-first in `.sqbyl/` (invariant 7). The differentiator for enterprise is the **export path**: document (and smoke-test) exporting to a real OTel collector → Datadog / Honeycomb / Grafana Tempo. "Local by default, exportable to your existing observability stack" is a strong enterprise line — make sure it actually works end-to-end and write the how-to.

### 5.7 Prompt-injection / untrusted-data posture — **S doc, M if hardening — P1**
- The SQL agent reads schema metadata, column samples, and (via importers) existing query text — some of which can be attacker-influenced in a real warehouse (e.g. a table/column named to carry an instruction, or a profiled sample value). We already refuse non-SELECT at the SQL layer (invariant 6), which caps the blast radius to *reads*, and importers tag `contains-literals` for review. Good structural containment.
- **Write it down as a threat model:** what an injected instruction *could* do here is limited to steering which SELECT runs — it can't write, can't DDL, can't exfiltrate beyond what the read-only role already sees. State that explicitly; it's a reassuring story when told plainly.
- Consider whether profiled sample values / column descriptions that flow into the prompt need any sanitization or delimiting. Low urgency given the read-only cap, but a security reviewer will ask.

### 5.8 Licensing, governance & contribution — **S, P1** — ✅ done
> **Status:** `SECURITY.md` (GitHub private-advisory reporting), `CONTRIBUTING.md` (setup + gate + the 7 invariants in contributor language), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), and a PR template shipped. CI now runs a **license-compatibility gate** (`pip-licenses --fail-on=GPL;AGPL;LGPL`) — the current tree is all MIT/BSD/Apache/PSF/MPL-2.0, no copyleft-viral deps. Issue templates remain a nice-to-have. *(CoC enforcement contact is the maintainer email — swap for a dedicated address if preferred.)*
- **License:** MIT is declared in the README; confirm a `LICENSE` file exists and headers are consistent. MIT is a good, adoption-friendly choice for enterprise (no copyleft friction). Confirm all dependencies are MIT/BSD/Apache-compatible (a `pip-licenses` check in CI catches a surprise GPL dep).
- **Open-source hygiene for adoption:** add `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, a `SECURITY.md` (how to report vulns — enterprises look for this), and issue/PR templates. Cheap, and they're checkboxes on enterprise OSS-vetting forms.

---

## 6. Suggested sequencing

A defensible order if we pursue these:

1. **P0, immediate & cheap:** README status table (§1.1), start versioning + reserve PyPI names (§3.1), CI vuln scanning + Dependabot/Renovate + bandit/CodeQL (§5.1). These are low-effort, high-signal, and unblock external eyes.
2. **P1, "toward 1.0":** README enterprise overhaul + security section (§1.2), docs site with the DuckDB and FastAPI tutorials (§1.3), document per-role model pinning + judge-independence guidance (§2.1), `base_url` passthrough then Bedrock/Vertex clients (§2.2), `sqbyl reset` + multi-project docs (§4), live-Postgres CI (§5.4), OTel export how-to (§5.6), SECURITY.md/CONTRIBUTING.md (§5.8).
3. **P2, demand-driven:** OpenAI/Azure providers + per-provider pricing seam (§2.2), secret-manager resolvers (§5.3), SBOM/signing (§5.1), any `serve` hardening (§5.2).

## 7. Open questions for Jack

- **Multi-provider vs. "one Claude key" story** — do we want non-Claude providers at all, or is "bring your own Claude endpoint (Bedrock/Vertex/gateway)" the ceiling? This decides whether §2.2's OpenAI work is ever on the table.
- **Is `serve` a keeper or a demo?** If we're committed to "embed the runtime," we could even *remove* `serve` for prod-shaped use and keep only `run`/MCP. Clarifies the hardening question.
- **Docs tooling** — MkDocs (lighter, Python-native) vs Docusaurus (versioned docs, richer)? Affects §1.3 effort.
- **What's the "1.0" bar?** Naming the criteria (e.g. live-Postgres CI green + docs site + versioned + SBOM) turns this backlog into a milestone.
