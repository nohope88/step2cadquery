# step2cadquery

Give it a **STEP file** — get back a clean, **parametric CadQuery project**
that rebuilds the same object at the same size.

## How it works

```
                          part.step
                              │
                              ▼
             ┌────────────────────────────────┐
             │  1. RENDER + MEASURE           │
             │     render_step.py             │
             │                                │
             │  CadQuery imports the STEP     │
             │  • measures ground truth:      │
             │    bbox / volume / solids      │
             │  • renders 6 views:            │
             │    iso front back left right   │
             │    top                         │
             └────────────────────────────────┘
                              │
              6 PNGs + measurements.json
                              │
                              ▼
             ┌────────────────────────────────┐
             │  2. BRIEF  (vision)            │
             │     gen_brief_step.py          │
             │                                │
             │  a headless Claude session     │
             │  LOOKS at the renders, takes   │
             │  the measurements as ground    │
             │  truth (renders have no scale) │
             │  and writes a faithful         │
             │  reconstruction spec           │
             └────────────────────────────────┘
                              │
                          brief.md
                              │
                              ▼
             ┌────────────────────────────────┐
             │  3. BUILD                      │
             │     gen_model.py               │
             │                                │
             │  a headless Claude session     │
             │  implements the brief in       │
             │  parametric CadQuery (bundled  │
             │  cadcode skill), iterating     │
             │  render → inspect → fix until  │
             │  the geometry verifies solid   │
             └────────────────────────────────┘
                              │
                              ▼
                         out/<slug>/
        main.py · params.py · spec.md · <slug>.step · <slug>.stl
```

## Usage

```bash
python3 pipeline.py examples/3DBenchyStepFile.step
# → out/3dbenchystepfile/{main.py, params.py, spec.md, *.step, *.stl}
```

## Requirements

- `uv` — stages pull their Python deps on demand
- authenticated `claude` CLI (subscription OAuth token works headless)
- the cadcode skill is **bundled** at `skills/cadcode/` (MIT) — nothing to
  install; set `CADCODE_SKILL` to use a shared install instead

## Models

| Env | Default |
|---|---|
| `BRIEF_MODEL` | `claude-opus-4-8` |
| `GEN_MODEL` | `claude-sonnet-5` |
| `GEN_MAX_TURNS` | `250` |

## Example

`examples/3DBenchyStepFile.step` — [#3DBenchy](https://www.3dbenchy.com/) by
Creative Tools (CC BY-ND 4.0), included unmodified as a test input.
