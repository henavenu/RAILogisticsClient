# Draft message to Sharad — monorepo `/assurance/` re-sync

**Status:** Drafted 2026-05-26 while Sharad was off. Send when he's back.
**Send via:** Slack DM or email — both work. Slack probably feels more natural given the tone.
**Companion doc (for him to skim if he wants detail):** `docs/monorepo_assurance_gap_analysis_2026-05-26.md` in `RAI-Trust-Platform`.

---

## Subject line options

- "Quick monorepo `/assurance/` heads-up — easy fix"
- "Heads-up on the monorepo assurance merge — looks like a partial pickup"
- "Tier 2 evals on the cloud link — found the likely cause"

---

## Slack-style version (shorter)

> Hey Sharad — was poking around the new monorepo today to test Tier 2 against the cloud link and ran into two things worth a heads-up. **First** — the SDK regression you patched in the old prototype is back: `sdk/rai_trust_sdk/observability/otel.py` line 244 uses bare `Resource(attributes=...)` instead of `Resource.create(attributes=...)`, which silently drops `OTEL_RESOURCE_ATTRIBUTES=model_id=<app>` from every emitted span. Arize then rejects them for missing `model_id`. We saw zero spans in the `mybank` project today even though MyBank was happily emitting — that one-line fix resolves it. I patched locally to unblock the demo but it's not pushed; landing it on `master` would mean the next clone works out of the box. (Doc: gap analysis §F.)
>
> **Second** — the `/assurance/` merge looks like it grabbed `api/` and `core/agents/` but didn't sweep in the rest of the tree (`evals/`, `metrics/`, `core/providers/`, `core/store/`, `core/models/`, plus `api/v1/models.py` and `core/agents/span_adapter.py`). The `api/main.py` and `router.py` still import those modules, so `uvicorn assurance.api.main:app` would crash at the first import — which probably explains why T2 evals aren't populating on the deployed link.
>
> Easy fix on our end — three options (in order of laziest → cleanest):
>
> 1. **Re-merge full tree:** `git read-tree --prefix=assurance/ -u` from `henavenu/RAI-Trust-Platform` branch `integration/pap-fast-evals` (commit `91b8dd9`). Overwrites the partial state with the working one.
> 2. **Submodule** — track our repo from the monorepo so it stays in sync long-term.
> 3. **Targeted patch** — we hand you just the missing modules + the env vars we need.
>
> Also: `env.example` at the monorepo root is missing `ANTHROPIC_API_KEY`, `ARIZE_API_KEY`, `ARIZE_SPACE_ID`, and a few `LLM_EVAL_CACHE_*` knobs. Even after the re-merge those need to be set for evals to fire.
>
> Tiny side note: GitHub default branch on the monorepo is `main` (which is basically empty), but all the real work is on `master`. Anyone cloning fresh — including Varun's CI — will get an empty tree unless they explicitly check out `master`. Easy to swap, just flagging.
>
> Full write-up with file/line references is in `docs/monorepo_assurance_gap_analysis_2026-05-26.md` on our repo. Happy to drive any of the three options, just let me know which you'd prefer.

---

## Email-style version (slightly longer, more formal)

> Hi Sharad,
>
> Hope your day off is going well — sending this so it's waiting for you when you're back, no rush. Two things came up while testing today, both with easy fixes on your end:
>
> ---
>
> **(1) SDK regression that blocks all OTel → Arize emission.** A clean clone of the monorepo today has `sdk/rai_trust_sdk/observability/otel.py` back to using bare `Resource(attributes={SERVICE_NAME: resolved_name})` on line 244. That's the same one-line bug you patched in your old prototype tree but never pushed upstream — it silently drops `OTEL_RESOURCE_ATTRIBUTES=model_id=<app>` from the resource, so Arize rejects every emitted span for missing `model_id`.
>
> Symptom on our side today: MyBank was up, the UI was responding to prompts, but the Arize `mybank` project showed 0 spans / 0 traces for the entire session — silently dropped. The fix is the same one-liner — change `Resource(...)` to `Resource.create(...)`:
>
> ```python
> resource = Resource.create(attributes={SERVICE_NAME: resolved_name})
> ```
>
> I patched it locally on my machine to unblock today's demo, but it's not committed or pushed. Landing it on `master` would mean the next clone (yours, Varun's CI, anyone's) works out of the box without surprise.
>
> ---
>
> **(2) The `/assurance/` merge missed most of the tree.**
>
> While testing the deployed cloud link this morning I noticed that Tier 2 evals weren't populating, and a bit of digging in the new monorepo pointed at a likely cause: the merge of `rai-assurance` into `/assurance/` looks like it only picked up the API layer (`api/main.py`, `api/v1/router.py`) and the agent skeleton (`core/agents/base.py`, `registry.py`), but the packages those files import from didn't follow across:
>
> - `metrics/tier2metrics.py` — the Tier 2 calculator (`run_cycle`)
> - `evals/evals.py` and `evals/fast_evals.py` — the Phoenix LLM-as-judge orchestration plus the Detoxify / Presidio / MS-MARCO / DeBERTa fast lane
> - `core/providers/`, `core/store/`, `core/models/`, `core/provider_registry.py`
> - `core/agents/span_adapter.py`
> - `api/v1/models.py` — the Pydantic response models the router imports on line 14
>
> Because `assurance/api/main.py` line 23 still does `from metrics.tier2metrics import run_cycle`, the service can't boot in the monorepo's current state — which would explain the cloud link returning empty / mocked metrics. Worth a quick look at the container logs for `ModuleNotFoundError: No module named 'metrics'` to confirm.
>
> For context, all the Tier 2 work has been living on **`https://github.com/henavenu/RAI-Trust-Platform`** (branch `integration/pap-fast-evals`, current head `91b8dd9`) — that's the code we integrated and validated together on 5/14, 5/20, and 5/21. The straightforward fix is to bring that tree across to the monorepo. Three ways to do it:
>
> 1. **Full re-merge** — `git read-tree --prefix=assurance/ -u rai-assurance/integration/pap-fast-evals` from the monorepo root, after adding our repo as a remote.
> 2. **Submodule** — `git submodule add -b integration/pap-fast-evals https://github.com/henavenu/RAI-Trust-Platform.git assurance` — cleaner for ongoing sync, but Varun's container build needs `git submodule update --init` in its Dockerfile.
> 3. **Targeted patch** — we hand you a focused diff covering just the missing modules + env vars + a `start_assurance.sh`. Smallest change, but we'd diverge over time.
>
> One more small thing: the monorepo's `env.example` doesn't yet list `ANTHROPIC_API_KEY`, `ARIZE_API_KEY`, `ARIZE_SPACE_ID`, or the `LLM_EVAL_CACHE_*` knobs. Without those set, the assurance service will boot (post-fix) but return empty data. I can send a patch for `env.example` alongside whichever fix path you pick.
>
> Heads-up too — the monorepo's default GitHub branch is `main`, which is empty; all the real work is on `master`. Anyone cloning fresh (including the cloud deploy pipeline) gets an empty tree unless they explicitly switch. Worth either renaming `master` → `main` or pinning the deploy at `master`.
>
> Full write-up with file paths and line references is in `docs/monorepo_assurance_gap_analysis_2026-05-26.md` in our repo if you want the detail. Happy to drive any of the three options — just say which you'd prefer and I'll have it across to you the same day.
>
> Cheers,
> Hena

---

## Notes for Hena (not for Sharad)

- Both versions deliberately use "looks like" / "didn't follow across" / "doesn't yet" — avoids implying he made a mistake.
- The three-option fix path puts agency in his hands without prescribing.
- The "anyone cloning fresh ... including Varun's CI" framing makes the default-branch issue feel like a deploy-pipeline concern, not a Sharad oversight.
- If you want to skip option 3, drop it — keeps it cleaner.
- Tone-match check: friendly, lightly technical, no urgency. Adjust if his usual register is different.
