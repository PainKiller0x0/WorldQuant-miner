# WorldQuant Alpha Mining

This context covers Alpha generation, WorldQuant validation, submission, and reuse as future research material.

## Language

**Alpha**:
A WorldQuant FASTEXPR expression together with its simulation, check, and submission history.
_Avoid_: factor row, formula record

**Successful Pool**:
Alphas accepted by the WorldQuant Submit operation and eligible as preferred Evolver parents.
_Avoid_: submitted list, winner table

**Candidate Pool**:
Simulated Alphas that are neither submitted nor rejected and may still enter validation or submission.
_Avoid_: pending list, hopeful pool

**Failure Pool**:
Alphas rejected by simulation, checks, or Submit while retaining the authoritative failure reason.
_Avoid_: trash, blacklist

**Submission Queue**:
The persistent single-flight ordering of Candidate Pool Alphas awaiting WorldQuant Check or Submit.
_Avoid_: pending pool

**Evolver Parent**:
The Alpha whose economic idea is intentionally mutated by an Evolver generation.
_Avoid_: recent row, random seed

## Relationships

- An **Alpha** belongs to exactly one of the **Successful Pool**, **Candidate Pool**, or **Failure Pool**.
- A successful Submit moves an **Alpha** from the **Candidate Pool** to the **Successful Pool**.
- A definitive WorldQuant rejection moves an **Alpha** from the **Candidate Pool** to the **Failure Pool**.
- The **Submission Queue** contains only **Candidate Pool** Alphas.
- The **Evolver Parent** comes from the **Successful Pool** when that pool is non-empty, otherwise from the strongest **Candidate Pool** Alpha.

## Example dialogue

> **Dev:** "Submit accepted this Alpha. Should Evolver still pick the newest Candidate?"
> **Domain expert:** "No. Move it to the Successful Pool and prefer it as the next Evolver Parent; keep rejected Alphas in the Failure Pool as negative evidence."

## Flagged ambiguities

- "pending" previously meant both unsimulated Rust work and WorldQuant submission work; use **Candidate Pool** for Alpha state and **Submission Queue** for Check/Submit ordering.
