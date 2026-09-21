# AGENTS.md

## Project

A post-training project that follows Nathan Lambert's RLHF book (rlhfbook.com) by building. It trains three small models (Qwen3-0.6B Base) for an online store's customer support: a triage model, a reply writer and an action model. Each is scored against a frontier API on its own job. The store runs on the real Olist e-commerce data.

The plan is `docs/spine.md` (builds F0, B1–B13). Its Status section says which build is current. Work only on the current build.

## Who writes what

- The owner writes the core learning code: losses, log-probs, rewards, advantages, policy updates, scorers and estimators. It lives in `core/`, and includes anything the current build lists under "You implement".
- Do not create or edit files in `core/` unless the owner explicitly asks ("implement this for me"). You may read it and review it; put review comments in your reply, not in the code.
- If a task needs a core mechanism that doesn't exist yet, stop and describe what's needed (inputs, outputs, signature). Don't stub it with a working implementation.
- Everything else is yours: data download and loading, the store database and tools, generation pipelines, configs, training scaffolding, TRL library versions, logging, plots, Modal scripts and tests.
- Design decisions are the owner's: the store policy, the ticket mix, train/held-out splits, label definitions. Implement them from the owner's spec and ask when the spec is missing.

## Data and results integrity

- Never edit held-out data, labels, the policy text or scorers to change a result. If you think one is wrong, say so and wait.
- Splits are fixed once made. Never train on held-out data.
- Report only numbers produced by a logged run. Every result records the commit, config and seed.
- Never change a baseline, metric or comparison setting without saying so.
- Generated data (tickets, demonstrations, labels) is saved with the prompt, model and seed that produced it.

## Compute and cost

- Development runs on a local RTX 3050 6GB under WSL, with LoRA. Smoke-test every training script on a tiny config locally first.
- Never launch Modal jobs, bulk paid API calls or large downloads without asking. State the expected cost and time.

## Conventions

- Python with uv (`uv add`, `uv run`); PyTorch, Transformers, PEFT, TRL.
- Hyperparameters live in `configs/`, one file per experiment, never hard-coded in scripts.
- Keep changes small and say what changed and why. No scaffolding for future builds.

## Layout

Proposed; update it to match the repo as it grows.

```
core/        owner-written mechanisms (agent: read only)
store/       SQLite store, tools, final-state checker
data/        raw Olist CSVs and generated datasets (raw is gitignored)
configs/     one config per experiment
scripts/     entry points: generation, training, evaluation, Modal launch
runs/        run outputs and logs (gitignored)
docs/        spine.md and build notes
tests/
```

## Commands

Add commands here as builds introduce them.

- `uv sync`: install dependencies
- `uv run pytest`: run tests
