# step2cadquery

Turn a **STEP file** into a clean, **parametric CadQuery project** — faithful
reconstruction via multi-angle renders, a vision design brief, and a headless
CAD-build agent.

```
input:  path/to/part.step
output: out/<slug>/          ← importable model folder
          main.py            parametric CadQuery source (gen_step())
          params.py          every dimension as a named parameter
          spec.md            title + one-paragraph description
          <slug>.step        rebuilt geometry
          <slug>.stl         rebuilt mesh
```

## How it works

| Stage | Script | What it does |
|---|---|---|
| 1. Render + measure | `render_step.py` | Imports the STEP with CadQuery, measures ground-truth bbox / volume / solid count, renders 6 labeled views (iso, front, back, left, right, top) to `text/<slug>/showcase_images/` |
| 2. Brief (vision) | `gen_brief_step.py` | One headless Claude session Reads the renders, gets the measurements as ground truth (renders carry no scale reference), and writes `design_readme.md` + `brief.md` — a faithful reconstruction spec with explicit parametric approximations for organic surfaces |
| 3. Build | `gen_model.py` | A headless Claude session implements `brief.md` as a parametric CadQuery project via the cadcode skill, iterating render-and-fix until the geometry verifies (STEP+STL present, is_solid, no blocking warnings), then prunes to base files |

## Usage

```bash
python3 pipeline.py examples/3DBenchyStepFile.step
# → out/3dbenchystepfile/{main.py, params.py, spec.md, *.step, *.stl}
```

## Tests

```bash
uv run --python 3.12 --with pytest --with pytest-cov python3 -m pytest tests/ \
    --cov=pipeline --cov=render_step --cov=gen_brief_step --cov=gen_model \
    --cov-report=term-missing --cov-fail-under=100
```

Runs offline in <1 s — the Claude Agent SDK and CadQuery are stubbed
(`tests/sdk_stub.py`, fake `cadquery` module), so no auth or heavy deps needed.
Coverage is 100% on all four modules.

## Requirements

- `uv` (stages pull `cadquery` / `trimesh` / `matplotlib` / `claude-agent-sdk`
  on demand)
- authenticated `claude` CLI (subscription OAuth token works headless)
- the **cadcode skill** installed at `~/.claude/skills/cadcode` — provides the
  headless renderer used by stage 1 and the build loop / helpers used by
  stage 3

## Models

| Env | Default | Used by |
|---|---|---|
| `BRIEF_MODEL` | `claude-opus-4-8` | brief (decides the design direction — worth the stronger model) |
| `GEN_MODEL` | `claude-sonnet-5` | build |
| `GEN_MAX_TURNS` | `250` | build session turn cap |

## Folder layout after a run

```
text/<slug>/        intermediate: renders, measurements.json, design_readme.md, brief.md
out/<slug>/         final model folder (the deliverable)
sessions/<slug>/    agent transcripts (.jsonl + .md) for both LLM stages
```

## Example

`examples/3DBenchyStepFile.step` — [#3DBenchy](https://www.3dbenchy.com/) by
Creative Tools (CC BY-ND 4.0), included unmodified as a test input.
