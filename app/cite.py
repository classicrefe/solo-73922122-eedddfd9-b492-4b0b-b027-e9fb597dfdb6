"""引用页生成：用已公开的汇总记录拼出所需口径的算式。

规则（与公开页/内部台的产品约定一致）：
- 只能使用公开页上真实存在、且由使用者勾选的发布编号（1~20 条）；
- 每条算式对所选记录的总数做加/减，**同一记录在每条算式中至多出现一次**
  （系数 ∈ {-1, 0, 1}），不同算式之间可共用出处；
- 按公开人数表中的实际调查格核对：目标格（所选分区 × 年龄组交集）净计 1 次，
  其余调查格净计 0 次；
- 整份报告用到的不同发布编号数最少；相同时算式中的加减项总数最少；
- 任一范围拼不出，则整份报告不生成引用页，并逐项给出原因。

生成后的引用页保存快照（发布编号 + 系数 + 当时总数）。新增发布不会改变
快照；再次打开时按原编号和原算式复算，并与当前公开记录逐条核对。
"""

from __future__ import annotations

from datetime import datetime, timezone

from z3 import Bool, Context, If, Int, Optimize, Or, Solver, Sum, sat

MAX_PUBLICATIONS = 20
MAX_RANGES = 3
COEFF_LIMIT = 1  # 每条算式里同一记录至多一次


class CitationError(ValueError):
    """请求本身不合法（未知编号、空范围等），对应 HTTP 400。"""


# ---------------- 基础计算 ----------------


def _cell_matrix(cells: list[dict], publications: list[dict]) -> list[list[int]]:
    """每条发布记录 → 各调查格是否被该记录口径覆盖（0/1 向量）。"""
    matrix = []
    for pub in publications:
        ds = set(pub["districts"])
        ags = set(pub["age_groups"])
        matrix.append(
            [1 if (c["district"] in ds and c["age_group"] in ags) else 0 for c in cells]
        )
    return matrix


def _target_vector(cells: list[dict], districts: list[str], age_groups: list[str]) -> list[int]:
    ds, ags = set(districts), set(age_groups)
    return [1 if (c["district"] in ds and c["age_group"] in ags) else 0 for c in cells]


def _target_cells(cells: list[dict], districts: list[str], age_groups: list[str]) -> list[dict]:
    ds, ags = set(districts), set(age_groups)
    return [c for c in cells if c["district"] in ds and c["age_group"] in ags]


def _range_key(districts: list[str], age_groups: list[str]) -> tuple:
    return (tuple(sorted(set(districts))), tuple(sorted(set(age_groups))))


def _label(districts: list[str], age_groups: list[str]) -> str:
    return f"{'、'.join(districts)} / {'、'.join(age_groups)}"


# ---------------- 单范围可行性 ----------------


def _single_range_feasible(counts: list[list[int]], target: list[int]) -> bool:
    ctx = Context()
    cs = [Int(f"c_{r}", ctx=ctx) for r in range(len(counts))]
    s = Solver(ctx=ctx)
    for c in cs:
        s.add(c >= -COEFF_LIMIT, c <= COEFF_LIMIT)
    for i, t in enumerate(target):
        s.add(Sum([cs[r] for r in range(len(counts)) if counts[r][i]]) == t)
    return s.check() == sat


def _coverage(cells: list[dict], counts: list[list[int]], selected: list[dict],
              target: list[int]) -> tuple[list[dict], list[dict]]:
    """按格列出目标格的覆盖情况，返回 (覆盖明细, 未被覆盖的格)。"""
    covered, uncovered = [], []
    for i, t in enumerate(target):
        if not t:
            continue
        ids = [pub["id"] for pub, row in zip(selected, counts) if row[i]]
        item = {"district": cells[i]["district"], "age_group": cells[i]["age_group"],
                "publication_ids": ids}
        covered.append(item)
        if not ids:
            uncovered.append(item)
    return covered, uncovered


# ---------------- 全报告最优拼合 ----------------


def _build_optimize(counts: list[list[int]], targets: list[list[int]]):
    ctx = Context()
    n_ranges, n_pubs = len(targets), len(counts)
    coef = [[Int(f"c_{j}_{r}", ctx=ctx) for r in range(n_pubs)] for j in range(n_ranges)]
    used = [Bool(f"u_{r}", ctx=ctx) for r in range(n_pubs)]

    def add_constraints(opt: Optimize) -> None:
        for j in range(n_ranges):
            for r in range(n_pubs):
                opt.add(coef[j][r] >= -COEFF_LIMIT, coef[j][r] <= COEFF_LIMIT)
            for i, t in enumerate(targets[j]):
                terms = [coef[j][r] for r in range(n_pubs) if counts[r][i]]
                opt.add(Sum(terms) == t)
        for r in range(n_pubs):
            opt.add(used[r] == Or([coef[j][r] != 0 for j in range(n_ranges)]))

    return ctx, coef, used, add_constraints


def _solve_combined(counts: list[list[int]], targets: list[list[int]]) -> list[list[int]]:
    """返回每条算式对各记录的系数；无解返回空列表。

    第一目标：不同发布编号最少；第二目标：加减项总数最少。
    """
    ctx, coef, used, add_constraints = _build_optimize(counts, targets)

    opt = Optimize(ctx=ctx)
    add_constraints(opt)
    distinct = Sum([If(u, 1, 0) for u in used])
    h_used = opt.minimize(distinct)
    if opt.check() != sat:
        return []
    min_used = opt.lower(h_used).as_long()

    opt2 = Optimize(ctx=ctx)
    add_constraints(opt2)
    opt2.add(Sum([If(u, 1, 0) for u in used]) == min_used)
    terms = Sum([
        If(coef[j][r] != 0, 1, 0)
        for j in range(len(targets)) for r in range(len(counts))
    ])
    h_terms = opt2.minimize(terms)
    if opt2.check() != sat:
        return []
    opt2.lower(h_terms)  # 迫使求最优；随后 model() 即为最优模型
    model = opt2.model()
    plan: list[list[int]] = []
    for j in range(len(targets)):
        plan.append([model.eval(coef[j][r], model_completion=True).as_long()
                     for r in range(len(counts))])
    return plan


def _verify_plan(counts: list[list[int]], targets: list[list[int]],
                 plan: list[list[int]]) -> None:
    """独立核对：逐格净计必须等于目标（范围内 1、范围外 0）。"""
    for j, target in enumerate(targets):
        for i, t in enumerate(target):
            net = sum(plan[j][r] * counts[r][i] for r in range(len(counts)))
            if net != t:
                raise RuntimeError(
                    f"内部校验失败：范围 {j + 1} 的格 {i} 净计 {net}，应为 {t}"
                )


# ---------------- 对外：生成 ----------------


def plan_report(cells: list[dict], publications: list[dict],
                publication_ids: list[int], ranges: list[dict]) -> dict:
    """校验请求并尝试拼合。

    成功：{"feasible": True, "snapshot": {...}, "ranges": [...]}
    失败：{"feasible": False, "ranges": [逐项诊断]}
    请求不合法：抛 CitationError。
    """
    if not cells:
        raise CitationError("尚未导入调查数据")

    ids: list[int] = []
    for pid in publication_ids:
        if pid not in ids:
            ids.append(pid)
    if not 1 <= len(ids) <= MAX_PUBLICATIONS:
        raise CitationError(f"须勾选 1~{MAX_PUBLICATIONS} 条已公开记录")
    by_id = {p["id"]: p for p in publications}
    missing = [pid for pid in ids if pid not in by_id]
    if missing:
        raise CitationError(f"发布编号不存在：{missing}")

    if not 1 <= len(ranges) <= MAX_RANGES:
        raise CitationError(f"须填写 1~{MAX_RANGES} 个汇总范围")

    known_d = {c["district"] for c in cells}
    known_a = {c["age_group"] for c in cells}
    normalized: list[dict] = []
    seen: set[tuple] = set()
    for pos, rng in enumerate(ranges, start=1):
        ds = list(dict.fromkeys(rng.get("districts", [])))
        ags = list(dict.fromkeys(rng.get("age_groups", [])))
        if not ds or not ags:
            raise CitationError(f"第 {pos} 个范围须至少选择一个分区和一个年龄组")
        bad = [d for d in ds if d not in known_d] + [a for a in ags if a not in known_a]
        if bad:
            raise CitationError(f"第 {pos} 个范围含未知的分区或年龄组：{bad}")
        target = _target_vector(cells, ds, ags)
        if not any(target):
            raise CitationError(f"第 {pos} 个范围的交集不含任何调查格")
        key = _range_key(ds, ags)
        if key in seen:
            raise CitationError(f"第 {pos} 个范围与前面的范围重复")
        seen.add(key)
        normalized.append({
            "position": pos,
            "districts": ds,
            "age_groups": ags,
            "target": target,
        })

    selected = sorted((by_id[pid] for pid in ids), key=lambda p: p["id"])
    counts = _cell_matrix(cells, selected)
    targets = [rng["target"] for rng in normalized]

    # ---- 先逐项判定；任一拼不出就整体不生成 ----
    diagnostics = []
    all_feasible = True
    for rng in normalized:
        covered, uncovered = _coverage(cells, counts, selected, rng["target"])
        feasible = _single_range_feasible(counts, rng["target"])
        reasons = []
        if not feasible:
            all_feasible = False
            if uncovered:
                names = "、".join(f"{u['district']} / {u['age_group']}" for u in uncovered)
                reasons.append(f"目标格未被任何所选记录覆盖：{names}")
            else:
                reasons.append("所选记录无法在“同一记录至多一次”的规则下拼出该范围")
        diagnostics.append({
            "position": rng["position"],
            "districts": rng["districts"],
            "age_groups": rng["age_groups"],
            "label": _label(rng["districts"], rng["age_groups"]),
            "target_cells": len([t for t in rng["target"] if t]),
            "population_total": sum(c["population"] for c in _target_cells(cells, rng["districts"], rng["age_groups"])),
            "feasible": feasible,
            "reasons": reasons,
            "coverage": covered,
        })

    if not all_feasible:
        return {"feasible": False, "ranges": diagnostics}

    # ---- 全报告联合求最优 ----
    plan = _solve_combined(counts, targets)
    if not plan:
        # 每个范围单独可行，但联合受限不符（理论上 ≤3 个范围不会发生）。
        for item in diagnostics:
            item["feasible"] = False
            item["reasons"] = ["所选记录无法同时满足全部范围"]
        return {"feasible": False, "ranges": diagnostics}

    _verify_plan(counts, targets, plan)

    used_indices = sorted({r for row in plan for r, c in enumerate(row) if c != 0})
    used_publications = [selected[r] for r in used_indices]

    snap_ranges = []
    out_ranges = []
    for j, rng in enumerate(normalized):
        terms = []
        total = 0
        for r, c in enumerate(plan[j]):
            if c == 0:
                continue
            terms.append({"publication_id": selected[r]["id"], "coefficient": c})
            total += c * int(selected[r]["total"])
        terms.sort(key=lambda t: (t["publication_id"], t["coefficient"]))
        pop_total = sum(
            c["population"]
            for c in _target_cells(cells, rng["districts"], rng["age_groups"])
        )
        snap_ranges.append({
            "districts": rng["districts"],
            "age_groups": rng["age_groups"],
            "label": _label(rng["districts"], rng["age_groups"]),
            "target_cells": len([t for t in rng["target"] if t]),
            "population_total": pop_total,
            "total": total,
            "terms": terms,
        })
        out_ranges.append(snap_ranges[-1])

    snapshot = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "publications": [
            {
                "id": p["id"],
                "districts": p["districts"],
                "age_groups": p["age_groups"],
                "total": p["total"],
                "created_at": p["created_at"],
            }
            for p in used_publications
        ],
        "ranges": snap_ranges,
    }

    return {
        "feasible": True,
        "publication_count": len(used_publications),
        "term_count": sum(len(r["terms"]) for r in snap_ranges),
        "ranges": out_ranges,
        "snapshot": snapshot,
    }


# ---------------- 对外：复算 ----------------


def verify_snapshot(cells: list[dict], publications: list[dict], snapshot: dict) -> dict:
    """按快照中的原编号、原算式复算，并核对当前公开人数表。

    返回 {"ok": bool, "problems": [...], "ranges": [...]}。
    """
    by_id = {p["id"]: p for p in publications}
    problems: list[str] = []
    checked_ranges = []

    for idx, rng in enumerate(snapshot.get("ranges", []), start=1):
        total = 0
        for term in rng.get("terms", []):
            pub = by_id.get(term["publication_id"])
            if pub is None:
                problems.append(f"第 {idx} 个范围引用的发布编号 #{term['publication_id']} 已不存在")
                continue
            total += int(term["coefficient"]) * int(pub["total"])
        target = _target_vector(cells, rng["districts"], rng["age_groups"])
        counts = _cell_matrix(cells, [
            by_id[t["publication_id"]] for t in rng["terms"] if t["publication_id"] in by_id
        ])
        coeffs = [t["coefficient"] for t in rng["terms"] if t["publication_id"] in by_id]
        # 逐格核对覆盖：范围内应为 1，范围外应为 0。
        for i, expected in enumerate(target):
            net = sum(c * counts[r][i] for r, c in enumerate(coeffs))
            if net != expected:
                problems.append(
                    f"第 {idx} 个范围在格「{cells[i]['district']} / {cells[i]['age_group']}」"
                    f"净计 {net}，应为 {expected}"
                )
        if total != int(rng.get("total", 0)):
            problems.append(
                f"第 {idx} 个范围按原编号复算为 {total}，快照记录为 {rng.get('total')}"
            )
        checked_ranges.append({
            "index": idx,
            "label": rng.get("label") or _label(rng["districts"], rng["age_groups"]),
            "recomputed_total": total,
            "recorded_total": int(rng.get("total", 0)),
        })

    return {"ok": not problems, "problems": problems, "ranges": checked_ranges}
