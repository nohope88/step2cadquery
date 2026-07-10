# cadpy (vendored)

This directory is a vendored snapshot of `packages/cadpy/` from the Panda
repo. In Panda it is populated at build time by
`scripts/build/build-skill-runtimes.sh`; that script is not part of this
repo, so the snapshot is **committed here** to keep the repo clone-and-run.

The runner imports `cadpy.generation.generate_step` via the standard
`import cadpy` mechanism. Tests inject a fast in-process stub via
`CADCODE_TEST_CADPY_PATH` (see `tests/conftest.py`) so the suite doesn't
pay for full OCCT STEP exports per assertion.

Do not hand-edit lightly: in Panda, edits to the canonical
`packages/cadpy/` source are the only way to change vendored contents.
Here, treat this directory as third-party vendor code.
