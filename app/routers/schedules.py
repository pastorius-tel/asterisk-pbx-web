"""スケジュール統合管理画面。

日次パターン (DayPattern) / カレンダー例外 (CalendarException) /
時間条件 (TimeCondition) の 3 つを 1 画面にまとめ、表形式でその場で
追加・編集・削除できるようにする。

個別の専用画面 (/day-patterns/ 等) も引き続き利用できるが、通常は
こちらの統合画面だけで設定が完結する。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    CalendarException,
    CompanyHoliday,
    DayPattern,
    Extension,
    Ivr,
    NationalHoliday,
    Queue,
    RingGroup,
    TimeCondition,
)
from app.routers._helpers import form_int
from app.validators import validate_time_ranges

router = APIRouter(prefix="/schedules", tags=["schedules"])

_WEEKDAYS = (
    ("mon", "月"), ("tue", "火"), ("wed", "水"), ("thu", "木"),
    ("fri", "金"), ("sat", "土"), ("sun", "日"),
)

# 時間条件の名前は [timecond-<name>] というダイヤルプランのコンテキスト名に
# そのまま使われる。Asterisk のコンテキスト名に日本語などは使えないため、
# 半角英数字・ハイフン・アンダースコアのみに制限する。
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 営業時間帯は HH:MM-HH:MM 形式 (カンマ区切りで複数可)。
# "0900-1700" のようにコロンを欠いた指定は Asterisk の GotoIfTime が
# 解釈できず、時間判定が常に失敗するため弾く。


def _validate_on_ranges(value: str) -> str | None:
    """営業時間帯の書式を検証する。問題なければ None、あればエラー文言を返す。

    実体は app/validators.py の共通ルール (日次パターン画面と同じ検証)。
    """
    return validate_time_ranges(value)


async def _dest_choices(db: AsyncSession) -> list[dict[str, str]]:
    """時間条件の転送先ドロップダウン用に、全種別をフラットな
    "種別:値" 形式の選択肢としてまとめる。"""
    out: list[dict[str, str]] = []
    exts = (
        await db.scalars(
            select(Extension).where(Extension.enabled.is_(True)).order_by(Extension.extension)
        )
    ).all()
    for e in exts:
        out.append({"value": f"extension:{e.extension}", "label": f"内線 {e.extension} {e.display_name}"})
    for g in (
        await db.scalars(select(RingGroup).where(RingGroup.enabled.is_(True)).order_by(RingGroup.group_number))
    ).all():
        out.append({"value": f"ring_group:{g.group_number}", "label": f"リンググループ {g.group_number} {g.name}"})
    for q in (
        await db.scalars(select(Queue).where(Queue.enabled.is_(True)).order_by(Queue.queue_number))
    ).all():
        out.append({"value": f"queue:{q.queue_number}", "label": f"キュー {q.queue_number} {q.name}"})
    for i in (await db.scalars(select(Ivr).order_by(Ivr.name))).all():
        out.append({"value": f"ivr:{i.name}", "label": f"IVR {i.display_name}"})
    for e in (
        await db.scalars(
            select(Extension).where(Extension.voicemail_enabled.is_(True)).order_by(Extension.extension)
        )
    ).all():
        out.append({"value": f"voicemail:{e.extension}", "label": f"留守番電話 {e.extension} {e.display_name}"})
    out.append({"value": "hangup:-", "label": "切断"})
    return out


def _redirect_with_errors(errors: list[str]) -> RedirectResponse:
    """保存後のリダイレクト。入力エラーがあれば画面に表示するため
    クエリに載せる。"""
    if errors:
        msg = " / ".join(errors[:3])
        return RedirectResponse(
            f"/schedules/?error={quote(msg)}", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse("/schedules/", status_code=status.HTTP_303_SEE_OTHER)


def _fmt_date_value(year: int | None, month: int, day: int, fallback_year: int) -> str:
    """<input type="date"> に渡す YYYY-MM-DD 文字列を作る。
    毎年繰り返し (year=None) の場合は表示用のダミー年を使う。"""
    y = year or fallback_year
    return f"{y:04d}-{month:02d}-{day:02d}"


@router.get("/", response_class=HTMLResponse)
async def schedules_home(
    request: Request, error: str | None = None,
    hol_message: str | None = None, hol_ok: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    patterns = (await db.scalars(select(DayPattern).order_by(DayPattern.name))).all()
    exceptions = (
        await db.scalars(
            select(CalendarException).order_by(
                CalendarException.priority,
                CalendarException.start_month,
                CalendarException.start_day,
            )
        )
    ).all()
    conditions = (await db.scalars(select(TimeCondition).order_by(TimeCondition.name))).all()

    # 祝日 (今年ぶんを一覧表示する)
    from datetime import datetime as _dt
    this_year = _dt.now().year
    national_holidays = (
        await db.scalars(
            select(NationalHoliday).where(NationalHoliday.year == this_year)
            .order_by(NationalHoliday.holiday_date)
        )
    ).all()
    national_holiday_total = len(
        (await db.scalars(select(NationalHoliday))).all()
    )
    # 会社指定休日
    company_holidays = (
        await db.scalars(
            select(CompanyHoliday)
            .order_by(CompanyHoliday.start_month, CompanyHoliday.start_day)
        )
    ).all()
    company_rows = []
    for h in company_holidays:
        y = h.year or 2000
        end_y = y if h.end_month >= h.start_month else y + 1
        company_rows.append({
            "obj": h,
            "start_date": f"{y:04d}-{h.start_month:02d}-{h.start_day:02d}",
            "end_date": f"{end_y:04d}-{h.end_month:02d}-{h.end_day:02d}",
        })

    exc_rows = []
    for e in exceptions:
        start_v = _fmt_date_value(e.year, e.start_month, e.start_day, 2000)
        end_year = (e.year or 2000) if e.end_month >= e.start_month else (e.year or 2000) + 1
        exc_rows.append({
            "obj": e,
            "start_date": start_v,
            "end_date": f"{end_year:04d}-{e.end_month:02d}-{e.end_day:02d}",
        })

    return request.app.state.templates.TemplateResponse(
        request, "schedules/index.html",
        {
            "title": "スケジュール設定",
            "error_message": error,
            "hol_message": hol_message,
            "hol_ok": hol_ok,
            "national_holidays": national_holidays,
            "national_holiday_total": national_holiday_total,
            "this_year": this_year,
            "company_rows": company_rows,
            "patterns": patterns,
            "exception_rows": exc_rows,
            "conditions": conditions,
            "pattern_choices": [
                {"value": str(p.id), "label": p.display_name}
                for p in patterns if p.enabled
            ],
            "dest_choices": await _dest_choices(db),
            "weekdays": _WEEKDAYS,
        },
    )


# ---------------------------------------------------------------------------
# 日次パターン
# ---------------------------------------------------------------------------


@router.post("/patterns/save")
async def save_patterns(
    request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    """表に並んだ全行をまとめて保存する。id が空の行は新規追加。"""
    raw = await request.form()
    ids = raw.getlist("p_id")
    names = raw.getlist("p_name")
    displays = raw.getlist("p_display_name")
    ranges = raw.getlist("p_on_ranges")
    enableds = raw.getlist("p_enabled")
    errors: list[str] = []

    for idx, pid in enumerate(ids):
        name = (names[idx] if idx < len(names) else "").strip()
        display = (displays[idx] if idx < len(displays) else "").strip()
        on_ranges = (ranges[idx] if idx < len(ranges) else "").strip()
        enabled = str(idx) in enableds
        if not name and not display:
            continue  # 完全に空の行はスキップ (新規行を使わなかった場合)
        if not name or not display:
            # 以前はここで黙って捨てていたため、片方だけ入力して保存すると
            # 「保存できたように見えて何も登録されていない」状態になった。
            errors.append(
                f"「{name or display}」の行は、パターン名と表示名の"
                "両方を入力してください。"
            )
            continue
        if not _NAME_RE.match(name):
            errors.append(
                f"パターン名「{name}」は使えません。"
                "半角英数字・ハイフン・アンダースコアのみで入力してください。"
            )
            continue
        range_err = _validate_on_ranges(on_ranges)
        if range_err:
            errors.append(f"「{display}」の{range_err}")
            continue

        if pid:
            pid_int = form_int(pid)
            obj = await db.get(DayPattern, pid_int) if pid_int else None
            if obj is None:
                continue
            obj.name = name
            obj.display_name = display
            obj.on_ranges = on_ranges
            obj.enabled = enabled
        else:
            db.add(DayPattern(
                name=name, display_name=display, on_ranges=on_ranges, enabled=enabled,
            ))
    await db.commit()
    return _redirect_with_errors(errors)


@router.post("/patterns/{p_id}/delete")
async def delete_pattern(p_id: int, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    obj = await db.get(DayPattern, p_id)
    if obj is not None:
        await db.delete(obj)
        await db.commit()
    return RedirectResponse("/schedules/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# カレンダー例外
# ---------------------------------------------------------------------------


@router.post("/exceptions/save")
async def save_exceptions(
    request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    raw = await request.form()
    ids = raw.getlist("c_id")
    names = raw.getlist("c_name")
    starts = raw.getlist("c_start_date")
    ends = raw.getlist("c_end_date")
    recurrings = raw.getlist("c_recurring")
    pattern_ids = raw.getlist("c_pattern_id")
    priorities = raw.getlist("c_priority")
    enableds = raw.getlist("c_enabled")

    for idx, cid in enumerate(ids):
        name = (names[idx] if idx < len(names) else "").strip()
        start_s = (starts[idx] if idx < len(starts) else "").strip()
        end_s = (ends[idx] if idx < len(ends) else "").strip()
        pattern_id_s = (pattern_ids[idx] if idx < len(pattern_ids) else "").strip()
        if not name and not start_s:
            continue
        if not name or not start_s or not end_s or not pattern_id_s:
            continue
        try:
            sy, sm, sd = (int(x) for x in start_s.split("-"))
            _ey, em, ed = (int(x) for x in end_s.split("-"))
            pattern_id = int(pattern_id_s)
            priority = int((priorities[idx] if idx < len(priorities) else "100") or 100)
        except ValueError:
            continue

        recurring = str(idx) in recurrings
        enabled = str(idx) in enableds
        year = None if recurring else sy

        if cid:
            obj = await db.get(CalendarException, int(cid))
            if obj is None:
                continue
            obj.name = name
            obj.start_month, obj.start_day = sm, sd
            obj.end_month, obj.end_day = em, ed
            obj.year = year
            obj.pattern_id = pattern_id
            obj.priority = priority
            obj.enabled = enabled
        else:
            db.add(CalendarException(
                name=name, start_month=sm, start_day=sd, end_month=em, end_day=ed,
                year=year, pattern_id=pattern_id, priority=priority, enabled=enabled,
            ))
    await db.commit()
    return RedirectResponse("/schedules/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/exceptions/{c_id}/delete")
async def delete_exception(c_id: int, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    obj = await db.get(CalendarException, c_id)
    if obj is not None:
        await db.delete(obj)
        await db.commit()
    return RedirectResponse("/schedules/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# 時間条件
# ---------------------------------------------------------------------------


def _parse_pattern_id(raw_val: str | None) -> int | None:
    """フォームの日次パターン選択値を int または None に変換する。

    「(休)」「(終日休業)」を選んだときは空文字で送られてくるので None
    (パターン未割り当て = 終日休業扱い) にする。
    """
    v = (raw_val or "").strip()
    return int(v) if v.isdigit() else None


def _split_dest(value: str) -> tuple[str, str]:
    """"種別:値" を (種別, 値) に分解する。"""
    if ":" not in value:
        return ("hangup", "-")
    kind, _, val = value.partition(":")
    return (kind, val)


@router.post("/conditions/save")
async def save_conditions(
    request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    raw = await request.form()
    ids = raw.getlist("t_id")
    names = raw.getlist("t_name")
    insides = raw.getlist("t_inside")
    outsides = raw.getlist("t_outside")
    use_nationals = raw.getlist("t_use_national")
    use_companies = raw.getlist("t_use_company")
    national_patterns = raw.getlist("t_national_pattern_id")
    company_patterns = raw.getlist("t_company_pattern_id")
    enableds = raw.getlist("t_enabled")
    weekday_lists = {
        f: raw.getlist(f"t_{f}_pattern_id") for f, _jp in _WEEKDAYS
    }

    errors: list[str] = []
    for idx, tid in enumerate(ids):
        name = (names[idx] if idx < len(names) else "").strip()
        if not name:
            continue
        if not _NAME_RE.match(name):
            errors.append(
                f"時間条件の名前「{name}」は使えません。"
                "ダイヤルプランの内部名として使われるため、"
                "半角英数字・ハイフン・アンダースコアのみで入力してください "
                "(例: business-hours)。"
            )
            continue
        inside_type, inside_value = _split_dest(
            insides[idx] if idx < len(insides) else "hangup:-"
        )
        outside_type, outside_value = _split_dest(
            outsides[idx] if idx < len(outsides) else "hangup:-"
        )
        use_national = str(idx) in use_nationals
        use_company = str(idx) in use_companies
        nat_pat = _parse_pattern_id(national_patterns[idx] if idx < len(national_patterns) else "")
        com_pat = _parse_pattern_id(company_patterns[idx] if idx < len(company_patterns) else "")
        enabled = str(idx) in enableds

        weekday_values: dict[str, int | None] = {}
        for f, _jp in _WEEKDAYS:
            lst = weekday_lists[f]
            v = (lst[idx] if idx < len(lst) else "").strip()
            weekday_values[f"{f}_pattern_id"] = _parse_pattern_id(v)

        if tid:
            obj = await db.get(TimeCondition, int(tid))
            if obj is None:
                continue
            obj.name = name
            obj.inside_type, obj.inside_value = inside_type, inside_value
            obj.outside_type, obj.outside_value = outside_type, outside_value
            obj.use_national_holidays = use_national
            obj.use_company_holidays = use_company
            obj.national_holiday_pattern_id = nat_pat
            obj.company_holiday_pattern_id = com_pat
            obj.enabled = enabled
            for k, v2 in weekday_values.items():
                setattr(obj, k, v2)
        else:
            db.add(TimeCondition(
                name=name,
                inside_type=inside_type, inside_value=inside_value,
                outside_type=outside_type, outside_value=outside_value,
                use_national_holidays=use_national,
                use_company_holidays=use_company, enabled=enabled,
                national_holiday_pattern_id=nat_pat,
                company_holiday_pattern_id=com_pat,
                **weekday_values,
            ))
    await db.commit()
    return _redirect_with_errors(errors)


@router.post("/conditions/{t_id}/delete")
async def delete_condition(t_id: int, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    obj = await db.get(TimeCondition, t_id)
    if obj is not None:
        await db.delete(obj)
        await db.commit()
    return RedirectResponse("/schedules/", status_code=status.HTTP_303_SEE_OTHER)
