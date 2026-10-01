"""발표용 PNG: 수집 퍼널, 기능별 언급률/불만율/불만 강도, 기회 매트릭스, 급여 방식 비교, 토픽 표, 워드클라우드."""
import logging
import platform
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

log = logging.getLogger("pipeline")

INK, INK2, MUTED, GRID, BASE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, SURFACE = "#2a78d6", "#eb6834", "#fcfcfb"


def set_korean_font() -> str | None:
    """OS별 한글 폰트 지정. 워드클라우드용 폰트 파일 경로를 돌려준다."""
    system = platform.system()
    candidates = {"Windows": ["Malgun Gothic"], "Darwin": ["AppleGothic", "Apple SD Gothic Neo"]}.get(
        system, ["NanumGothic", "Noto Sans CJK KR", "Noto Sans CJK JP"])
    installed = {f.name: f.fname for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False
            return installed[name]
    if system == "Windows" and Path("C:/Windows/Fonts/malgun.ttf").exists():
        font_manager.fontManager.addfont("C:/Windows/Fonts/malgun.ttf")
        plt.rcParams["font.family"] = "Malgun Gothic"
        plt.rcParams["axes.unicode_minus"] = False
        return "C:/Windows/Fonts/malgun.ttf"
    log.warning("한글 폰트를 찾지 못했습니다 — 그래프 글자가 깨질 수 있습니다")
    return None


def _style(ax, title, xlabel=None):
    ax.set_title(title, loc="left", fontsize=13, color=INK, pad=12)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(BASE)
    ax.tick_params(colors=INK2, length=0)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED)


def _save(fig, out: Path, name: str):
    fig.patch.set_facecolor(SURFACE)
    fig.tight_layout()
    fig.savefig(out / name, dpi=200, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)


def hbar(values, labels, title, xlabel, out, name, fmt="{:.0%}", note=None):
    values = np.nan_to_num(np.asarray(values, dtype=float))
    fig, ax = plt.subplots(figsize=(8, 0.6 * len(labels) + 1.6))
    y = np.arange(len(labels))[::-1]
    ax.barh(y, values, color=BLUE, height=0.55)
    ax.set_yticks(y, labels, color=INK)
    for yi, v in zip(y, values):
        ax.text(v, yi, " " + fmt.format(v), va="center", color=INK2, fontsize=10)
    ax.set_xlim(0, max(values.max() * 1.25, 1e-9))
    if "%" in fmt:
        ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    _style(ax, title, xlabel)
    if note:
        fig.text(0.01, 0.01, note, color=MUTED, fontsize=8)
    _save(fig, out, name)


def grouped_bar(t, out):
    groups, colors = ["생식·화식", "건사료"], [BLUE, ORANGE]
    fig, ax = plt.subplots(figsize=(8, 0.9 * len(t) + 1.6))
    y = np.arange(len(t))[::-1]
    h = 0.36
    for k, (g, c) in enumerate(zip(groups, colors)):
        vals = np.nan_to_num(t[f"{g} 언급률"].astype(float).values)
        n = int(t[f"{g} 문서 수"].iloc[0]) if len(t) else 0
        ax.barh(y + (h / 2 if k == 0 else -h / 2), vals, height=h - 0.04, color=c, label=f"{g} (n={n})")
        for yi, v in zip(y, vals):
            ax.text(v, yi + (h / 2 if k == 0 else -h / 2), f" {v:.0%}", va="center", color=INK2, fontsize=9)
    ax.set_yticks(y, t["기능"], color=INK)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.legend(frameon=False, loc="lower right", bbox_to_anchor=(1, 1.0), ncol=2, labelcolor=INK2)
    ax.set_xlim(0, max(np.nan_to_num(t[["생식·화식 언급률", "건사료 언급률"]].astype(float).values).max() * 1.2, 1e-9))
    _style(ax, "급여 방식별 기능 언급률", "해당 급여 방식 문서 중 기능 언급 비율")
    _save(fig, out, "mention_by_feed.png")


def opportunity_matrix(s, out):
    fig, ax = plt.subplots(figsize=(7, 6))
    x, y = s["언급률"].astype(float), s["불만율"].astype(float)
    ax.scatter(x, y, s=140, color=BLUE, edgecolor=SURFACE, linewidth=2, zorder=3)
    for xi, yi, lab, low in zip(x, y, s["기능"], s["표본 부족"]):
        ax.annotate(lab + (" (표본 부족)" if low else ""), (xi, yi), xytext=(8, 6),
                    textcoords="offset points", color=INK, fontsize=10)
    if x.notna().any() and y.notna().any():
        ax.axvline(x.median(), color=BASE, linestyle="--", linewidth=1)
        ax.axhline(y.median(), color=BASE, linestyle="--", linewidth=1)
        fig.text(0.01, 0.01, "점선 = 중앙값 · 오른쪽 위(많이 말하고 불만도 큼) = 우선 개발 후보", color=MUTED, fontsize=8)
    ax.margins(x=0.2, y=0.12)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.set_ylabel("불만율 (기능 언급 문장 중 불만 문장)", color=MUTED)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.spines["left"].set_color(BASE)
    _style(ax, "기능별 기회 매트릭스 (언급률 × 불만율)", "언급률 (탐색 수집분 중 언급 문서)")
    ax.spines["left"].set_visible(True)
    _save(fig, out, "opportunity_matrix.png")


def table_png(df, title, out, name, col_widths=None):
    if df.empty:
        return
    fig_h = 0.5 * len(df) + 1.2
    fig, ax = plt.subplots(figsize=(13, fig_h))
    ax.axis("off")
    tb = ax.table(cellText=df.values, colLabels=df.columns, loc="upper left", cellLoc="left",
                  colWidths=col_widths)
    tb.auto_set_font_size(False)
    tb.set_fontsize(9)
    tb.scale(1, 1.5)
    for (r, _), cell in tb.get_celld().items():
        cell.set_edgecolor(GRID)
        cell.set_facecolor(SURFACE)
        if r == 0:
            cell.set_text_props(color=INK, weight="bold")
            cell.set_facecolor("#f0efec")
    ax.set_title(title, loc="left", fontsize=13, color=INK)
    _save(fig, out, name)


def topic_table(topics, out):
    if topics.empty:
        return
    t = topics.copy()
    t["문서 비중"] = t["문서 비중"].map("{:.0%}".format)
    t["불만 문서 비율"] = t["불만 문서 비율"].map(lambda v: "" if pd.isna(v) else f"{v:.0%}")
    t["대표 문장"] = t["대표 문장"].str.slice(0, 45)
    table_png(t[["토픽", "상위 키워드", "문서 비중", "불만 문서 비율", "대표 문장"]],
              f"토픽별 상위 키워드 (신규 페인포인트 후보) — {topics['대상'].iloc[0]}",
              out, "topic_table.png", col_widths=[0.05, 0.42, 0.07, 0.08, 0.38])


def wordcloud_png(token_series, font_path, title, out, name):
    from wordcloud import WordCloud
    freq = Counter(w for t in token_series.fillna("") for w in t.split())
    if not freq or not font_path:
        return
    wc = WordCloud(font_path=font_path, width=1600, height=900, background_color=SURFACE,
                   colormap="Blues", max_words=120, prefer_horizontal=0.9).generate_from_frequencies(freq)
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.imshow(wc.recolor(color_func=lambda *a, **k: BLUE if np.random.rand() < 0.6 else "#1c5cab"),
              interpolation="bilinear")
    ax.axis("off")
    ax.set_title(title, loc="left", fontsize=13, color=INK)
    _save(fig, out, name)


def run_visualize(res: dict, cfg: dict) -> None:
    out = Path(cfg["paths"]["figures"])
    out.mkdir(parents=True, exist_ok=True)
    font = set_korean_font()
    s = res["summary"]
    basis = s["언급률 기준"].iloc[0] if len(s) else ""
    jobs = [
        lambda: hbar(res["funnel"]["문서 수"], res["funnel"]["단계"], "수집 → 필터 단계별 문서 수", "문서 수",
                     out, "funnel.png", fmt="{:,.0f}"),
        lambda: hbar(s["언급률"], s["기능"], "기능별 언급률", "언급률", out, "mention_rate.png", note=f"기준: {basis}"),
        lambda: hbar(s["불만율"], s["기능"], "기능별 불만율", "기능 언급 문장 중 불만 문장 비율",
                     out, "complaint_rate.png"),
        lambda: hbar(s["불만 강도(평균 부정점수)"], s["기능"], "기능별 불만 강도", "기능 언급 문장의 평균 부정 점수",
                     out, "complaint_intensity.png", fmt="{:.2f}"),
        lambda: hbar(s["기회 점수"], s["기능"], "기능별 기회 점수 (언급률 × 불만율)", "기회 점수",
                     out, "opportunity_score.png", fmt="{:.3f}"),
        lambda: opportunity_matrix(s, out),
        lambda: grouped_bar(res["feed"], out),
        lambda: topic_table(res["topics"], out),
        lambda: wordcloud_png(res["docs"]["tokens"], font, "전체 문서 워드클라우드", out, "wordcloud_all.png"),
        lambda: wordcloud_png(res["topic_docs"]["tokens"] if len(res["topic_docs"]) else res["docs"]["tokens"].iloc[0:0],
                              font, "기능 사전 밖 문서 워드클라우드", out, "wordcloud_residual.png"),
    ]
    for job in jobs:
        try:
            job()
        except Exception as e:                                # 그래프 1개 실패해도 나머지는 만든다
            log.warning("그래프 생성 실패: %s", e)
    log.info("그래프 저장: %s", sorted(p.name for p in out.glob("*.png")))
