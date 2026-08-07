"""価格履歴の蓄積と差分検出。このプロジェクトの中核。

【なぜ履歴がサイトの資産になるか】
  現在価格は誰でも公式サイトを見れば分かるので差別化にならない。
  「いつ、いくらから、いくらに変わったか」は記録し続けた者しか持てない。
  後発が今日から始めても、去年の値上げは永久に埋められない。
  だから data/prices.jsonl は絶対に消さないこと。これがこのサイトの堀。

【ファイル構成】
  data/prices.jsonl … 変化があった時だけ追記する観測記録。append-only。
                      毎日追記しないのは、ノイズで履歴が読めなくなるため。
  data/latest.json  … 毎回上書きする最終チェック結果。
                      「最後に確認したのはいつか」を正確に出すために必要。

  変更一覧は prices.jsonl から毎回再計算する。差分ロジックを直したときに
  過去分も自動で正しくなるので、変更内容そのものは保存しない。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .extract import Extraction, PlanPrice

log = logging.getLogger(__name__)

UTC = timezone.utc

# 表示に使う変更種別。テンプレート側の見出しと1対1で対応している。
KIND_INCREASE = "increase"
KIND_DECREASE = "decrease"
KIND_ADDED = "added"
KIND_REMOVED = "removed"
KIND_PAGE = "page"
KIND_FIRST = "first_seen"


@dataclass(frozen=True)
class Snapshot:
    ts: str
    slug: str
    ok: bool
    signature: str
    plans: dict[str, dict]
    method: str = ""
    note: str = ""

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.ts)

    def amount(self, plan: str) -> float | None:
        entry = self.plans.get(plan)
        return entry.get("amount") if entry else None


@dataclass(frozen=True)
class Change:
    ts: str
    slug: str
    kind: str
    plan: str = ""
    before: float | None = None
    after: float | None = None
    period: str = ""

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.ts)

    @property
    def pct(self) -> float | None:
        if self.before in (None, 0) or self.after is None:
            return None
        return (self.after - self.before) / self.before * 100.0

    def _money(self, value: float | None) -> str:
        if value is None:
            return "—"
        if value == 0:
            return "Free"
        return "$" + f"{value:,.2f}".rstrip("0").rstrip(".")

    @property
    def before_display(self) -> str:
        return self._money(self.before)

    @property
    def after_display(self) -> str:
        return self._money(self.after)


# ---------------------------------------------------------------------
# 読み書き
# ---------------------------------------------------------------------
def load_history(path: Path) -> dict[str, list[Snapshot]]:
    """prices.jsonl を slug ごとの時系列に読み込む。

    壊れた行は落として続行する。1行の破損で全履歴を失うのは割に合わない。
    """
    history: dict[str, list[Snapshot]] = {}
    if not path.exists():
        return history

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            snap = Snapshot(
                ts=raw["ts"],
                slug=raw["slug"],
                ok=bool(raw.get("ok", True)),
                signature=raw.get("signature", ""),
                plans=raw.get("plans") or {},
                method=raw.get("method", ""),
                note=raw.get("note", ""),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning("%s:%d を読み飛ばしました (%s)", path.name, lineno, e)
            continue
        history.setdefault(snap.slug, []).append(snap)

    for snaps in history.values():
        snaps.sort(key=lambda s: s.ts)
    return history


def append_snapshot(path: Path, snap: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(snap), ensure_ascii=False, sort_keys=True) + "\n")


def save_latest(path: Path, latest: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(latest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_latest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("%s が壊れています。空として続行します", path.name)
        return {}


# ---------------------------------------------------------------------
# 観測 → 記録
# ---------------------------------------------------------------------
def to_snapshot(slug: str, extraction: Extraction, now: datetime) -> Snapshot:
    plans = {
        p.plan: {"amount": p.amount, "period": p.period, "confidence": p.confidence}
        for p in extraction.plans
        if p.amount is not None
    }
    return Snapshot(
        ts=now.isoformat(),
        slug=slug,
        ok=extraction.ok,
        signature=extraction.signature,
        plans=plans,
        method=extraction.method,
        note=extraction.note,
    )


def should_record(previous: Snapshot | None, current: Snapshot) -> bool:
    """履歴に追記すべきか。

    毎日追記すると1年で数千行になり、人間にもクローラにも読めない履歴になる。
    「変わった瞬間」だけを残すことで、履歴そのものが読み物になる。
    """
    if not current.ok:
        return False  # 取得失敗は履歴を汚さない。latest.json 側に記録する
    if previous is None:
        return True
    if previous.signature != current.signature:
        return True
    # 金額が同じでも課金周期の表記が変わったら記録する。
    # $33/月 と $33/年 は読者にとって全く違う情報で、シグネチャは
    # 金額だけから作るのでこれを見ないと永久に古い表記が残る。
    for plan, now in current.plans.items():
        was = previous.plans.get(plan)
        if was and was.get("period") != now.get("period"):
            return True
    # シグネチャが同じでも、抽出できたプランが増えた場合は記録する
    # (前回は取りこぼしていた、というだけなので値上げ扱いにはならない)
    return set(current.plans) - set(previous.plans) != set()


def last_ok(snaps: list[Snapshot]) -> Snapshot | None:
    for snap in reversed(snaps):
        if snap.ok:
            return snap
    return None


# ---------------------------------------------------------------------
# 差分検出
# ---------------------------------------------------------------------
def diff(previous: Snapshot, current: Snapshot) -> list[Change]:
    changes: list[Change] = []
    plans_before, plans_after = previous.plans, current.plans

    for plan in sorted(set(plans_before) | set(plans_after)):
        before = plans_before.get(plan, {}).get("amount")
        after = plans_after.get(plan, {}).get("amount")
        period = (plans_after.get(plan) or plans_before.get(plan) or {}).get("period", "")

        if before is None and after is not None:
            kind = KIND_ADDED
        elif before is not None and after is None:
            kind = KIND_REMOVED
        elif before == after or before is None:
            continue
        else:
            kind = KIND_INCREASE if after > before else KIND_DECREASE

        changes.append(
            Change(
                ts=current.ts,
                slug=current.slug,
                kind=kind,
                plan=plan,
                before=before,
                after=after,
                period=period,
            )
        )

    if not changes and previous.signature != current.signature:
        # プラン単位の差は取れなかったが、ページ上の価格集合は確かに変わっている。
        # 「何が」まで言えないので、断定せず「変更を検出」とだけ記録する。
        changes.append(Change(ts=current.ts, slug=current.slug, kind=KIND_PAGE))

    # シグネチャが同じなら金額は1円も動いていない。課金周期の表記だけが
    # 変わった場合がこれに当たるが、それは抽出側を直したときにも起きる。
    # ベンダーが何かしたとは限らないので、何も言わない。
    # 記録は残す(データは直る)が、変更イベントとしては公開しない。
    return changes


def build_changes(snaps: list[Snapshot]) -> list[Change]:
    """1ツールの全履歴から変更一覧を作る(新しい順)。"""
    ok_snaps = [s for s in snaps if s.ok]
    if not ok_snaps:
        return []

    changes: list[Change] = [
        Change(ts=ok_snaps[0].ts, slug=ok_snaps[0].slug, kind=KIND_FIRST)
    ]
    for previous, current in zip(ok_snaps, ok_snaps[1:]):
        changes.extend(diff(previous, current))
    changes.sort(key=lambda c: c.ts, reverse=True)
    return changes


# ---------------------------------------------------------------------
# 表示用の状態
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class ToolState:
    slug: str
    plans: tuple[PlanPrice, ...]
    verified_at: datetime | None  # 最後に「この価格で合っている」と確認できた時刻
    current_since: datetime | None  # 今の価格になった時刻(最後に変化した日)
    last_checked: datetime | None  # 最後に取得を試みた時刻(失敗含む)
    ok: bool
    stale: bool
    note: str
    changes: tuple[Change, ...]
    observations: int

    @property
    def has_prices(self) -> bool:
        return any(p.amount is not None for p in self.plans)

    @property
    def tracked_since(self) -> datetime | None:
        if not self.changes:
            return None
        return self.changes[-1].when

    @property
    def last_change(self) -> Change | None:
        for change in self.changes:
            if change.kind != KIND_FIRST:
                return change
        return None


def build_state(
    slug: str,
    plan_names: tuple[str, ...],
    snaps: list[Snapshot],
    latest_entry: dict,
    now: datetime,
    stale_after_days: int,
) -> ToolState:
    """履歴と最終チェック結果から、テンプレートに渡す状態を組み立てる。

    「確認できた日」と「価格が変わった日」を必ず分けること。
    履歴(prices.jsonl)は変化があった時しか追記しないので、履歴の最終行を
    確認日として使うと、値段を据え置いているツールほど古く見えてしまう。
    毎日確認した事実は latest.json 側にしか無い。
    """
    newest = last_ok(snaps)
    current_since = newest.when if newest else None

    last_checked = None
    if latest_entry.get("checked_at"):
        last_checked = datetime.fromisoformat(latest_entry["checked_at"])

    # 今日の巡回で取得できていればそれが確認日。できていなければ、
    # 確認できた最後の日 = 履歴の最終行まで遡る。
    if latest_entry.get("ok") and last_checked is not None:
        verified_at = last_checked
    else:
        verified_at = current_since

    # 「最後に価格を確認できた日」から stale_after_days を超えたら価格を伏せる。
    # 古い価格を現在価格として出し続けるのは、このサイトでは最もやってはいけないこと。
    stale = verified_at is None or (now - verified_at) > timedelta(days=stale_after_days)

    if stale:
        plans = tuple(PlanPrice(name, None) for name in plan_names)
    else:
        recorded = newest.plans if newest else {}
        plans = tuple(
            PlanPrice(
                plan=name,
                amount=(recorded.get(name) or {}).get("amount"),
                period=(recorded.get(name) or {}).get("period", ""),
                confidence=(recorded.get(name) or {}).get("confidence", "none"),
            )
            for name in plan_names
        )

    return ToolState(
        slug=slug,
        plans=plans,
        verified_at=verified_at,
        current_since=current_since,
        last_checked=last_checked,
        ok=bool(latest_entry.get("ok", False)),
        stale=stale,
        note=str(latest_entry.get("note", "") or ""),
        changes=tuple(build_changes(snaps)),
        observations=len([s for s in snaps if s.ok]),
    )
