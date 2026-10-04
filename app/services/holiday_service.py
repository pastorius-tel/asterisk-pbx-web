"""国民の祝日データの取り込み。

内閣府「国民の祝日について」で公開されている CSV を取得して DB に
保存する。このデータはデジタル庁のデータカタログサイトで CC-BY として
公開されているオープンデータ。

CSV の仕様:
  - URL : https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv
  - 文字コード : Shift-JIS (CP932)
  - 形式 : 「国民の祝日・休日月日,国民の祝日・休日名称」のヘッダ + 明細
           例) 2026/9/21,敬老の日
  - 1955年頃から翌年分まで含まれる

注意: 過去に一度ファイル名が shukujitsu.csv に変更され、その後
syukujitsu.csv に戻された経緯がある。取得に失敗したときのために
両方の URL を順に試す。
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from datetime import date

logger = logging.getLogger(__name__)

CSV_URLS = [
    "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv",
    # 予備 (過去に使われていたファイル名)
    "https://www8.cao.go.jp/chosei/shukujitsu/shukujitsu.csv",
]

# ブラウザ以外からのアクセスを弾くサイトがあるため UA を明示する
_USER_AGENT = "asterisk-pbx-web/holiday-importer"


def fetch_holiday_csv(timeout: int = 30) -> tuple[bool, str, bytes]:
    """祝日 CSV をダウンロードする。

    戻り値: (成功したか, メッセージ, 生バイト列)
    """
    last_error = ""
    for url in CSV_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if not data:
                last_error = f"{url}: 空のレスポンス"
                continue
            return (True, url, data)
        except Exception as e:  # noqa: BLE001
            last_error = f"{url}: {e}"
            logger.warning("祝日 CSV の取得に失敗 %s", last_error)
    return (False, last_error, b"")


def parse_holiday_csv(data: bytes) -> tuple[list[tuple[date, str]], list[str]]:
    """祝日 CSV をパースして [(日付, 名称), ...] を返す。

    戻り値: (祝日リスト, 警告メッセージのリスト)
    """
    warnings: list[str] = []
    # 内閣府の CSV は Shift-JIS。将来 UTF-8 に変わっても読めるよう順に試す。
    text = ""
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return ([], ["CSV の文字コードを判別できませんでした。"])

    holidays: list[tuple[date, str]] = []
    reader = csv.reader(io.StringIO(text))
    for i, row in enumerate(reader):
        if len(row) < 2:
            continue
        raw_date, name = row[0].strip(), row[1].strip()
        if not raw_date or not name:
            continue
        # ヘッダ行を読み飛ばす
        if i == 0 and not raw_date[0].isdigit():
            continue
        try:
            # "2026/9/21" 形式
            y, m, d = (int(x) for x in raw_date.replace("-", "/").split("/"))
            holidays.append((date(y, m, d), name))
        except (ValueError, TypeError):
            warnings.append(f"読み取れない行をスキップしました: {raw_date},{name}")
            continue
    return (holidays, warnings)
