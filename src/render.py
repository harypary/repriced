"""ページデータを静的サイト(docs/)として書き出す。

出力先が docs/ なのは GitHub Pages の慣例に合わせているだけで、
実際のデプロイは GitHub Actions が artifact として直接アップロードする。
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .catalog import Catalog
from .pages import ComparePage, FeedItem, ToolPage, grouped_by_category, related

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


def _fmt_date(value: datetime | None) -> str:
    """英語圏向けの日付表記。%-d は Windows で動かないので手で組む。"""
    if value is None:
        return "—"
    return f"{value:%b} {value.day}, {value.year}"


def _fmt_rfc822(value: datetime) -> str:
    return format_datetime(value)


def _fmt_iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["date"] = _fmt_date
    env.filters["rfc822"] = _fmt_rfc822
    env.filters["iso"] = _fmt_iso
    return env


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def render_site(
    catalog: Catalog,
    tool_pages: list[ToolPage],
    compare_pages: list[ComparePage],
    feed: list[FeedItem],
    all_changes: list[FeedItem],
    cfg: dict,
    base_url: str,
    out_dir: Path,
    now: datetime,
    google_site_verification: str = "",
    demo: bool = False,
) -> None:
    env = _env()
    base_url = base_url.rstrip("/")

    ctx = {
        "site": cfg["site"],
        "generation": cfg["generation"],
        "base_url": base_url,
        "now": now,
        "google_site_verification": google_site_verification,
        "demo": demo,
        # ページ末尾の「このサイトについて」に出す実績値。
        # 手で書いた数字は必ず古くなるので、必ず生成時に数える。
        "stats": {
            "tools": len(tool_pages),
            "changes": len([i for i in all_changes if i.change.kind != "first_seen"]),
            "tracking_since": min(
                (p.state.tracked_since for p in tool_pages if p.state.tracked_since),
                default=None,
            ),
        },
    }

    # docs/ は毎回まっさらにする。設定から外したツールのページを残さないため。
    # 手書きファイルをここに置かないこと(site_root/ を使う)。
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # ---- トップページ: 最近の価格変更が主役 ----
    _write(
        out_dir / "index.html",
        env.get_template("index.html").render(
            **ctx,
            root="",
            feed=feed,
            grouped=grouped_by_category(catalog, tool_pages),
            history_rows=cfg["generation"]["history_rows"],
        ),
    )

    # ---- ツール別ページ ----
    tool_tpl = env.get_template("tool.html")
    for page in tool_pages:
        _write(
            out_dir / "tools" / page.tool.slug / "index.html",
            tool_tpl.render(
                **ctx,
                root="../../",
                page=page,
                related=related(page, tool_pages),
                history_rows=cfg["generation"]["history_rows"],
            ),
        )

    # ---- 比較ページ(site.yaml に書いた組み合わせだけ) ----
    compare_tpl = env.get_template("compare.html")
    for page in compare_pages:
        _write(
            out_dir / "compare" / page.slug / "index.html",
            compare_tpl.render(**ctx, root="../../", page=page),
        )

    # ---- 全変更ログ ----
    _write(
        out_dir / "changes" / "index.html",
        env.get_template("changes.html").render(**ctx, root="../", items=all_changes),
    )

    # ---- 固定ページ ----
    # methodology は飾りではない。「どう集めた数字か」を公開しているかどうかが
    # 2026年のGoogleが見ている一次情報の証拠になる。消さないこと。
    for name in ("methodology", "disclosure"):
        _write(
            out_dir / name / "index.html",
            env.get_template(f"{name}.html").render(
                **ctx, root="../", tool_pages=tool_pages, catalog=catalog
            ),
        )

    # ---- 機械向け ----
    for name in ("sitemap.xml", "feed.xml", "robots.txt"):
        _write(
            out_dir / name,
            env.get_template(name).render(
                **ctx, tool_pages=tool_pages, compare_pages=compare_pages, feed=feed
            ),
        )

    shutil.copytree(ROOT / "static", out_dir / "assets")

    # Search Console の所有権確認ファイルや ads.txt など、
    # ルート直下でなければならない非生成物はここで配る。
    site_root = ROOT / "site_root"
    if site_root.is_dir():
        for item in site_root.iterdir():
            if item.is_file() and not item.name.startswith("."):
                shutil.copy2(item, out_dir / item.name)
                log.info("ルート直下に配置: %s", item.name)

    # Jekyll のビルドを止める。無いと _ 始まりのパスが404になることがある
    (out_dir / ".nojekyll").touch()

    log.info(
        "出力完了: %s (ツール%d / 比較%d / 変更%d)",
        out_dir,
        len(tool_pages),
        len(compare_pages),
        ctx["stats"]["changes"],
    )
