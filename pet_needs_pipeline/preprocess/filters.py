"""강아지/고양이 판별, 기간 필터, 급여 방식 태깅 (수집 단계와 전처리 단계에서 같이 쓴다)."""
from datetime import datetime, timedelta

import pandas as pd


def count_terms(text: str, terms) -> int:
    t = (text or "").lower()
    return sum(t.count(w.lower()) for w in terms)


def animal_check(text: str, cfg: dict, require_dog_word: bool = True) -> str:
    """'dog' | 'mixed' | 'cat' | 'none'. 고양이 단어가 강아지 단어보다 많으면 cat."""
    d = count_terms(text, cfg["animal"]["dog_terms"])
    c = count_terms(text, cfg["animal"]["cat_terms"])
    if c > d:
        return "cat"
    if d == 0:
        return "none" if require_dog_word else "dog"
    return "mixed" if c > 0 else "dog"


def cutoff_date(cfg: dict) -> pd.Timestamp:
    return pd.Timestamp(datetime.now() - timedelta(days=cfg["period_days"])).normalize()


def in_period(date, cfg: dict) -> bool:
    if date is None or pd.isna(date):
        return False
    d = pd.Timestamp(date)
    return cutoff_date(cfg) <= d <= pd.Timestamp(datetime.now())


def feed_type(text: str, cfg: dict) -> str:
    """생식 / 화식 / 건사료 / 혼합 / 미상. 한 방식 언급이 나머지 합의 2배 이상이면 그 방식."""
    counts = {k: count_terms(text, words) for k, words in cfg["feed_type_dict"].items()}
    counts = {k: v for k, v in counts.items() if v > 0}
    if not counts:
        return "미상"
    top = max(counts, key=counts.get)
    rest = sum(counts.values()) - counts[top]
    return top if counts[top] >= 2 * rest else "혼합"


def feed_group(ft: str) -> str:
    """비교용 2그룹: 생식·화식 vs 건사료 (혼합·미상은 그대로)."""
    return {"생식": "생식·화식", "화식": "생식·화식"}.get(ft, ft)
