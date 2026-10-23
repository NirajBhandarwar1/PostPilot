# Promotion gate before the speed test

A small, offline gate for **saved outputs**. It does not call DeepSeek V4.1 Flash or any other model. `candidate_SIMULATED.json` contains hand-written **SIMULATED examples, not model results**; three are deliberately invalid.

## Run on Windows

Requires Python 3 and no packages, API key, paid account, or network access. Save all four files in one directory, then run in PowerShell:

```powershell
py -3 gate.py
$LASTEXITCODE
```

Exit status: `0` = all cases pass; `1` = promotion blocked; `2` = invalid or unreadable input. The default simulated run exits `1`. Expected terminal output:

```text
SOURCE: SIMULATED (not model results)
CASE     STATUS RULE / DETAIL
C01      PASS   -
C02      PASS   -
C03      PASS   -
C04      FAIL   invariant.route expected 'billing'; got 'technical'
C05      PASS   -
C06      PASS   -
C07      PASS   -
C08      PASS   -
C09      FAIL   required.priority missing
C10      PASS   -
C11      PASS   -
C12      PASS   -
C13      PASS   -
C14      PASS   -
C15      FAIL   invariant.human_review expected True; got False
C16      PASS   -
C17      PASS   -
C18      PASS   -
PROMOTION: BLOCKED | failed cases: 3/18 | failed rules: 3
```

`$LASTEXITCODE` then prints `1`. For a screenshot, label the image **“Synthetic example.”**

## Adapt to a real gate

The toy task routes synthetic tickets. Each `cases.json` entry has an ID, a task, required output fields, and exact-match invariants. For this *illustration*, the expected route follows `category`; `service_down: true` requires `priority: high` and `human_review: true`. These are invented toy rules, **not** claims about a production system.

Before using this at work, replace the toy cases and rules with publicly discussable examples of **{{the production task we would gate}}**, **{{required fields}}**, and **{{product-specific invariant}}**. Define the hard stop as **{{the failure we cannot accept}}** and the post-deployment rollback condition as **{{our rollback trigger}}**. Check that the saved cases and invariants actually represent those risks.

If access is available, collect outputs separately using an authorized process. Save them in a **different** JSON file with the same `outputs` structure, IDs matching the cases, and `"source": "USER_SUPPLIED_REAL"`. Run:

```powershell
py -3 gate.py --candidate .\candidate_real.json
$LASTEXITCODE
```

The CLI does **not** verify provenance or generate outputs. Document the model and collection procedure separately; never relabel the simulated file as a model run. Publish the tool and its limitations alongside any result.

**Limitations:** A pass means only that saved outputs matched these exact fields and cases. It does not establish model quality, latency, cost, safety beyond the checked rules, or a production rollout decision. The checker trusts the case definitions and JSON provenance label; it cannot catch omitted risks or verify that a supposedly real file came from a model.
