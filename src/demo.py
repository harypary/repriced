"""ネットワークに触れずに、それらしい履歴を合成する。

このサイトは「履歴が溜まってはじめて意味が出る」構造なので、
公開初日のサイトは変更ログが空で、見た目の判断ができない。
デモモードは半年後の姿を先に見るためのもの。

  - 乱数は slug から決定的に作る。実行のたびに変わると差分が読めない
  - data/ には一切書き込まない。本物の履歴を汚さないため
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .catalog import Catalog, Tool
from .track import Snapshot

# プラン名からそれらしい価格帯を作るための目安(USD/月)。
# 位置が後ろのプランほど高い、という料金表の一般的な構造を再現する。
_TIERS = (0.0, 12.0, 20.0, 30.0, 60.0, 99.0, 200.0)


def _rng(*parts: str) -> float:
    """0.0〜1.0 の決定的な擬似乱数。"""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF


def _base_prices(tool: Tool) -> dict[str, float]:
    prices: dict[str, float] = {}
    for i, plan in enumerate(tool.plans):
        lowered = plan.lower()
        if "free" in lowered or (i == 0 and lowered in ("hobby", "launch", "starter")):
            prices[plan] = 0.0
            continue
        if "enterprise" in lowered or "custom" in lowered:
            continue  # 問い合わせ型は価格が出ない。本番でもここは空になる
        tier = _TIERS[min(i, len(_TIERS) - 1)]
        # ±20% ばらつかせて $X9 に丸める(SaaSの実際の価格の付き方に寄せる)
        jitter = 0.8 + _rng(tool.slug, plan) * 0.4
        prices[plan] = max(5.0, round(tier * jitter / 10) * 10 - 1)
    return prices


def build_demo_history(
    catalog: Catalog, now: datetime, months: int = 10
) -> tuple[dict[str, list[Snapshot]], dict[str, dict]]:
    history: dict[str, list[Snapshot]] = {}
    latest: dict[str, dict] = {}
    start = now - timedelta(days=months * 30)

    for tool in catalog.tools:
        prices = _base_prices(tool)
        snaps: list[Snapshot] = [_snapshot(tool, prices, start)]

        # このツールが期間中に何回価格を動かすか(0〜3回)
        events = int(_rng("events", tool.slug) * 4)
        for n in range(events):
            offset = 0.15 + 0.8 * ((n + 1) / (events + 1))
            when = start + timedelta(days=int(months * 30 * offset))
            mutated = _mutate(tool, prices, n)
            if mutated == prices:
                # 丸めの結果として同額に戻ることがある。そのまま記録すると
                # 「変わったが何が変わったか言えない(page)」という偽の行が出る
                continue
            prices = mutated
            snaps.append(_snapshot(tool, prices, when))

        history[tool.slug] = snaps
        latest[tool.slug] = {
            "checked_at": now.isoformat(),
            "ok": True,
            "note": "",
            "method": "demo",
            "plans_resolved": len(prices),
            "plans_expected": len(tool.plans),
        }

    return history, latest


def _reprice(before: float, factor: float) -> float:
    """改定後の価格。必ず元の値と変わることを保証する。

    $X9 に丸めるのは実際のSaaSの価格の付き方に寄せるためだが、
    安いプランに10ドル刻みを当てると丸め戻って同額になり、
    「変わったのに何も変わっていない」履歴が出来てしまう。
    """
    step = 10.0 if before >= 30 else 1.0
    after = round(before * factor / step) * step
    if step == 10.0:
        after -= 1.0
    if after == before:
        after = before + step * (1.0 if factor > 1 else -1.0)
    return max(4.0, after)


def _mutate(tool: Tool, prices: dict[str, float], seed: int) -> dict[str, float]:
    """価格改定を1回起こす。値上げが多く、たまに値下げや新プラン追加。"""
    updated = dict(prices)
    paid = [p for p, v in updated.items() if v > 0]
    if not paid:
        return updated

    roll = _rng("mutate", tool.slug, str(seed))
    plan = paid[int(_rng("plan", tool.slug, str(seed)) * len(paid))]

    if roll < 0.12 and len(tool.plans) > len(updated):
        # 未収録のプランが料金表に載った
        for candidate in tool.plans:
            if candidate not in updated:
                updated[candidate] = _reprice(max(updated.values()), 1.5)
                return updated

    factor = 0.8 if roll < 0.28 else 1.15 + roll * 0.35
    updated[plan] = _reprice(updated[plan], factor)
    return updated


def _snapshot(tool: Tool, prices: dict[str, float], when: datetime) -> Snapshot:
    plans = {
        plan: {"amount": amount, "period": "month", "confidence": "high"}
        for plan, amount in prices.items()
    }
    signature = hashlib.sha256(
        "|".join(f"{k}:{v}" for k, v in sorted(prices.items())).encode()
    ).hexdigest()[:16]
    return Snapshot(
        ts=when.isoformat(),
        slug=tool.slug,
        ok=True,
        signature=signature,
        plans=plans,
        method="demo",
    )
