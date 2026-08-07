"""毎日の観測。全ツールの価格ページを巡回して履歴に記録する。

生成(render)と分離してあるのは、ネットワークに触る処理とHTMLを書く処理を
混ぜると、片方の失敗でもう片方が巻き添えになるため。
1つのツールの取得に失敗しても、他のツールと過去の履歴でサイトは成立する。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .catalog import Catalog
from .extract import extract
from .fetch import Fetcher
from .track import Snapshot, append_snapshot, last_ok, load_history, should_record, to_snapshot

log = logging.getLogger(__name__)


def collect(
    catalog: Catalog,
    fetcher: Fetcher,
    history_path: Path,
    now: datetime,
) -> tuple[dict[str, dict], int, int]:
    """全ツールを巡回する。

    戻り値: (latest.json に書く辞書, 記録した変更数, 取得に失敗した数)
    """
    history = load_history(history_path)
    latest: dict[str, dict] = {}
    recorded = 0
    failed = 0

    for tool in catalog.tools:
        result = fetcher.get(tool.pricing_url)

        if not result.ok:
            failed += 1
            log.warning("%s: 取得できませんでした (%s)", tool.slug, result.error)
            latest[tool.slug] = {
                "checked_at": now.isoformat(),
                "ok": False,
                "note": result.error,
                "http_status": result.status,
            }
            continue

        extraction = extract(result.html, tool.plans, tool.patterns)
        snapshot = to_snapshot(tool.slug, extraction, now)
        previous = last_ok(history.get(tool.slug, []))

        if not extraction.ok:
            failed += 1
            log.warning("%s: 価格を抽出できませんでした (%s)", tool.slug, extraction.note)
        elif should_record(previous, snapshot):
            append_snapshot(history_path, snapshot)
            history.setdefault(tool.slug, []).append(snapshot)
            recorded += 1
            if previous is None:
                log.info("%s: 初回記録 (%d プラン取得)", tool.slug, len(snapshot.plans))
            else:
                log.info(
                    "%s: 変更を検出 %s → %s",
                    tool.slug,
                    previous.signature or "(なし)",
                    snapshot.signature,
                )
        else:
            log.info("%s: 変更なし", tool.slug)

        resolved = len(extraction.resolved_plans)
        if extraction.ok and resolved < len(tool.plans):
            # 全プラン取れないのは普通だが、0件が続くならセレクタが死んでいる
            log.info(
                "%s: %d/%d プランのみ特定 (%s)",
                tool.slug,
                resolved,
                len(tool.plans),
                extraction.method,
            )

        latest[tool.slug] = {
            "checked_at": now.isoformat(),
            "ok": extraction.ok,
            "note": extraction.note,
            "http_status": result.status,
            "method": extraction.method,
            "plans_resolved": resolved,
            "plans_expected": len(tool.plans),
        }

    return latest, recorded, failed


def check(catalog: Catalog, fetcher: Fetcher) -> int:
    """--check 用。履歴を汚さずに、どのツールの抽出が壊れているかだけ報告する。

    プラン名の表記変更や料金ページのURL変更は必ず起きる。それを早く見つけるための道具。
    """
    broken = 0
    print(f"{'slug':<16} {'status':<8} {'plans':<9} method")
    print("-" * 56)

    for tool in catalog.tools:
        result = fetcher.get(tool.pricing_url)
        if not result.ok:
            broken += 1
            print(f"{tool.slug:<16} {'NG':<8} {'-':<9} {result.error}")
            continue

        extraction = extract(result.html, tool.plans, tool.patterns)
        resolved = len(extraction.resolved_plans)
        ratio = f"{resolved}/{len(tool.plans)}"

        if not extraction.ok or resolved == 0:
            broken += 1
            status = "NG"
        elif resolved < len(tool.plans):
            status = "PARTIAL"
        else:
            status = "OK"

        detail = extraction.method
        if extraction.note:
            detail = f"{detail} — {extraction.note}"
        print(f"{tool.slug:<16} {status:<8} {ratio:<9} {detail}")

        missing = [p.plan for p in extraction.plans if p.amount is None]
        if missing and status != "NG":
            print(f"{'':<16} {'':<8} 未検出プラン: {', '.join(missing)}")

    print("-" * 56)
    print(f"要対応: {broken} / {len(catalog.tools)} ツール")
    return broken


def snapshot_count(history: dict[str, list[Snapshot]]) -> int:
    return sum(len(v) for v in history.values())
