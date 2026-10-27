# Recorded validation and limits

The checked suite is **216 Python cases and 6 JavaScript cases**. The original synthetic PDF and controlled vector fixtures provide explicit geometry oracles. The reported SI dimensions are designed values or declared synthetic assumptions, not recovered or independently verified building dimensions.

## Focused room case

Run `python -m scripts.demonstrate_room_rebuild` from the repository root. The workflow uses actual PDF extraction and existing calibration/assembly code, then reconstructs the actual stale room. No expected candidate or area is injected into detection.

| Step | Recorded result |
| --- | --- |
| Original source face | Bounds `(100,100)–(300,400)`; native area 60,000 points² |
| Declared synthetic calibration | 4 m / 400 points and 3 m / 300 points → 0.01 m/point |
| Initial nominal area | 6.0 m² |
| Move left wall to x=110 | Original dependent area withheld |
| Explicitly reconstruct | Bounds `(110,100)–(300,400)`; native area 57,000 points² |
| Recalculate | 5.7 m², still `needs_review` and `manual_unverified` |
| Boundary provenance | 620 drawing points of automatic source-stroke runs, 300 of manual wall geometry, 60 of explicit hypothesis closure |
| Refreshed model | 11 active entities, 2 retired snapshots, 25 relationships; restored export matches |

The area change is −0.3 m². A further wall edit invalidates the reconstructed room again. Tests cover open boundaries, centred/off-centre splits, possible merges, holes, stale bridges, scale changes, evidence lineage, persistence and UI selection/reconstruction.

The compact [room report](../support/evaluation/reports/room_rebuild.json) and [evaluation report](../support/evaluation/reports/stage6.json) retain machine-readable evidence and implementation fingerprints.

## Detection benchmark

The dataset contains **14 synthetic pages**, of which **13 have geometry labels**. The original raster page is unlabelled and is not treated as a negative architectural example. The layouts are small and related; they are not a representative real-plan sample.

One-to-one matching maximises cardinality, then similarity. Room matching uses polygon IoU thresholds 0.50 and 0.75. Strict wall/opening matching uses Hausdorff distance ≤0.01 PDF point at labelled run granularity. Unresolved candidates still count as predictions. F1 is a micro aggregate; empty/unavailable labels are null rather than perfect scores.

| Metric | TP / predictions / labels | F1 |
| --- | --- | ---: |
| Rooms, IoU ≥0.50 | 19 / 20 / 23 | 0.883721 |
| Rooms, IoU ≥0.75 | 15 / 20 / 23 | 0.697674 |
| Strict wall runs | 69 / 82 / 80 | 0.851852 |
| Possible openings | 3 / 3 / 3 | 1.000000 |
| Wall connectivity | 82 / 98 / 103 | 0.815920 |
| Room boundary links | 75 / 87 / 97 | 0.815217 |
| Room adjacency | 7 / 7 / 11 | 0.777778 |

Length MAE/P95 is **0/0 m over 69 matched values**, out of 80 wall labels and 82 predictions. Area MAE/P95 is **1.263158/6 m² over 19 matched values**, out of 23 room labels and 20 predictions. P95 uses linear interpolation at `(n−1)×0.95`. These errors are conditional on matched objects with SI labels/values; false/missed objects do not become zero error.

Four merged faces match one intended room each at IoU 0.50, explaining the stricter-score decline and area error. One decorative false room remains. All normal benchmark measurements stay needs-review because reference dimensions are assumptions. Opening/flank support does not establish door/window semantics: confirmed hosts are zero.

## Failure injection

| Population | Material cases | Detected | Blocked | Escalated | Measurement-review passes | Engineering passes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Original scenarios | 12 | 9 | 12 | 3 | 0 | 0 |
| False-provenance declaration probe | 1 | 0 | 0 | 0 | 1 | 0 |
| Combined | 13 | 9 | 12 | 3 | **1/13** | **0/13** |

Escalated cases are also blocked; these columns overlap. Blocking unavailable evidence is not detection of the material error. The three undetectable original cases are consistently wrong scale, consistently wrong units and an entirely absent source partition. Their nominal geometry/calculations can remain internally consistent, so independent correctness or completeness is left unresolved.

The added probe deliberately supplies false physical-evidence metadata with different declared groups, records and hashes. It actually passes measurement review because the underlying records are not authenticated. This residual is reported rather than hidden by the original 0/12 gate-pass result. Engineering handoff is explicitly unimplemented, so zero engineering passes does not establish safety.

## Missing coverage

- **Zero independent real architectural drawings/pages/SI labels have been evaluated.** Source/license metadata was identified, but original downloads were blocked. The [access inventory](../support/evaluation/real-plan-inventory-stage6.json) records this.
- Enabled browser inventory was empty. AppTest exercises the upload branch, actual geometry, component payloads, review/correction, export and fresh sessions; it does not exercise a real file dialog or browser visual rendering.
- Node tests exercise actual picker handlers at different zoom levels/scrolled bounds. Python tests verify affine overlay alignment across CropBoxes, rotations and render sizes.
- Raster extraction/OCR, general structural completeness, authenticated physical evidence, confirmed opening semantics, multi-page building assembly and engineering handoff are outside the demonstrated scope.

For a permitted independent input, `python -m scripts.prepare_independent_evaluation --help` describes the original-byte/permission import and blank annotation workflow. It never copies candidates into ground truth. Human annotation must be independent of detector output, and SI labels require verified dimensions and explicit provenance.
