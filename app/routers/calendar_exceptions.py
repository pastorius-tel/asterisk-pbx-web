"""カレンダー例外 CRUD。

祝日・盆休み・年末年始など、特定の日付 (範囲) に通常の曜日設定を
上書きして特定のパターンを適用する設定を管理する。日付は HTML5 の
<input type="date"> (ブラウザ標準のカレンダーピッカー) で入力する。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import CalendarException, DayPattern
from app.schemas.calendar_exception import CalendarExceptionForm

router = APIRouter(prefix="/calendar-exceptions", tags=["calendar_exceptions"])

_MONTH_JP = {
    1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
    7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月",
}


def _fmt_md(month: int, day: int) -> str:
    return f"{_MONTH_JP.get(month, month)}{day}日"


async def _pattern_choices(db: AsyncSession) -> list[dict[str, str]]:
    rows = (
        await db.scalars(select(DayPattern).where(DayPattern.enabled.is_(True)).order_by(DayPattern.name))
    ).all()
    return [{"value": str(p.id), "label": p.display_name} for p in rows]


@router.get("/", response_class=HTMLResponse)
async def list_calendar_exceptions(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    rows = (
        await db.scalars(select(CalendarException).order_by(CalendarException.priority, CalendarException.start_month, CalendarException.start_day))
    ).all()
    return request.app.state.templates.TemplateResponse(
        request, "calendar_exceptions/list.html",
        {"items": rows, "title": "カレンダー例外 (祝日・休業日)", "fmt_md": _fmt_md},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_calendar_exception_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    choices = await _pattern_choices(db)
    return request.app.state.templates.TemplateResponse(
        request, "calendar_exceptions/form.html",
        {
            "item": None, "form_action": "/calendar-exceptions/", "title": "カレンダー例外の追加",
            "pattern_choices": choices, "no_patterns": not choices,
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create_calendar_exception(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    raw = await request.form()
    return await _save(request, db, raw, None)


@router.get("/{c_id}/edit", response_class=HTMLResponse)
async def edit_calendar_exception_form(
    c_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(CalendarException, c_id)
    if obj is None:
        raise HTTPException(status_code=404)
    # 編集画面用に year/month/day を YYYY-MM-DD の date input 値へ変換
    display_year = obj.year or 2000  # 毎年繰り返しの場合はダミー年で表示
    start_date = f"{display_year:04d}-{obj.start_month:02d}-{obj.start_day:02d}"
    end_year = display_year if obj.end_month >= obj.start_month else display_year + 1
    end_date = f"{end_year:04d}-{obj.end_month:02d}-{obj.end_day:02d}"
    return request.app.state.templates.TemplateResponse(
        request, "calendar_exceptions/form.html",
        {
            "item": obj, "form_action": f"/calendar-exceptions/{c_id}",
            "title": f"カレンダー例外の編集 — {obj.name}",
            "pattern_choices": await _pattern_choices(db), "no_patterns": False,
            "start_date_value": start_date, "end_date_value": end_date,
        },
    )


@router.post("/{c_id}", response_class=HTMLResponse)
async def update_calendar_exception(
    c_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(CalendarException, c_id)
    if obj is None:
        raise HTTPException(status_code=404)
    raw = await request.form()
    return await _save(request, db, raw, obj)


@router.post("/{c_id}/delete")
async def delete_calendar_exception(
    c_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(CalendarException, c_id)
    if obj is None:
        raise HTTPException(status_code=404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/calendar-exceptions/", status_code=status.HTTP_303_SEE_OTHER)


async def _save(request, db: AsyncSession, raw, instance: CalendarException | None):  # type: ignore[no-untyped-def]
    data = {
        "name": raw.get("name", ""),
        "start_date": raw.get("start_date", ""),
        "end_date": raw.get("end_date", ""),
        "recurring": "recurring" in raw,
        "pattern_id": raw.get("pattern_id") or 0,
        "priority": raw.get("priority") or 100,
        "enabled": "enabled" in raw,
        "note": raw.get("note") or None,
    }

    try:
        pattern_id_int = int(data["pattern_id"])
        data["pattern_id"] = pattern_id_int
        validated = CalendarExceptionForm.model_validate(data)
        errors: list[dict] = []
    except (ValidationError, ValueError) as ve:
        validated = None
        errors = ve.errors() if isinstance(ve, ValidationError) else [
            {"loc": ("pattern_id",), "msg": "適用パターンを選択してください。", "type": "value_error"}
        ]

    if not errors:
        pattern = await db.get(DayPattern, data["pattern_id"])
        if pattern is None:
            errors.append({"loc": ("pattern_id",), "msg": "選択したパターンが見つかりません。", "type": "value_error"})

    if errors:
        return request.app.state.templates.TemplateResponse(
            request, "calendar_exceptions/form.html",
            {
                "item": instance,
                "form_action": f"/calendar-exceptions/{instance.id}" if instance else "/calendar-exceptions/",
                "title": "入力エラー",
                "errors": errors,
                "submitted": data,
                "pattern_choices": await _pattern_choices(db),
                "no_patterns": False,
                "start_date_value": data["start_date"],
                "end_date_value": data["end_date"],
            },
            status_code=400,
        )

    assert validated is not None
    payload = validated.model_dump(exclude={"start_date", "end_date", "recurring"})

    if instance is None:
        instance = CalendarException(**payload)
        db.add(instance)
    else:
        for k, v in payload.items():
            setattr(instance, k, v)

    await db.commit()
    return RedirectResponse("/calendar-exceptions/", status_code=status.HTTP_303_SEE_OTHER)
