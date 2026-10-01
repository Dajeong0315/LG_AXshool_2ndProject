"""규칙 라벨 정확도 검증: 사람이 human_label 을 채운 ig_reaction_label_sample.csv 로 라벨별 precision 계산.

사용:  python -m analysis.ig_label_eval [outputs/tables/ig_reaction_label_sample.csv]
human_label 에는 fear|curious|play|indifferent|adapted|unknown 을 '|' 또는 ',' 로 여러 개 적을 수 있다.
"""
import re
import sys

import pandas as pd


def label_precision(df: pd.DataFrame) -> pd.DataFrame:
    """규칙이 라벨 L 을 붙인 표본 중 사람 라벨에도 L 이 있는 비율. human_label 이 빈 행은 제외."""
    df = df[df["human_label"].fillna("").str.strip() != ""]
    rows = []
    for lb, g in df.groupby("rule_label"):
        ok = g["human_label"].map(lambda x: lb in {t.strip() for t in re.split(r"[|,]", str(x))}).sum()
        rows.append({"rule_label": lb, "n_labeled": len(g), "n_correct": int(ok), "precision": round(ok / len(g), 3)})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "outputs/tables/ig_reaction_label_sample.csv"
    print(label_precision(pd.read_csv(path, encoding="utf-8-sig")).to_string(index=False))
