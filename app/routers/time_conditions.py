"""時間条件 CRUD。

営業時間内/時間外で着信の転送先を自動的に切り替える設定を作成する。
v0.3.42 で、曜日ごとに「日次パターン (DayPattern)」を割り当てる方式に
刷新。祝日・盆休み・年末年始等は「カレンダー例外」画面で一元管理し、
全ての時間条件から自動的に考慮される。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import DayPattern, Extension, Ivr, Queue, RingGroup, TimeCondition
from app.schemas.time_condition import TimeConditionForm

router = APIRouter(prefix="/time-conditions", tags=["time_conditions"])


async def _dest_choices(db: AsyncSession) -> dict[str, list[dict[str, str]]]:
    """内側/外側の転送先ドロップダウンに使う選択肢を種別ごとにまとめる。"""
    exts = (
        await db.scalars(
            select(Extension).where(Extension.enabled.is_(True)).order_by(Extension.extension)
        )
    ).all()
    ring_groups = (
        await db.scalars(select(RingGroup).where(RingGroup.enabled.is_(True)).order_by(RingGroup.group_number))
    ).all()
    queues = (
        await db.scalars(select(Queue).where(Queue.enabled.is_(True)).order_by(Queue.queue_number))
    ).all()
    ivrs = (
        await db.scalars(select(Ivr).order_by(Ivr.name))
    ).all()
    vm_exts = (
        await db.scalars(
            select(Extension).where(Extension.voicemail_enabled.is_(True))
            .order_by(Extension.extension)
        )
    ).all()
    return {
        "extension": [{"value": e.extension, "label": f"{e.extension} {e.display_name}"} for e in exts],
        "ring_group": [{"value": g.group_number, "label": f"{g.group_number} {g.name}"} for g in ring_groups],
        "queue": [{"value": q.queue_number, "label": f"{q.queue_number} {q.name}"} for q in queues],
        "ivr": [{"value": i.name, "label": i.display_name} for i in ivrs],
        "voicemail": [{"value": e.extension, "label": f"{e.extension} {e.display_name}"} for e in vm_exts],
    }


async def _pattern_choices(db: AsyncSession) -> list[dict[str, str]]:
    rows = (
        await db.scalars(select(DayPattern).where(DayPattern.enabled.is_(True)).order_by(DayPattern.name))
    ).all()
    return [{"value": str(p.id), "label": p.display_name} for p in rows]


@router.get("/", response_class=HTMLResponse)
async def list_time_conditions(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    rows = (await db.scalars(select(TimeCondition).order_by(TimeCondition.name))).all()
    return request.app.state.templates.TemplateResponse(
        request, "time_conditions/list.html",
        {"items": rows, "title": "時間条件"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_time_condition_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    pattern_choices = await _pattern_choices(db)
    return request.app.state.templates.TemplateResponse(
        request, "time_conditions/form.html",
        {
            "item": None,
            "form_action": "/time-conditions/",
            "title": "時間条件の追加",
            "dest_choices": await _dest_choices(db),
            "pattern_choices": pattern_choices,
            "no_patterns": not pattern_choices,
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create_time_condition(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    raw = await request.form()
    return await _save(request, db, raw, None)


@router.get("/{tc_id}/edit", response_class=HTMLResponse)
async def edit_time_condition_form(
    tc_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(TimeCondition, tc_id)
    if obj is None:
        raise HTTPException(status_code=404)
    pattern_choices = await _pattern_choices(db)
    return request.app.state.templates.TemplateResponse(
        request, "time_conditions/form.html",
        {
            "item": obj,
            "form_action": f"/time-conditions/{tc_id}",
            "title": f"時間条件の編集 — {obj.name}",
            "dest_choices": await _dest_choices(db),
            "pattern_choices": pattern_choices,
            "no_patterns": not pattern_choices,
        },
    )


@router.post("/{tc_id}", response_class=HTMLResponse)
async def update_time_condition(
    tc_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(TimeCondition, tc_id)
    if obj is None:
        raise HTTPException(status_code=404)
    raw = await request.form()
    return await _save(request, db, raw, obj)


@router.post("/{tc_id}/delete")
async def delete_time_condition(
    tc_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(TimeCondition, tc_id)
    if obj is None:
        raise HTTPException(status_code=404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/time-conditions/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------


def _parse_pattern_id(raw_val) -> int | None:  # type: ignore[no-untyped-def]
    v = (raw_val or "").strip()
    return int(v) if v else None


async def _save(request, db: AsyncSession, raw, instance: TimeCondition | None):  # type: ignore[no-untyped-def]
    inside_type = raw.get("inside_type", "extension")
    outside_type = raw.get("outside_type", "voicemail")
    inside_value = raw.get("inside_value") or ""
    outside_value = raw.get("outside_value") or ""
    if inside_type == "hangup":
        inside_value = "-"
    if outside_type == "hangup":
        outside_value = "-"

    data = {
        "name": raw.get("name", ""),
        "mon_pattern_id": _parse_pattern_id(raw.get("mon_pattern_id")),
        "tue_pattern_id": _parse_pattern_id(raw.get("tue_pattern_id")),
        "wed_pattern_id": _parse_pattern_id(raw.get("wed_pattern_id")),
        "thu_pattern_id": _parse_pattern_id(raw.get("thu_pattern_id")),
        "fri_pattern_id": _parse_pattern_id(raw.get("fri_pattern_id")),
        "sat_pattern_id": _parse_pattern_id(raw.get("sat_pattern_id")),
        "sun_pattern_id": _parse_pattern_id(raw.get("sun_pattern_id")),
        "use_calendar_exceptions": "use_calendar_exceptions" in raw,
        "inside_type": inside_type,
        "inside_value": inside_value,
        "outside_type": outside_type,
        "outside_value": outside_value,
        "enabled": "enabled" in raw,
        "note": raw.get("note") or None,
    }

    try:
        validated = TimeConditionForm.model_validate(data)
        errors: list[dict] = []
    except ValidationError as ve:
        validated = None
        errors = ve.errors()

    if not errors:
        if inside_type != "hangup" and not inside_value:
            errors.append({
                "loc": ("inside_value",), "msg": "時間内の転送先を選択してください。",
                "type": "value_error",
            })
        if outside_type != "hangup" and not outside_value:
            errors.append({
                "loc": ("outside_value",), "msg": "時間外の転送先を選択してください。",
                "type": "value_error",
            })

    if not errors:
        existing = (
            await db.scalars(select(TimeCondition).where(TimeCondition.name == data["name"]))
        ).first()
        if existing and (instance is None or existing.id != instance.id):
            errors.append({
                "loc": ("name",),
                "msg": "この名前は既に使われています。別の名前にしてください。",
                "type": "value_error",
            })

    if errors:
        pattern_choices = await _pattern_choices(db)
        return request.app.state.templates.TemplateResponse(
            request, "time_conditions/form.html",
            {
                "item": instance,
                "form_action": (
                    f"/time-conditions/{instance.id}" if instance else "/time-conditions/"
                ),
                "title": "入力エラー",
                "errors": errors,
                "submitted": data,
                "dest_choices": await _dest_choices(db),
                "pattern_choices": pattern_choices,
                "no_patterns": not pattern_choices,
            },
            status_code=400,
        )

    assert validated is not None
    payload = validated.model_dump()

    if instance is None:
        instance = TimeCondition(**payload)
        db.add(instance)
    else:
        for k, v in payload.items():
            setattr(instance, k, v)

    await db.commit()
    return RedirectResponse("/time-conditions/", status_code=status.HTTP_303_SEE_OTHER)
