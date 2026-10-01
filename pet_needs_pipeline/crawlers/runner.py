"""사이트 × 키워드 수집 루프 + 체크포인트(이어하기)."""
import json
import logging
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from crawlers.naver_cafe import NaverCafeCrawler
from crawlers.naver_search import NaverBlogCrawler, NaverKinCrawler
from crawlers.reviews import ManualReviewCrawler, ShopReviewCrawler
from crawlers.utils import FailureLog, Polite
from crawlers.youtube import YoutubeCrawler

log = logging.getLogger("pipeline")

CRAWLERS = {c.name: c for c in (NaverBlogCrawler, NaverKinCrawler, NaverCafeCrawler,
                                YoutubeCrawler, ManualReviewCrawler, ShopReviewCrawler)}


class Checkpoint:
    """data/raw/checkpoint.json: {'done': {'site|keyword': n}, 'exhausted': [...], 'seen': {site: [hash]}}"""

    def __init__(self, path: Path):
        self.path = path
        self.state = json.loads(path.read_text("utf-8")) if path.exists() else {"done": {}, "exhausted": [], "seen": {}}

    def seen(self, site) -> set:
        return set(self.state["seen"].get(site, []))

    def update(self, site, keyword, n_new, seen: set, exhausted: bool):
        key = f"{site}|{keyword}"
        self.state["done"][key] = self.state["done"].get(key, 0) + n_new
        if exhausted and key not in self.state["exhausted"]:
            self.state["exhausted"].append(key)
        self.state["seen"][site] = sorted(seen)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)                               # 저장 도중 끊겨도 파일이 깨지지 않게

    def done(self, site, keyword) -> int:
        return self.state["done"].get(f"{site}|{keyword}", 0)

    def site_total(self, site) -> int:
        return sum(v for k, v in self.state["done"].items() if k.startswith(site + "|"))

    def is_exhausted(self, site, keyword) -> bool:
        return f"{site}|{keyword}" in self.state["exhausted"]


def keyword_plan(cfg: dict, site_cfg: dict) -> list[tuple[str, str, int]]:
    """(그룹, 키워드, 키워드별 목표) 목록. 사이트 target × scale 을 group_ratio로 나눈다."""
    target = site_cfg.get("target", 0) * cfg.get("scale", 1)
    kw_map = site_cfg.get("keywords") or cfg["keywords"]
    plan = []
    for group, ratio in cfg["group_ratio"].items():
        kws = kw_map.get(group, [])
        if kws:
            per_kw = math.ceil(target * ratio / len(kws))
            plan += [(group, kw, per_kw) for kw in kws]
    return plan


def run_collection(cfg: dict) -> None:
    raw_dir = Path(cfg["paths"]["raw"])
    raw_dir.mkdir(parents=True, exist_ok=True)
    ckpt = Checkpoint(raw_dir / "checkpoint.json")
    polite, fails = Polite(cfg), FailureLog(cfg["paths"]["logs"])
    test_cap = cfg["test_limit_per_site"] if cfg.get("test_mode") else None

    for site, site_cfg in cfg["sites"].items():
        if not site_cfg.get("enabled") or site not in CRAWLERS:
            continue
        seen = ckpt.seen(site)
        try:
            crawler = CRAWLERS[site](cfg, site_cfg, polite, fails, seen)
        except Exception as e:
            fails.add(site, "", "", f"크롤러 생성 실패: {e}")
            continue
        out_csv = raw_dir / f"{site}.csv"
        log.info("== %s 수집 시작 (현재 %d건)", site, ckpt.site_total(site))
        try:
            for group, kw, per_kw in keyword_plan(cfg, site_cfg):
                total = ckpt.site_total(site)
                if test_cap is not None and total >= test_cap:
                    break
                need = per_kw - ckpt.done(site, kw)
                if test_cap is not None:
                    need = min(need, test_cap - total)
                if need <= 0 or ckpt.is_exhausted(site, kw):
                    continue
                df = crawler.collect(kw, need)
                if not df.empty:
                    df["query_group"] = group
                    df["collected_at"] = datetime.now().isoformat(timespec="seconds")
                    df.to_csv(out_csv, mode="a", header=not out_csv.exists(), index=False, encoding="utf-8-sig")
                ckpt.update(site, kw, len(df), crawler.seen, crawler.exhausted)
                log.info("  [%s] %-20s +%d (누적 %d) 탈락=%s", site, kw, len(df), ckpt.site_total(site),
                         dict(crawler.reject))
                crawler.reject.clear()
        except KeyboardInterrupt:
            log.warning("사용자 중단 — 체크포인트까지 저장됨. 다시 실행하면 이어서 수집합니다.")
            raise
        except Exception as e:
            fails.add(site, "", "", f"사이트 수집 중단: {e}")
        finally:
            crawler.close()


def load_raw(cfg: dict) -> pd.DataFrame:
    """A 코퍼스(견주 고민 분석)로 읽을 원본. config corpus.sources 에 든 source(파일명)만 읽는다.
    corpus.sources 가 없으면(옛 설정) data/raw/*.csv 전부."""
    allowed = (cfg.get("corpus") or {}).get("sources")
    files = [f for f in Path(cfg["paths"]["raw"]).glob("*.csv")
             if not f.stem.endswith(("_meta", "_v1", "_removed")) and (allowed is None or f.stem in allowed)]
    frames = [pd.read_csv(f, encoding="utf-8-sig", dtype={"rating": float}) for f in files]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
