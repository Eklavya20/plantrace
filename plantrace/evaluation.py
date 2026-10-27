"""Offline evaluation over extracted geometry. Labels never enter the detector.

Matching maximises cardinality first, then similarity, using a deterministic
min-cost augmenting-path assignment. Unlabelled metrics are null, not zero.
"""

import math
from statistics import mean

from shapely.geometry import shape
from plantrace.measurements import approval_blockers


def match_one_to_one(scores, threshold):
    """Return (prediction, truth, score); handle non-greedy competing matches."""
    n = len(scores)
    m = len(scores[0]) if n else 0
    if any(len(row) != m for row in scores):
        raise ValueError("Similarity matrix must be rectangular.")
    if any(not math.isfinite(v) or not 0 <= v <= 1 for row in scores for v in row):
        raise ValueError("Similarities must be finite in [0, 1].")
    if not 0 < threshold <= 1:
        raise ValueError("Threshold must be in (0, 1].")
    # Residual graph: source -> predictions -> truth -> sink. Bellman-Ford
    # handles negative reverse edges and reassigns earlier matches as needed.
    source, sink = n + m, n + m + 1
    graph = [[] for _ in range(sink + 1)]
    def edge(a, b, cost):
        graph[a].append([b, 1, cost, len(graph[b])])
        graph[b].append([a, 0, -cost, len(graph[a]) - 1])
    for i in range(n):
        edge(source, i, 0)
    for j in range(m):
        edge(n + j, sink, 0)
    for i, row in enumerate(scores):
        for j, score in enumerate(row):
            if score >= threshold:
                edge(i, n + j, -score)
    while True:
        distances, previous = [math.inf] * len(graph), [None] * len(graph)
        distances[source] = 0
        for _ in range(len(graph) - 1):
            changed = False
            for u, edges in enumerate(graph):
                if not math.isfinite(distances[u]):
                    continue
                for k, (v, capacity, cost, _) in enumerate(edges):
                    if capacity and distances[u] + cost < distances[v] - 1e-12:
                        distances[v] = distances[u] + cost
                        previous[v] = (u, k)
                        changed = True
            if not changed:
                break
        if previous[sink] is None:
            break
        v = sink
        while v != source:
            u, k = previous[v]
            item = graph[u][k]
            item[1] -= 1
            graph[v][item[3]][1] += 1
            v = u
    return [(i, e[0] - n, scores[i][e[0] - n]) for i in range(n) for e in graph[i]
            if n <= e[0] < n + m and e[1] == 0]


def counts(tp, predicted, labelled):
    fp, fn = predicted - tp, labelled - tp
    return {"tp": tp, "fp": fp, "fn": fn, "predictions": predicted, "labels": labelled,
            "precision": tp / predicted if predicted else None,
            "recall": tp / labelled if labelled else None,
            "f1": 2 * tp / (predicted + labelled) if predicted + labelled else None}


def error_summary(errors, labelled, eligible):
    errors = sorted(errors)
    p = (len(errors) - 1) * .95
    p95 = (errors[math.floor(p)] * (1 - p % 1) + errors[math.ceil(p)] * (p % 1)) if errors else None
    return {"samples": len(errors), "labelled_objects": labelled, "eligible_predictions": eligible,
            "mae": mean(errors) if errors else None, "p95_linear": p95,
            "absolute_errors": errors}


def evaluate_page(scene, topology, measurements, labels, room_iou=.5, line_tolerance=.01):
    """Labels: GeoJSON objects with stable IDs; null lists mean unavailable.

    Walls use exact run granularity: endpoint Hausdorff <= tolerance. Detection
    metrics include unresolved candidates. Review gating is reported separately.
    SI error is conditional on matched, available measurements, with coverage.
    """
    results, mappings = {}, {}
    values = {m.candidate_id: m for m in measurements.measurements}
    for kind in ("room", "wall", "opening"):
        predicted = scene.list_candidates(kind)
        truth = labels.get(kind + "s")
        if truth is None:
            results[kind] = {"available": False, "reason": "Ground truth unavailable", "predictions": len(predicted)}
            continue
        actual = [c.geometry.as_shapely() for c in predicted]
        expected = [shape(c["geometry"]) for c in truth]
        identities = [c["id"] for c in truth]
        if len(set(identities)) != len(identities) or any(not isinstance(identity,str) or not identity for identity in identities):
            raise ValueError("Ground-truth IDs must be unique nonempty strings per class.")
        if any(t.get("value_si") is not None and (not math.isfinite(t["value_si"]) or t["value_si"] <= 0) for t in truth):
            raise ValueError("SI labels must be positive finite values or null.")
        if any(not g.is_valid or g.is_empty or g.geom_type != ("Polygon" if kind == "room" else "LineString") for g in expected):
            raise ValueError("Ground truth contains invalid geometry or wrong geometry types.")
        scores = [[a.intersection(b).area / a.union(b).area if kind == "room"
                   else float(a.hausdorff_distance(b) <= line_tolerance) for b in expected] for a in actual]
        matched = match_one_to_one(scores, room_iou if kind == "room" else 1)
        mappings[kind] = {predicted[i].id: truth[j]["id"] for i, j, _ in matched}
        metric = {"available": True, **counts(len(matched), len(predicted), len(truth)),
                  "matches": [{"candidate_id": predicted[i].id, "label_id": truth[j]["id"], "similarity": score} for i, j, score in matched]}
        if kind == "room":
            metric["matched_polygon_iou_mean"] = mean(s for _, _, s in matched) if matched else None
        if kind != "opening":
            available_si = [t for t in truth if t.get("value_si") is not None]
            errors = [abs(values[predicted[i].id].value_si - truth[j]["value_si"]) for i, j, _ in matched
                      if truth[j].get("value_si") is not None and predicted[i].id in values and values[predicted[i].id].value_si is not None]
            metric["si_errors"] = error_summary(errors, len(available_si), sum(values[c.id].value_si is not None for c in predicted if c.id in values))
            metric["si_errors"]["unit"] = "m2" if kind == "room" else "m"
            matched_indices = {i for i, _, _ in matched}
            metric["approval_gate"] = {
                "eligible": sum(not approval_blockers(values[c.id]) for c in predicted if c.id in values),
                "eligible_unmatched_predictions": sum(i not in matched_indices and not approval_blockers(values[c.id]) for i,c in enumerate(predicted) if c.id in values),
                "materially_wrong_matched_values_eligible": sum(
                    truth[j].get("value_si") is not None and truth[j]["value_si"] > 0 and values[predicted[i].id].value_si is not None and
                    abs(values[predicted[i].id].value_si / truth[j]["value_si"] - 1) > .05 and not approval_blockers(values[predicted[i].id])
                    for i,j,_ in matched if predicted[i].id in values),
                "material_error_threshold_relative": .05}
        results[kind] = metric
    for kind, left, right, unordered in [("wall_connectivity", "wall", "wall", True),
                                       ("room_adjacency", "room", "room", True),
                                       ("room_boundary", "room", "wall", False),
                                       ("opening_host", "opening", "wall", False)]:
        expected = labels.get(kind)
        predicted = [r for r in topology.relationships if r.kind == kind]
        if expected is None:
            results[kind] = {"available": False, "reason": "Relationship labels unavailable", "predictions": len(predicted)}
            continue
        # Unmatched endpoints remain unique FP edges, never silently dropped.
        def key(a, b):
            return tuple(sorted((a, b))) if unordered else (a, b)
        predicted_keys = {key(mappings.get(left, {}).get(r.from_candidate_id, "unmatched:" + r.from_candidate_id),
                              mappings.get(right, {}).get(r.to_candidate_id, "unmatched:" + r.to_candidate_id)) for r in predicted}
        truth_keys = {key(*pair) for pair in expected}
        valid_left={t["id"] for t in labels.get(left+"s",[]) or []}
        valid_right={t["id"] for t in labels.get(right+"s",[]) or []}
        if len(truth_keys)!=len(expected) or any(a not in valid_left or b not in valid_right for a,b in expected):
            raise ValueError("Relationship labels must be unique and reference available object IDs.")
        results[kind] = {"available": True, **counts(len(predicted_keys & truth_keys), len(predicted_keys), len(truth_keys)),
                         "interpretation": "possible geometric flank associations; not confirmed door/window hosts" if kind == "opening_host" else "geometry-derived relationships"}
    assessments = measurements.assessments
    host_labels=labels.get("opening_host")
    manual_hosts=[r for r in topology.relationships if r.kind=="opening_host" and r.assignment_origin=="manual_supported_assignment"]
    correct_assignments=sum((mappings.get("opening",{}).get(r.from_candidate_id),mappings.get("wall",{}).get(r.to_candidate_id)) in
                            {tuple(pair) for pair in host_labels or []} for r in manual_hosts)
    results["opening_host_assignments"]={"labels_available":host_labels is not None,
        "opening_labels":len(labels["openings"]) if labels.get("openings") is not None else None,
        "manual_assignments":len(manual_hosts),"correct_supported_assignments":correct_assignments if host_labels is not None else None,
        "accuracy_on_assignments":correct_assignments/len(manual_hosts) if manual_hosts and host_labels is not None else None,
        "confirmed_assignments":sum(r.status!="unresolved" for r in manual_hosts),
        "unresolved_opening_candidates":sum(c.status=="unresolved" for c in scene.list_candidates("opening")),
        "confirmed_host_accuracy":None,"reason":"Only possible flank support is labelled here; no confirmed semantic door/window host truth or automatic confirmation."}
    results["calibration"] = {"regions": len(assessments), "conflicting": sum(a.status == "conflicting" for a in assessments),
                              "status_counts": {s: sum(a.status == s for a in assessments) for s in sorted({a.status for a in assessments})},
                              "relative_scale_errors": [abs(a.metres_per_source_unit / labels["scale_m_per_unit"] - 1)
                                                        for a in assessments if a.metres_per_source_unit is not None] if labels.get("scale_m_per_unit") else None}
    results["calibration"]["reference_residuals_m"]=[abs(r.residual_metres) for a in assessments for r in a.residuals]
    results["review"] = {"candidates": len(scene.candidates), "unresolved": sum(c.status == "unresolved" for c in scene.candidates),
                         "measurements": len(values), "needs_review": sum(m.verification_state == "needs_review" for m in values.values()),
                         "unavailable_si": sum(m.value_si is None for m in values.values()),
                         "inferred_measurements": sum(m.has_inferred_boundaries for m in values.values())}
    results["review"]["unresolved_rate"] = results["review"]["unresolved"] / len(scene.candidates) if scene.candidates else None
    results["review"]["needs_review_rate"] = results["review"]["needs_review"] / len(values) if values else None
    results["diagnostics"] = sorted({d.code for d in scene.diagnostics})
    return results
