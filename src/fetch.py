"""価格ページの取得。robots.txt の遵守とレート制限を強制する。

他社のサーバを毎日叩き続ける以上、行儀の悪いクローラだと判断された時点で
IPごとブロックされてサイトが死ぬ。ここは「速く取る」より
「何年も取り続けられる」ことを優先して書いてある。

  - robots.txt を必ず確認し、Disallow なら取得しない
  - Crawl-delay が指定されていればそちらを尊重する
  - 同一ホストへは最低 interval_sec 秒あける
  - User-Agent で名乗り、連絡先URLを含める(苦情の窓口が無いと問答無用で弾かれる)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger(__name__)

# 取得を諦める上限。価格ページ1枚にこれ以上かかるならその日は諦めて
# last-known-good を使う。CI の実行時間を守るため。
MAX_ATTEMPTS = 3
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class FetchResult:
    url: str
    ok: bool
    status: int | None
    html: str
    error: str = ""

    @property
    def blocked_by_robots(self) -> bool:
        return self.error.startswith("robots")


class Fetcher:
    def __init__(self, user_agent: str, interval_sec: float = 2.0, timeout_sec: int = 20) -> None:
        self.user_agent = user_agent
        self.interval_sec = max(interval_sec, 1.0)  # 1秒未満は許可しない
        self.timeout_sec = timeout_sec
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    # ---- レート制限 ------------------------------------------------
    def _wait(self, host: str, delay: float) -> None:
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    # ---- robots.txt ------------------------------------------------
    def _robots_for(self, url: str) -> RobotFileParser | None:
        """ホストの robots.txt を取得して解析する(ホストごとに1回だけ)。

        RFC 9309 の区分に厳密に従う。ここは直感と逆なので注意すること。

          200        … 内容に従う
          4xx        … 「Unavailable」(§2.3.1.3)。robots.txt が存在しない場合と
                        同じ扱いで、制限なし。401/403 も含む。
                        禁止だと解釈しがちだが、RFC も Google の実装も「制限なし」
          5xx / 通信不能 … 「Unreachable」(§2.3.1.4)。判断材料が無いので全面禁止。
                        その日は取得を諦め、last-known-good を使う

        None を返した場合は制限なしの意味。
        """
        parts = urlsplit(url)
        host = parts.netloc
        if host in self._robots:
            return self._robots[host]

        robots_url = urlunsplit((parts.scheme, host, "/robots.txt", "", ""))
        parser: RobotFileParser | None = None
        try:
            self._wait(host, self.interval_sec)
            res = self._session.get(robots_url, timeout=self.timeout_sec)
            if res.status_code == 200:
                parser = RobotFileParser()
                parser.parse(res.text.splitlines())
            elif res.status_code >= 500:
                parser = RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
                log.warning(
                    "%s: robots.txt が %d。到達不能とみなし今回は取得しません",
                    host,
                    res.status_code,
                )
            else:
                # 4xx。robots.txt が無いのと同じで制限なし
                log.info("%s: robots.txt が %d のため制限なしとして扱います", host, res.status_code)
        except requests.RequestException as e:
            # 到達不能。判断材料が無い状態で叩きに行かない
            parser = RobotFileParser()
            parser.parse(["User-agent: *", "Disallow: /"])
            log.warning("%s: robots.txt に到達できません (%s)。今回は取得しません", host, e)

        self._robots[host] = parser
        return parser

    def _allowed(self, url: str) -> tuple[bool, float]:
        """(取得してよいか, 待つべき秒数) を返す。"""
        parser = self._robots_for(url)
        if parser is None:
            return True, self.interval_sec
        if not parser.can_fetch(self.user_agent, url):
            return False, self.interval_sec

        delay = self.interval_sec
        # Crawl-delay / Request-rate が指定されていれば必ずそちらに従う。
        # 相手が明示している以上、こちらの設定値より優先されるべき。
        crawl_delay = parser.crawl_delay(self.user_agent)
        if crawl_delay:
            delay = max(delay, float(crawl_delay))
        rate = parser.request_rate(self.user_agent)
        if rate and rate.requests > 0:
            delay = max(delay, rate.seconds / rate.requests)
        return True, delay

    # ---- 本体 ------------------------------------------------------
    def get(self, url: str) -> FetchResult:
        allowed, delay = self._allowed(url)
        if not allowed:
            log.warning("robots.txt により取得禁止: %s", url)
            return FetchResult(url, False, None, "", "robots.txt により取得が禁止されています")

        host = urlsplit(url).netloc
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait(host, delay)
            try:
                res = self._session.get(url, timeout=self.timeout_sec)
            except requests.RequestException as e:
                last_error = f"{type(e).__name__}: {e}"
                log.warning("取得失敗 (%d/%d) %s: %s", attempt, MAX_ATTEMPTS, url, last_error)
                delay = min(delay * 2, 30.0)
                continue

            if res.status_code in RETRY_STATUS:
                last_error = f"HTTP {res.status_code}"
                # 429 で Retry-After が来たら必ず従う。無視するとBAN行き。
                retry_after = res.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, min(float(retry_after), 60.0))
                else:
                    delay = min(delay * 2, 30.0)
                log.warning("取得失敗 (%d/%d) %s: %s", attempt, MAX_ATTEMPTS, url, last_error)
                continue

            if res.status_code != 200:
                return FetchResult(url, False, res.status_code, "", f"HTTP {res.status_code}")

            # requests は Content-Type に charset が無いと latin-1 を仮定して文字化けする
            if res.encoding is None or "charset" not in (res.headers.get("Content-Type") or ""):
                res.encoding = res.apparent_encoding or "utf-8"
            return FetchResult(url, True, res.status_code, res.text)

        return FetchResult(url, False, None, "", last_error or "取得に失敗しました")
