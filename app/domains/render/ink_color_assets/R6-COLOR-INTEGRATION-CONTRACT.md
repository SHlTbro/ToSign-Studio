# Color-only integration proposal

The measured source photos support two distinct train-source palette choices: magenta `#C41AB4` and violet-blue `#2F0CDD`. They do not support a single blended author color. The lighter `#448EE9` is from the validation parent and remains a separately labeled descriptive comparison in this study.

## What is established

R4 `service.py` chooses only black or white from the whole region's mean encoded-RGB brightness, with threshold 140. It cannot reproduce the original colored lettering. This weighted encoded-RGB number is a heuristic, not calibrated optical luminance.

The current natural-layout path retains the full glyph coverage, adds no outline, and uses proper alpha composition for intermediate strips. Its final `RGB_image.paste(RGBA_strip, xy, RGBA_strip)` is a single correct coverage blend. There is no demonstrated natural-path double-alpha defect.

The historical baseline `_compose_strip` instead self-masks an RGBA paste onto transparency. A source pixel `(195,25,179,128)` becomes `(98,13,90,64)` before the final photograph blend. That squares coverage and attenuates RGB. Reproductions on all three genuine cached glyphs match rounded alpha-squared exactly. The outline path additionally expands the footprint. These findings are preserved as diagnostics; R4 source was not changed.

## Implementation boundary

Expose the following separate, explicit render setting without changing the trained adapter or glyph-generation request:

- Existing `auto-contrast-v1`: retain the current behavior and exact legacy default compatibility
- New `source-solid-srgb-v1`: choose one server-validated palette identity from the authorized style's source evidence

Suggested immutable request fields are `ink_color_mode`, `ink_palette_id`, and server-resolved `ink_color_binding`. A binding includes schema/version, exact integer RGB, source-image hash, palette-measurement hash, palette-role/split, alpha policy, transfer policy, and profile digest. Do not accept arbitrary file paths or treat a caller-supplied hash as evidence of ownership. Bind the palette to the same authorized user/style/source lineage that discovery advertised.

Color selection and glyph geometry are independent. Never silently change layout mode, seed, spacing, size, model reference or generation seed because a color was chosen. The first bounded demonstration uses the already accepted R4 natural layout. A product path that still goes through the old self-mask/outline compositor must explicitly implement a coverage-correct, outline-free source-solid route before advertising support; otherwise reject that combination clearly. Do not silently route a requested baseline layout into natural layout.

The color-only operation is small:

1. Complete the existing selected layout, including its uniform fitting and all resampling
2. Read the final strip's alpha channel without thresholding, erosion, dilation, gamma, or opacity changes
3. Create a constant-RGB image from the selected apparent sRGB swatch and attach the identical alpha bytes
4. Composite once onto a copy of the actual RGB background with the existing final Pillow source-over path

The isolated reference implementation is `ink_color_contract.py`. It validates three exact uint8 channels, a recognized profile, fixed opacity 1, source/measurement identities, and clipping-safe placement. It does not provide application authorization or publication policy; those belong to the product integration.

Assigning color after layout avoids repeated premultiply/unpremultiply rounding of very low-alpha RGB during intermediate RGBA resizes. The final alpha still includes the project's normal resampling; the color operation changes none of those bytes. There is no invented pressure variation, grain, colored edge map, physical pigment simulation, or random per-character hue change.

## Review and provenance

Freeze the selected color in job creation, retry and PATCH snapshots. Discovery/DTO/start/render/export must agree on the same binding. A color-only PATCH changes output identity and invalidates the prior image review; it does not invalidate or relabel the underlying cached M2 producer receipt.

Each slot execution should record:

- Color mode, palette ID, full resolved binding and its digest
- Color implementation/version and composite policy
- Exact post-layout alpha hash and dimensions, alpha-preserved flag, no-outline flag
- Background identity, actual placement, output identity, and existing layout/source provenance
- A clear distinction between model-produced M2 glyph geometry and the subsequent deterministic color operation

Review/export must verify every visible slot's current color binding and output bytes, in addition to the existing source/layout checks. Tampered, missing, stale, cross-user or unsupported modern color bindings must block. Historical all-absent color metadata can retain the documented historical auto-color meaning; partially present modern metadata must not fall back silently.

## Visibility and uncertainty

The native-pixel sheets show real tradeoffs. Saturated violet-blue has weak brightness separation on dark photo areas. Light blue can merge into cyan/white details. Even a region-average black/white decision can miss local structure. Show the actual background preview and provide a non-destructive warning or review prompt; do not silently recolor individual strokes, add a halo, darken the chosen swatch, or manufacture a contrast PASS. A numerical visibility threshold has not been calibrated in this study.

Original photos have no embedded color profile and do not identify lighting, physical ink, substrate, or original image processing. The sampled RGBs are apparent display colors under an sRGB assumption. Original mixed edge pixels cannot be inverted into pigment opacity or a pressure map. All 27 canonical sample RGB planes are white; their alpha contains coverage, and original hue must be obtained from the verified original images.

The project M2 model's glyph-style limitations remain. Recoloring improves source-palette fidelity but is not new model training or evidence of stronger generalization.

## Required product regressions

- All 256 alpha values remain exact through color assignment, including alpha 1–11 and thin low-alpha ends
- Exact one-coverage blend on white, black and mixed RGB backgrounds; outside-support pixels remain identical
- Same alpha/layout/producer metadata across palette choices; no model call or inference-cache key change
- No added halo, clipping, implicit opacity multiplier, hue jitter or source-image-specific code branch
- Exact default/off pixel parity; explicitly supported combinations only
- Color choice survives immutable retry/PATCH binding and is visible in per-slot metadata
- A changed color invalidates approval; matching current color/output review permits export; stale/tampered/cross-owner color proof blocks
- Actual normal API/PATCH/review/export exercise on source-photo copies before product readiness

The isolated study passes 11 primitive/project-helper tests. A second verifier independently reproduces all 12 full-photo output PNGs with integer source-over arithmetic, verifies the three original M2 producer receipts, and rehashes all 356 frozen R4 source files. This proves the bounded engineering renders and source preservation; it does not substitute for the product API acceptance above.

## Evidence

- Palette `palette_measurements/palette_candidates.json`: `2511cf55b5fa8bf3cbbc2a38636352c677bb1c030965567e3242b25ec0ae2bc0`
- Comparison manifest: `9b78d963a80dcfa026d27ee8e46045f2176eba75e7824f6ab188f1988fd2addc`
- Fixed geometry alpha: `c5f2277c3dc3a542e8e24ee6492f43c99be8a051f18d9c7e60dfc21835cefaa5`
- Independent comparison verification: `e35fc3d18f1e3e1ba1971455f6fe7df866c4165c89a34289c5e52fbcd361ef98`

The 12 full-photo images and three native-pixel comparison sheets are in `photo-comparisons-v1`. Columns are the current auto black/white mode, train magenta, train violet-blue, and validation-source light blue. Each full photo contains two additional rows in previously unlettered regions. Existing original handwriting and background pixels outside the new alpha support are unchanged. The placement labels describe study regions, not an automatic placement policy or a guarantee of uniform local brightness.
