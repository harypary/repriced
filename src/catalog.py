"""config/tools.yaml を読み込み、壊れた設定を起動時に落とす。

設定ミスは「サイトは生成できたが中身が間違っている」という一番たちの悪い
壊れ方をする。価格情報を扱う以上そこは読者の信用に直結するので、
おかしい設定は生成前に例外で止める。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ConfigError(Exception):
    """tools.yaml / site.yaml の内容が不正。"""


@dataclass(frozen=True)
class Affiliate:
    program: str  # none | direct | partnerstack | impact | ...
    url: str
    terms: str

    @property
    def is_monetized(self) -> bool:
        """成果リンクが実際に発行済みか。

        program が埋まっていても url が空なら承認待ち。その状態で
        rel="sponsored" を付けると「広告でないものを広告と申告する」ことになるし、
        FTCの開示文も嘘になるので、判定は必ず url の有無で行う。
        """
        return bool(self.url)


@dataclass(frozen=True)
class Tool:
    slug: str
    name: str
    vendor: str
    category: str
    homepage: str
    pricing_url: str
    plans: tuple[str, ...]
    currency: str
    affiliate: Affiliate
    # プラン名 → 正規表現。近傍探索が外れるツールだけ書く(通常は空)
    patterns: dict[str, str]

    @property
    def outbound_url(self) -> str:
        """記事から外部へ飛ばすときのURL。成果リンクが無ければ公式サイト。"""
        return self.affiliate.url or self.homepage

    @property
    def rel(self) -> str:
        # sponsored は金銭が絡むリンクにのみ付ける。ただの参照リンクは nofollow だけ。
        return "nofollow sponsored noopener" if self.affiliate.is_monetized else "nofollow noopener"


@dataclass(frozen=True)
class Catalog:
    tools: tuple[Tool, ...]
    categories: dict[str, str]
    comparisons: tuple[tuple[str, str], ...]

    def by_slug(self, slug: str) -> Tool:
        for t in self.tools:
            if t.slug == slug:
                return t
        raise KeyError(slug)

    def in_category(self, key: str) -> list[Tool]:
        return [t for t in self.tools if t.category == key]

    @property
    def monetized(self) -> list[Tool]:
        return [t for t in self.tools if t.affiliate.is_monetized]


def _require_https(url: str, where: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigError(f"{where}: https:// のURLである必要があります → {url!r}")
    return url


def load_catalog(tools_path: Path, site_cfg: dict) -> Catalog:
    raw = yaml.safe_load(tools_path.read_text(encoding="utf-8"))

    categories = raw.get("categories") or {}
    if not isinstance(categories, dict) or not categories:
        raise ConfigError("tools.yaml: categories が空です")

    tools: list[Tool] = []
    seen: set[str] = set()

    for i, entry in enumerate(raw.get("tools") or []):
        where = f"tools.yaml tools[{i}]"
        slug = str(entry.get("slug", "")).strip()
        if not SLUG_RE.match(slug):
            raise ConfigError(f"{where}: slug は英小文字・数字・ハイフンのみ → {slug!r}")
        if slug in seen:
            raise ConfigError(f"{where}: slug が重複しています → {slug!r}")
        seen.add(slug)

        category = str(entry.get("category", ""))
        if category not in categories:
            raise ConfigError(
                f"{where} ({slug}): category {category!r} が categories に未定義です"
            )

        plans = tuple(str(p) for p in (entry.get("plans") or []))
        if not plans:
            raise ConfigError(f"{where} ({slug}): plans が空です。追跡対象のプラン名が必要です")

        # 抽出パターンは起動時にコンパイルして検証する。
        # 壊れた正規表現を巡回中に踏むと、そのツールだけ黙って値が消える
        patterns = {}
        for plan, pattern in (entry.get("patterns") or {}).items():
            if plan not in plans:
                raise ConfigError(
                    f"{where} ({slug}): patterns の {plan!r} は plans に存在しません"
                )
            try:
                compiled = re.compile(pattern)
            except re.error as e:
                raise ConfigError(f"{where} ({slug}) patterns.{plan}: 正規表現が不正です — {e}")
            if compiled.groups != 1:
                raise ConfigError(
                    f"{where} ({slug}) patterns.{plan}: 金額を捉えるグループを1つだけ書いてください"
                )
            patterns[str(plan)] = pattern

        aff_raw = entry.get("affiliate") or {}
        aff_url = str(aff_raw.get("url", "") or "")
        if aff_url:
            _require_https(aff_url, f"{where} ({slug}) affiliate.url")

        tools.append(
            Tool(
                slug=slug,
                name=str(entry["name"]),
                vendor=str(entry.get("vendor", "")),
                category=category,
                homepage=_require_https(str(entry["homepage"]), f"{where} ({slug}) homepage"),
                pricing_url=_require_https(
                    str(entry["pricing_url"]), f"{where} ({slug}) pricing_url"
                ),
                plans=plans,
                currency=str(entry.get("currency", "USD")),
                patterns=patterns,
                affiliate=Affiliate(
                    program=str(aff_raw.get("program", "none")),
                    url=aff_url,
                    terms=str(aff_raw.get("terms", "") or ""),
                ),
            )
        )

    if not tools:
        raise ConfigError("tools.yaml: tools が空です")

    comparisons: list[tuple[str, str]] = []
    for pair in site_cfg.get("comparisons") or []:
        if len(pair) != 2:
            raise ConfigError(f"site.yaml comparisons: 2要素の組で書いてください → {pair!r}")
        a, b = str(pair[0]), str(pair[1])
        for slug in (a, b):
            if slug not in seen:
                raise ConfigError(f"site.yaml comparisons: 未知の slug → {slug!r}")
        if a == b:
            raise ConfigError(f"site.yaml comparisons: 同じツール同士の比較です → {a!r}")
        comparisons.append((a, b))

    return Catalog(tools=tuple(tools), categories=categories, comparisons=tuple(comparisons))
