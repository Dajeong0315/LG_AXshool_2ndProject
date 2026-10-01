"""전처리: 재필터 → 광고 제거 → 길이 → 중복(URL·본문 유사도) → 정규화 → 형태소 분석."""
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer

from crawlers.utils import sha
from preprocess.filters import animal_check, feed_group, feed_type, in_period

log = logging.getLogger("pipeline")

URL_RE = re.compile(r"https?://\S+|www\.\S+")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}")
KEEP_RE = re.compile(r"[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ\s.,!?~%]")


def read_list(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text("utf-8").splitlines() if l.strip() and not l.startswith("#")]


def normalize(text: str) -> str:
    t = URL_RE.sub(" ", str(text or ""))
    t = EMAIL_RE.sub(" ", t)
    t = PHONE_RE.sub(" ", t)                                  # 개인정보 마스킹
    t = re.sub(r"([ㅋㅎㅠㅜ])\1{2,}", r"\1\1", t)              # ㅋㅋㅋㅋ → ㅋㅋ (감성 단서는 남김)
    t = re.sub(r"(.)\1{3,}", r"\1\1\1", t)                    # 같은 글자 반복 축약
    t = KEEP_RE.sub(" ", t)                                   # 이모지·특수문자 제거
    return re.sub(r"\s+", " ", t).strip()


def near_duplicates(texts: list[str], threshold: float) -> np.ndarray:
    """글자 3-gram TF-IDF 코사인 유사도 ≥ threshold 인 뒤쪽 문서를 True로 표시."""
    n = len(texts)
    drop = np.zeros(n, dtype=bool)
    if n < 2:
        return drop
    X = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 3), use_idf=False).fit_transform(texts)  # 글자 빈도 코사인
    for s in range(0, n, 1000):                               # 1만 건도 메모리 안에서 처리되게 1,000행씩
        sim = (X[s:s + 1000] @ X.T).tocoo()
        for i, j, v in zip(sim.row + s, sim.col, sim.data):
            if j < i and v >= threshold and not drop[j]:
                drop[i] = True
    return drop


class Tokenizer:
    def __init__(self, cfg: dict):
        from kiwipiepy import Kiwi
        self.kiwi = Kiwi()
        for w in read_list(cfg["paths"]["user_dict"]):
            self.kiwi.add_user_word(w, "NNP")
        self.stop = set(read_list(cfg["paths"]["stopwords"]))

    def tokens(self, text: str) -> list[str]:
        out = []
        for t in self.kiwi.tokenize(text):
            if t.tag in ("NNG", "NNP", "XR", "SL"):
                w = t.form.lower()
            elif t.tag in ("VV", "VA"):
                w = t.form + "다"
            else:
                continue
            if len(w) >= 2 and w not in self.stop:
                out.append(w)
        return out

    def sentences(self, text: str) -> list[str]:
        return [s.text for s in self.kiwi.split_into_sents(text)] or [text]


def run_preprocess(raw: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    funnel = [("수집 원본", len(raw))]
    df = raw.copy()
    for c in ("title", "body"):
        df[c] = df[c].fillna("").astype(str)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    raw_text = df["title"] + " " + df["body"]

    # 1) 기간·강아지 재확인 (config를 바꿨을 때 대비)
    need_dog = df["source"].map(lambda s: cfg["sites"].get(s, {}).get("require_dog_word", True))
    keep = df["date"].apply(lambda d: in_period(d, cfg))
    df, raw_text, need_dog = df[keep], raw_text[keep], need_dog[keep]
    funnel.append(("최근 1년", len(df)))
    df["animal"] = [animal_check(t, cfg, nd) for t, nd in zip(raw_text, need_dog)]
    keep = ~df["animal"].isin(["cat", "none"])
    df, raw_text = df[keep], raw_text[keep]
    funnel.append(("강아지 글", len(df)))

    # 2) 광고·협찬
    ad_re = re.compile("|".join(map(re.escape, cfg["ad_patterns"])))
    n_links = raw_text.str.count(r"https?://")
    is_ad = raw_text.str.contains(ad_re) | (n_links >= cfg["max_links"])
    df, raw_text = df[~is_ad], raw_text[~is_ad]
    funnel.append(("광고·협찬 제거", len(df)))

    # 3) 정규화 + 길이
    df["text"] = [normalize(t) for t in raw_text]
    df = df[df["text"].str.replace(" ", "").str.len() >= cfg["min_body_len"]]
    funnel.append(("짧은 글 제거", len(df)))

    # 4) 중복: URL → 본문 완전일치 → 본문 유사도
    df = df.sort_values("date").drop_duplicates("url_hash")
    bare = df["text"].str.replace(r"[^0-9A-Za-z가-힣]", "", regex=True)   # 문장부호·공백 무시
    df["text_hash"] = bare.map(sha)
    df = df.drop_duplicates("text_hash")
    df = df[~near_duplicates(df["text"].str.replace(r"[^\w\s]", "", regex=True).tolist(), cfg["near_dup_threshold"])]
    funnel.append(("중복 제거", len(df)))

    # 5) 급여 방식 재태깅 + 형태소
    df["feed_type"] = df["text"].map(lambda t: feed_type(t, cfg))
    df["feed_group"] = df["feed_type"].map(feed_group)
    tok = Tokenizer(cfg)
    df["tokens"] = [" ".join(tok.tokens(t)) for t in df["text"]]
    df["sentences"] = [" || ".join(tok.sentences(t)) for t in df["text"]]
    df = df.reset_index(drop=True)

    funnel_df = pd.DataFrame(funnel, columns=["단계", "문서 수"])
    out = Path(cfg["paths"]["processed"])
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "docs.csv", index=False, encoding="utf-8-sig")
    log.info("전처리 완료: %s", " → ".join(f"{a} {b}" for a, b in funnel))
    return df, funnel_df
