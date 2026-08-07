"""生成物(docs/)の検査。デプロイ前の最後の関門。

【なぜ回帰テストが必要か】
  このサイトには「消すと法令違反になる要素」と「消えても見た目では気づかない要素」が
  ある。特にFTC開示バーは全ページの最上部に必要で、テンプレートを1行消すだけで
  全28ページから同時に消えるのに、ブラウザで見ても違和感が無い。
  そういう壊れ方をするものだけをここで機械的に押さえる。

  デザインの良し悪しは見ない。「無いと困るものが無い」ことだけを検出する。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from xml.etree import ElementTree

log = logging.getLogger(__name__)

_CANONICAL_RE = re.compile(r'<link rel="canonical" href="([^"]+)"')
_HREF_RE = re.compile(r'href="([^"]+)"')
_ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_ANCHOR_HREF_RE = re.compile(r'href="([^"]+)"')

# テンプレートから消えてはいけない目印。値は「何のためにあるか」。
REQUIRED_MARKERS = {
    "disclosure-bar": "FTC開示バー(16 CFR Part 255)。全ページ必須",
}
# 本番の生成物に混ざってはいけない目印
FORBIDDEN_MARKERS = {
    "demo-bar": "--demo で生成した合成データ。公開すると架空の価格を掲載することになる",
}


def _external(href: str, base_url: str) -> bool:
    """自サイト宛の絶対URL(feed.xml など)は外部リンクではない。"""
    if not href.startswith(("http://", "https://")):
        return False
    return not href.startswith(base_url.rstrip("/"))


def verify_site(out_dir: Path, base_url: str) -> list[str]:
    """問題を列挙して返す。空リストなら公開してよい。"""
    problems: list[str] = []
    base_url = base_url.rstrip("/")

    if not (out_dir / "index.html").exists():
        return [f"{out_dir} に index.html がありません。先に生成してください"]

    pages = sorted(out_dir.rglob("*.html"))
    links_checked = 0

    for page in pages:
        name = page.relative_to(out_dir).as_posix()
        html = page.read_text(encoding="utf-8")

        for marker, why in REQUIRED_MARKERS.items():
            if marker not in html:
                problems.append(f"{name}: {marker} が無い — {why}")
        for marker, why in FORBIDDEN_MARKERS.items():
            if marker in html:
                problems.append(f"{name}: {marker} が混入している — {why}")

        canonical = _CANONICAL_RE.search(html)
        if not canonical:
            problems.append(f"{name}: canonical が無い")
        elif not canonical.group(1).startswith(base_url):
            # ローカル確認用のURLのまま公開すると、全ページが他所を正規URLとして指す
            problems.append(f"{name}: canonical が本番URLでない — {canonical.group(1)}")

        for href in _HREF_RE.findall(html):
            if href.startswith(("http://", "https://", "mailto:", "#")):
                continue
            links_checked += 1
            target = (page.parent / href).resolve()
            if target.is_dir():
                target = target / "index.html"
            if not target.exists():
                problems.append(f"{name}: リンク切れ — {href}")

        for anchor in _ANCHOR_RE.findall(html):
            href_match = _ANCHOR_HREF_RE.search(anchor)
            if not href_match or not _external(href_match.group(1), base_url):
                continue
            if "rel=" not in anchor:
                problems.append(f"{name}: 外部リンクに rel が無い — {anchor[:80]}")
            elif "cta-affiliate" in anchor and "sponsored" not in anchor:
                # 成果リンクなのに sponsored が付いていない = 未開示の広告
                problems.append(f"{name}: 成果リンクに rel=sponsored が無い — {anchor[:80]}")

    for xml_name in ("sitemap.xml", "feed.xml"):
        path = out_dir / xml_name
        if not path.exists():
            problems.append(f"{xml_name} が生成されていません")
            continue
        try:
            ElementTree.parse(path)
        except ElementTree.ParseError as e:
            problems.append(f"{xml_name}: XMLとして壊れています — {e}")

    robots = out_dir / "robots.txt"
    if not robots.exists():
        problems.append("robots.txt が生成されていません")
    elif "Sitemap:" not in robots.read_text(encoding="utf-8"):
        problems.append("robots.txt に Sitemap: 行がありません")

    log.info("検証: %dページ / 内部リンク%d本", len(pages), links_checked)
    return problems
