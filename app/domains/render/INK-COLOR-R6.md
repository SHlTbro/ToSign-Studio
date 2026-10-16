# R6 bounded apparent source ink

The distribution `ink_color_catalog.json` is disabled. A code-only distribution
contains no original photographs. Its two train choices are `train-magenta-v1`
(`#C41AB4`) and `train-violet-v1` (`#2F0CDD`). The validation source in the exact
measurement JSON is descriptive evidence only and can never resolve as a palette.
The copies of measurement/contract evidence retain their original file hashes.
Historical paths in that evidence are descriptive, never opened for authorization.

## Integration contract

- `resolve_author_palette(user_id, resolved_style, ink_palette_id,
  layout_profile='natural-layout-v1', render_source_policy='m2_required',
  frozen=None, session=None)` returns `rgb` and the complete `binding`
- `discover_author_palettes(...)` returns `available`, `reason`, and `palettes`.
  Only currently enabled, fully revalidated choices are advertised; disabled or
  unavailable choices are absent. Availability does not assert visual readiness
- The optional session selects the database bind. Resolution opens a separate,
  read-only Session for committed profile/source permissions and actual source
  bytes. It does not autoflush, commit, admit, approve or mutate caller state
- Binding schema is `author-ink-color-binding/1`. Freeze the entire binding and
  its canonical `digest` when creating or changing the job. On retries, rendering,
  review and export, resolve current authority with `frozen=stored_binding`
- Require all modern source fields consistently: `ink_color_mode`,
  `ink_palette_id`, `ink_color_binding`, `ink_color_binding_sha256`. Reject partial
  or foreign source metadata; never recover it as automatic ink
- Resolve source-solid ink only for explicit `natural-layout-v1` and
  `m2_required`. Reject baseline/fallback combinations before any model work
- Preserve the existing auto-contrast branch verbatim. Calling the new primitive
  is opt-in only; it is never the implementation of default/legacy auto ink
- Finish natural layout and every resize first. Then call
  `recolor_strip(final_strip, resolved['rgb'])`, which returns `(strip, metadata)`
- Call `background_diagnostics(background_RGB, strip, (x, y))` before the service's
  existing single final `image.paste(strip, (x,y), strip)` operation. The diagnostic
  itself paints no pixels. No masked intermediate paste, halo, outline, opacity
  multiplier, erosion, alpha gamma or additional resize is permitted
- Persist the returned exact alpha hash/dimensions, compositor version, full
  color binding/digest, actual placement, background and output identities beside
  the existing M2 producer and layout receipt. A color change changes output
  identity and requires new image review, but leaves glyph producer/cache identity
  unchanged. Color assignment is deterministic postprocessing, not model training

## Authorization chain

Resolution independently reruns existing natural authorization for the exact
owned adapter, artifact, runtime and S03 snapshot. It checks both ink source-code
hashes, exact measurement/contract bytes and the selected profile's canonical
content digest. The actual snapshot's unique sample IDs, candidate IDs, parent
hash, exclusively train assignments and occurrence/label counts must agree with
the selected measured parent and current natural-trace evidence.

`profile_id=2` identifies the published output profile. `intake_style_id=1` is
independently recovered from the hash-verified snapshot index, binding and every
sample row. Current source admission is checked for the intake style, not silently
created for the output profile. The exact canonical `assets/<id>` references must
resolve to currently owned, enabled, approved rows. Normal source-admission policy
and ledger are revalidated. Source files must resolve within this owner's asset
pool and match the original source SHA256, byte count and dimensions. Missing,
revoked, foreign or altered originals fail closed. No caller path or caller RGB
is an authorization input.

A frozen binding includes the catalog revision, full profile and provenance,
current source-admission digest, natural binding digest, canonical train-parent
membership, numerical runtime identity and both implementation hashes. Any drift
requires a new job. The catalog is server-owned; changing enablement or another
entry also invalidates an earlier frozen catalog binding.

## What the pixels do and do not establish

Color assignment uses an exact constant apparent encoded-sRGB triplet and copies
every post-layout alpha byte, including the lowest nonzero values. Diagnostics
summarize actual supported local background pixels and the integer one-coverage
result. They report `UNCALIBRATED_REQUIRES_VISUAL_REVIEW`, with no readability PASS.
Encoded-RGB weighted brightness is explicitly a heuristic, not optical luminance.
No physical pigment, opacity, pressure, pen type or general author-color model is
claimed. Color does not fix the M2 glyph model's shape limitations.

## Verification boundary

Focused tests use generated in-memory strips, inert temporary byte fixtures and
disposable authority/DB fixtures. They exercise all 256 alpha values, both RGBs,
white/black/mixed background integer composition, unchanged outside-support
pixels, exact frozen binding, unsupported choices/combinations, owner and model
changes, train lineage, current source permissions/bytes and code/catalog/evidence
mutation. No model process or rendered-photo output is created by these tests.

Run from the source root with the existing backend environment:

    /workspace/scratch/64fc1a466651/dyyy_backend_testenv/bin/python -m unittest discover -s tests/m2_pipeline -p 'test_ink*.py' -v

Service default parity, actual create/PATCH/retry/review/export acceptance and
normal source-photo output review belong to the integration suite. Focused
primitive/authority passes do not replace those checks. Enable only a separately
recorded QA catalog derivative after code audit; preserve the disabled distribution
catalog and do not promote source profiles automatically.
