"""入力検証の共通ルール。

同じ種類の値 (時間帯・電話番号・エクステンションパターン) が画面ごとに
別々の正規表現で検証されていると、片方だけ通ってしまう「抜け道」が
できる。実際、日次パターンの時間帯は
  - スケジュール設定画面 (routers/schedules.py) … 25:99 を弾いていた
  - 日次パターン画面     (schemas/day_pattern.py) … 書式だけ見ていた
と検証内容が食い違っていた。ここに集約して両方から使う。

正規表現についての注意:
  Python の ``\\d`` は Unicode の数字すべて (全角 '１'、アラビア数字 '٣'
  など) にマッチする。電話番号や内線番号は半角数字でなければ Asterisk
  に渡せないため、この種の検証では必ず ``[0-9]`` を使う。
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# 文字種
# ---------------------------------------------------------------------------

# 半角数字のみ (\d だと全角にも一致してしまうため使わない)
DIGITS_RE = re.compile(r"^[0-9]+$")

# HH:MM-HH:MM
TIME_RANGE_RE = re.compile(r"^[0-9]{1,2}:[0-9]{2}-[0-9]{1,2}:[0-9]{2}$")

# コンテキスト名などに使う識別子
IDENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 電話番号 (ダイヤルできる文字のみ)
PHONE_RE = re.compile(r"^[0-9*#+]{1,32}$")

# 内線番号
EXTENSION_RE = re.compile(r"^[0-9]{1,10}$")

# Asterisk のエクステンションパターンに使える文字
_PATTERN_CHARS_RE = re.compile(r"^_?[0-9XZNxzn.!\[\]+*#-]{1,64}$")


# ---------------------------------------------------------------------------
# 全角 → 半角
# ---------------------------------------------------------------------------

# 全角ハイフン類 (Excel やメールからの貼り付けで紛れ込みやすい)。
# NFKC でも半角 '-' にならない文字があるためここで個別に置換する。
_DASHES = "－−‐‑‒–—―ー"


def _to_halfwidth(raw: str | None) -> str:
    """全角英数記号を半角に直し、ダッシュ類を半角ハイフンに揃える。"""
    text = unicodedata.normalize("NFKC", (raw or "").strip())
    for ch in _DASHES:
        text = text.replace(ch, "-")
    return text


def normalize_phone_number(raw: str | None) -> str:
    """電話番号を「実際にダイヤルできる文字」だけに正規化する。

    「０９０−１２３４−５６７８」「03 (1234) 5678」のように貼り付けられても
    そのまま受け取れるようにする。ハイフン・空白・括弧は落とす。
    """
    return "".join(c for c in _to_halfwidth(raw) if c in "0123456789*#+")


def normalize_pattern_input(raw: str | None) -> str:
    """Asterisk のエクステンションパターン入力を正規化する。

    電話番号と違い、``[2-9]`` の範囲指定で使うハイフンは残さなければ
    ならない (消すと [29] になり「2 と 9 のどちらか」に意味が変わって
    しまう)。そのため **[] の外側のハイフンだけ** を区切り文字として
    落とす。
    """
    text = re.sub(r"[\s()]", "", _to_halfwidth(raw))
    out: list[str] = []
    in_bracket = False
    for ch in text:
        if ch == "[":
            in_bracket = True
        elif ch == "]":
            in_bracket = False
        elif ch == "-" and not in_bracket:
            continue  # 番号の区切りとして書かれたハイフンは捨てる
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 時間帯
# ---------------------------------------------------------------------------


def validate_time_ranges(value: str) -> str | None:
    """"HH:MM-HH:MM,..." を検証する。問題なければ None、あればエラー文言。

    書式だけでなく実在する時刻かどうかも見る。25:99 のような値は
    Asterisk の GotoIfTime が解釈できず、時間条件が**常に不成立**に
    なって「なぜか時間内に転送されない」という分かりにくい障害になる。
    """
    value = (value or "").strip()
    if not value:
        return None  # 空欄は「終日休業」として有効
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if not TIME_RANGE_RE.match(part):
            return (
                f"時間帯「{part}」の形式が正しくありません。"
                "09:00-18:00 のように HH:MM-HH:MM で入力してください "
                "(コロンを省略した 0900-1700 のような書き方は使えません)。"
            )
        start_s, end_s = part.split("-")
        times = []
        for hhmm in (start_s, end_s):
            hh, mm = (int(x) for x in hhmm.split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                return (
                    f"時間帯「{part}」に存在しない時刻が含まれています "
                    "(時は 0〜23、分は 0〜59 で指定してください)。"
                )
            times.append(hh * 60 + mm)
        if times[0] >= times[1]:
            return (
                f"時間帯「{part}」の開始時刻が終了時刻以降になっています。"
                "日をまたぐ営業時間は 09:00-23:59 と 00:00-02:00 のように"
                "2 つに分けて指定してください。"
            )
    return None


def normalize_time_ranges(value: str) -> str:
    """前後の空白を落としてカンマ区切りに正規化する。"""
    parts = [p.strip() for p in (value or "").split(",") if p.strip()]
    return ",".join(parts)


# ---------------------------------------------------------------------------
# エクステンションパターン
# ---------------------------------------------------------------------------


def validate_exten_pattern(pat: str) -> str | None:
    """Asterisk の exten => の左辺として成立しているか検証する。

    ``_[9-1]X`` のように範囲が逆向きのパターンや、括弧が閉じていない
    パターンを書き込むと、Asterisk がそのコンテキストの読み込みに失敗
    することがある。迷惑電話ブロックのつもりが**着信全体が止まる**
    という壊れ方をするため、保存の時点で弾く。
    """
    pat = (pat or "").strip()
    if not pat:
        return "番号またはパターンを入力してください。"
    if not any(c.isascii() and (c.isdigit() or c in "XZNxzn.!") for c in pat):
        return "番号またはパターンを入力してください (数字が含まれていません)。"
    if not _PATTERN_CHARS_RE.match(pat):
        return (
            "使える文字は半角数字と X, Z, N, ., !, [, ], *, #, + のみです "
            "(先頭に _ を付けるとパターン指定になります。例: _03XXXXXXXX)。"
        )

    body = pat[1:] if pat.startswith("_") else pat
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "[":
            close = body.find("]", i + 1)
            if close == -1:
                return "[ に対応する ] がありません。"
            inner = body[i + 1 : close]
            if not inner:
                return "[] の中が空です。例: [2-9]"
            j = 0
            while j < len(inner):
                if j + 2 < len(inner) and inner[j + 1] == "-":
                    lo, hi = inner[j], inner[j + 2]
                    if not (lo.isascii() and lo.isdigit()) or not (
                        hi.isascii() and hi.isdigit()
                    ):
                        return f"範囲指定 [{inner}] には半角数字を使ってください。"
                    if lo > hi:
                        return (
                            f"範囲指定 [{inner}] の順序が逆です "
                            f"([{hi}-{lo}] の間違いではありませんか)。"
                        )
                    j += 3
                else:
                    if not (inner[j].isascii() and inner[j].isdigit()):
                        return f"[] の中には半角数字か範囲 (2-9) を指定してください: [{inner}]"
                    j += 1
            i = close + 1
            continue
        if ch == "]":
            return "] に対応する [ がありません。"
        i += 1
    return None
