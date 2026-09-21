"""電話帳 (短縮ダイヤル)。

相手先の名前と番号を登録し、短縮番号を割り当てて発信できるようにする。
発着信履歴の「⋯」からも登録できる。

短縮ダイヤルは「*7 + 短縮番号」でかける (短縮番号 01 なら *701)。
既存の特番 (*8 / *43 / *97 / *98) と衝突しない番号帯を使っている。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import PhoneBookEntry
from app.routers._helpers import form_int
from app.validators import normalize_phone_number

router = APIRouter(prefix="/phone-book", tags=["phone_book"])

# 短縮番号は 2〜3 桁の「半角」数字のみ (ダイヤルプランのパターンを単純に
# 保つため)。\d は全角数字 '１' にも一致してしまうので使わない
# (全角のまま保存されると *7 に続く番号が Asterisk 側で解決できず、
#  「登録したのに短縮ダイヤルが繋がらない」という不具合になる)。
_SPEED_RE = re.compile(r"^[0-9]{2,3}$")
# 電話番号として許可する文字 (数字と、外線発信の 0 発信等で使う記号)。
# 桁数は DB のカラム長 String(64) に合わせて 32 桁までとする。
_NUMBER_RE = re.compile(r"^[0-9*#+]{1,32}$")

SPEED_DIAL_PREFIX = "*7"
"""短縮ダイヤルの前置番号。ここを変える場合は
asterisk_config.py の短縮ダイヤル生成も合わせること。"""


def _back(message: str, ok: bool = True) -> RedirectResponse:
    return RedirectResponse(
        f"/phone-book/?message={quote(message)}&ok={'1' if ok else '0'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _clean_number(raw: str) -> str:
    """入力された電話番号を正規化する。

    ハイフン・空白・括弧を除去するのに加え、全角数字と全角ハイフンも
    半角に直す。Excel やメールから貼り付けると「０９０−１２３４−５６７８」
    のように全角が混ざることが多く、以前はこれがそのまま弾かれていた。
    """
    return normalize_phone_number(raw)


@router.get("/", response_class=HTMLResponse)
async def phone_book_list(
    request: Request,
    message: str | None = None,
    ok: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    rows = (
        await db.scalars(
            select(PhoneBookEntry).order_by(
                PhoneBookEntry.speed_dial.is_(None),  # 短縮番号あり を先に
                PhoneBookEntry.speed_dial,
                PhoneBookEntry.name,
            )
        )
    ).all()
    return request.app.state.templates.TemplateResponse(
        request, "phone_book/list.html",
        {
            "title": "電話帳 (短縮ダイヤル)",
            "items": rows,
            "message": message,
            "ok": ok,
            "prefix": SPEED_DIAL_PREFIX,
        },
    )


@router.post("/save")
async def save_entries(
    request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    """表に並んだ全行をまとめて保存する。id が空の行は新規追加。"""
    raw = await request.form()
    ids = raw.getlist("e_id")
    names = raw.getlist("e_name")
    numbers = raw.getlist("e_number")
    kanas = raw.getlist("e_kana")
    speeds = raw.getlist("e_speed")
    companies = raw.getlist("e_company")
    enableds = raw.getlist("e_enabled")

    errors: list[str] = []
    used_speed: dict[str, str] = {}

    for idx, eid in enumerate(ids):
        name = (names[idx] if idx < len(names) else "").strip()
        number = _clean_number(numbers[idx] if idx < len(numbers) else "")
        kana = (kanas[idx] if idx < len(kanas) else "").strip()
        speed = (speeds[idx] if idx < len(speeds) else "").strip()
        company = (companies[idx] if idx < len(companies) else "").strip()
        enabled = str(idx) in enableds

        if not name and not number:
            continue  # 空行はスキップ (使わなかった新規行)
        if not name or not number:
            errors.append("名前と電話番号の両方を入力してください。")
            continue
        if not _NUMBER_RE.match(number):
            errors.append(f"「{name}」の電話番号に使えない文字があります。")
            continue
        if speed:
            if not _SPEED_RE.match(speed):
                errors.append(
                    f"「{name}」の短縮番号は 2〜3 桁の数字で入力してください。"
                )
                continue
            # 同じ短縮番号が二重に割り当てられていないか
            if speed in used_speed:
                errors.append(
                    f"短縮番号 {speed} が「{used_speed[speed]}」と重複しています。"
                )
                continue
            used_speed[speed] = name

        if eid:
            # eid は画面から来る想定だが、URL を直接叩けば "abc" や
            # 巨大な数も届く。int() をそのまま呼ぶと 500 になるので
            # 変換できない行は黙って飛ばす。
            eid_int = form_int(eid)
            obj = await db.get(PhoneBookEntry, eid_int) if eid_int else None
            if obj is None:
                continue
            obj.name, obj.number = name, number
            obj.kana = kana or None
            obj.speed_dial = speed or None
            obj.company = company or None
            obj.enabled = enabled
        else:
            db.add(PhoneBookEntry(
                name=name, number=number, kana=kana or None,
                speed_dial=speed or None, company=company or None,
                enabled=enabled,
            ))

    # 保存後に、DB 全体で短縮番号が重複していないか最終確認する
    await db.flush()
    all_rows = (await db.scalars(select(PhoneBookEntry))).all()
    seen: dict[str, str] = {}
    for r in all_rows:
        if not r.speed_dial:
            continue
        if r.speed_dial in seen:
            errors.append(
                f"短縮番号 {r.speed_dial} が「{seen[r.speed_dial]}」と"
                f"「{r.name}」で重複しています。どちらかを変更してください。"
            )
            r.speed_dial = None  # 重複は解除して保存を通す
        else:
            seen[r.speed_dial] = r.name

    await db.commit()
    if errors:
        return _back(" / ".join(errors[:3]), ok=False)
    return _back("電話帳を保存しました。")


@router.post("/{entry_id}/delete")
async def delete_entry(
    entry_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(PhoneBookEntry, entry_id)
    if obj is not None:
        name = obj.name
        await db.delete(obj)
        await db.commit()
        return _back(f"「{name}」を削除しました。")
    return _back("見つかりませんでした。", ok=False)
