"""発着信履歴。

Asterisk のダイヤルプラン (ハングアップハンドラー) から通知を受けて
記録し、画面で発信履歴・着信履歴を分けて表示する。

履歴の行からは、相手の番号を
  - 迷惑電話ブロックリスト
  - 電話帳 (短縮ダイヤル)
へワンタッチで登録できる。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.hooks import verify_hook
from app.models import BlockedNumber, CallLog, Extension, PhoneBookEntry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/call-logs", tags=["call_logs"])

# 着信先の種別 → 画面表示名
_DEST_LABELS = {
    "extension": "内線",
    "ring_group": "リンググループ",
    "queue": "キュー",
    "ivr": "IVR",
    "voicemail": "留守番電話",
    "fax": "FAX",
    "time_condition": "時間条件",
    "timecond": "時間条件",
    "trunk": "外線",
    "hangup": "切断",
}


def _friendly_dest(log: CallLog) -> str:
    """着信先を読みやすい文字列にする。"""
    if not log.dest_type:
        return "-"
    base = _DEST_LABELS.get(log.dest_type, log.dest_type)
    if log.dest_value:
        return f"{base} {log.dest_value}"
    return base


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("/hook/record")
async def record_call(
    request: Request,
    token: str = Query(""),
    direction: str = Query(""),
    peer: str = Query(""),
    peer_name: str = Query(""),
    did: str = Query(""),
    dest_type: str = Query(""),
    dest_value: str = Query(""),
    dest_label: str = Query(""),
    trunk: str = Query(""),
    blocked: str = Query(""),
    disposition: str = Query(""),
    duration: str = Query(""),
    billsec: str = Query(""),
    uniqueid: str = Query(""),
    start: str = Query(""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Asterisk から通話終了の通知を受けて履歴を 1 件記録する。"""
    await verify_hook(request, db, "calllog", token)
    if direction not in ("in", "out"):
        return {"ok": False, "reason": "invalid direction"}

    # 同じ通話が二重に通知された場合は記録しない
    if uniqueid:
        exists = (
            await db.scalars(select(CallLog).where(CallLog.uniqueid == uniqueid))
        ).first()
        if exists is not None:
            return {"ok": True, "skipped": "duplicate"}

    # 開始日時: Asterisk の CDR(start) は "YYYY-MM-DD HH:MM:SS" 形式
    started = datetime.now()
    if start:
        try:
            started = datetime.strptime(start.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    db.add(CallLog(
        direction=direction,
        started_at=started,
        peer_number=(peer or "anonymous").strip() or "anonymous",
        peer_name=(peer_name or None),
        did_number=(did or None),
        dest_type=(dest_type or None),
        dest_value=(dest_value or None),
        dest_label=(dest_label or None),
        disposition=(disposition or None),
        duration_seconds=_parse_int(duration),
        billable_seconds=_parse_int(billsec),
        uniqueid=(uniqueid or None),
        trunk_name=(trunk or None),
        is_blocked=(blocked == "1"),
    ))
    await db.commit()
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
async def call_log_list(
    request: Request,
    direction: str = Query("in"),
    days: int = Query(30),
    q: str = Query(""),
    message: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """発着信履歴の一覧。direction で着信/発信を切り替える。"""
    if direction not in ("in", "out"):
        direction = "in"
    days = max(1, min(days, 365))
    since = datetime.now() - timedelta(days=days)

    stmt = (
        select(CallLog)
        .where(CallLog.direction == direction, CallLog.started_at >= since)
        .order_by(CallLog.started_at.desc())
        .limit(500)
    )
    if q.strip():
        needle = f"%{q.strip()}%"
        stmt = stmt.where(CallLog.peer_number.like(needle))
    rows = (await db.scalars(stmt)).all()

    # 既に登録済みの番号は、サブメニューで「登録済み」と出す
    blocked_numbers = {
        b.pattern for b in (await db.scalars(select(BlockedNumber))).all()
    }
    book_numbers = {
        p.number for p in (await db.scalars(select(PhoneBookEntry))).all()
    }
    items = [
        {
            "log": r,
            "dest": r.dest_label or _friendly_dest(r),
            "in_block": r.peer_number in blocked_numbers,
            "in_book": r.peer_number in book_numbers,
        }
        for r in rows
    ]
    return request.app.state.templates.TemplateResponse(
        request, "call_logs/list.html",
        {
            "title": "着信履歴" if direction == "in" else "発信履歴",
            "items": items,
            "direction": direction,
            "days": days,
            "q": q,
            "message": message,
            "ext_choices": [
                {"value": e.extension, "label": f"{e.extension} {e.display_name}"}
                for e in (
                    await db.scalars(
                        select(Extension).where(Extension.enabled.is_(True))
                        .order_by(Extension.extension)
                    )
                ).all()
            ],
        },
    )


@router.post("/{log_id}/block")
async def add_to_blocklist(
    log_id: int,
    action: str = Form("hangup"),
    voicemail_target: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """履歴の相手番号を迷惑電話ブロックリストに登録する。"""
    log = await db.get(CallLog, log_id)
    if log is None:
        return _back(log_id and "in" or "in", "履歴が見つかりません。")
    number = log.peer_number
    if not number or number == "anonymous":
        return _back(log.direction, "非通知の番号は登録できません。")

    exists = (
        await db.scalars(select(BlockedNumber).where(BlockedNumber.pattern == number))
    ).first()
    if exists is not None:
        return _back(log.direction, f"{number} は既に登録されています。")

    if action not in ("hangup", "voicemail"):
        action = "hangup"
    db.add(BlockedNumber(
        pattern=number,
        action=action,
        voicemail_target=(voicemail_target or None) if action == "voicemail" else None,
        note=f"発着信履歴から登録 ({log.started_at:%Y-%m-%d})",
        enabled=True,
    ))
    await db.commit()
    return _back(log.direction, f"{number} を迷惑電話ブロックリストに登録しました。")


@router.post("/{log_id}/phonebook")
async def add_to_phonebook(
    log_id: int,
    name: str = Form(""),
    speed_dial: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """履歴の相手番号を電話帳に登録する。"""
    log = await db.get(CallLog, log_id)
    if log is None:
        return _back("in", "履歴が見つかりません。")
    number = log.peer_number
    if not number or number == "anonymous":
        return _back(log.direction, "非通知の番号は登録できません。")

    exists = (
        await db.scalars(select(PhoneBookEntry).where(PhoneBookEntry.number == number))
    ).first()
    if exists is not None:
        return _back(log.direction, f"{number} は既に電話帳にあります。")

    # 短縮番号 (任意)。2〜3 桁の数字で、既に使われていなければ設定する。
    speed = speed_dial.strip()
    msg_extra = ""
    if speed:
        if not speed.isdigit() or not (2 <= len(speed) <= 3):
            return _back(log.direction, "短縮番号は 2〜3 桁の数字で入力してください。")
        dup = (
            await db.scalars(
                select(PhoneBookEntry).where(PhoneBookEntry.speed_dial == speed)
            )
        ).first()
        if dup is not None:
            return _back(
                log.direction,
                f"短縮番号 {speed} は「{dup.name}」で使用中です。別の番号にしてください。",
            )
        msg_extra = f" (短縮 *7{speed})"

    db.add(PhoneBookEntry(
        name=(name.strip() or log.peer_name or number),
        number=number,
        speed_dial=speed or None,
        enabled=True,
    ))
    await db.commit()
    return _back(log.direction, f"{number} を電話帳に登録しました。{msg_extra}")


@router.post("/clear")
async def clear_logs(
    direction: str = Form("in"),
    days: int = Form(0),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """履歴を削除する。days>0 ならその日数より古いものだけ消す。"""
    stmt = delete(CallLog)
    if direction in ("in", "out"):
        stmt = stmt.where(CallLog.direction == direction)
    if days > 0:
        stmt = stmt.where(CallLog.started_at < datetime.now() - timedelta(days=days))
    await db.execute(stmt)
    await db.commit()
    label = "より古い履歴" if days > 0 else "履歴"
    return _back(direction, f"{label}を削除しました。")


def _back(direction: str, message: str) -> RedirectResponse:
    from urllib.parse import quote

    d = direction if direction in ("in", "out") else "in"
    return RedirectResponse(
        f"/call-logs/?direction={d}&message={quote(message)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
