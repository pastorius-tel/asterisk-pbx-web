"""日次パターン CRUD。

「1日の中でON(営業時間)となる時間帯」を再利用可能なテンプレートとして
管理する。時間条件の曜日割り当てや、カレンダー例外の適用先として使う。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import DayPattern
from app.schemas.day_pattern import DayPatternForm

router = APIRouter(prefix="/day-patterns", tags=["day_patterns"])


@router.get("/", response_class=HTMLResponse)
async def list_day_patterns(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    rows = (await db.scalars(select(DayPattern).order_by(DayPattern.name))).all()
    return request.app.state.templates.TemplateResponse(
        request, "day_patterns/list.html",
        {"items": rows, "title": "日次パターン"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_day_pattern_form(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "day_patterns/form.html",
        {"item": None, "form_action": "/day-patterns/", "title": "日次パターンの追加"},
    )


@router.post("/", response_class=HTMLResponse)
async def create_day_pattern(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    raw = await request.form()
    return await _save(request, db, raw, None)


@router.get("/{p_id}/edit", response_class=HTMLResponse)
async def edit_day_pattern_form(
    p_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(DayPattern, p_id)
    if obj is None:
        raise HTTPException(status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "day_patterns/form.html",
        {"item": obj, "form_action": f"/day-patterns/{p_id}", "title": f"日次パターンの編集 — {obj.display_name}"},
    )


@router.post("/{p_id}", response_class=HTMLResponse)
async def update_day_pattern(
    p_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(DayPattern, p_id)
    if obj is None:
        raise HTTPException(status_code=404)
    raw = await request.form()
    return await _save(request, db, raw, obj)


@router.post("/{p_id}/delete")
async def delete_day_pattern(p_id: int, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    obj = await db.get(DayPattern, p_id)
    if obj is None:
        raise HTTPException(status_code=404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/day-patterns/", status_code=status.HTTP_303_SEE_OTHER)


async def _save(request, db: AsyncSession, raw, instance: DayPattern | None):  # type: ignore[no-untyped-def]
    # チェックボックスで選んだ時間帯ペア (on_start[]/on_end[]) を
    # "HH:MM-HH:MM" のカンマ区切り文字列に組み立てる。
    starts = raw.getlist("on_start")
    ends = raw.getlist("on_end")
    pairs = []
    incomplete = False
    # 開始/終了は必ず対で送られてくるが、片方だけ欠けた不正な POST でも
    # 落ちないよう strict=False (短い方に合わせる) を明示する
    for s, e in zip(starts, ends, strict=False):
        s, e = s.strip(), e.strip()
        if s and e:
            pairs.append(f"{s}-{e}")
        elif s or e:
            # 片方だけ入力されている行。以前はここで黙って捨てていたため、
            # 終了時刻の入力を忘れると **時間帯が 1 つも無い = 終日休業**
            # として保存され、「営業時間を設定したのに電話が繋がらない」
            # という分かりにくい事故になっていた。
            incomplete = True
    on_ranges = ",".join(pairs)

    data = {
        "name": raw.get("name", ""),
        "display_name": raw.get("display_name", ""),
        "on_ranges": on_ranges,
        "enabled": "enabled" in raw,
        "note": raw.get("note") or None,
    }

    try:
        validated = DayPatternForm.model_validate(data)
        errors: list[dict] = []
    except ValidationError as ve:
        validated = None
        errors = ve.errors()

    if incomplete:
        errors.append({
            "loc": ("on_ranges",),
            "msg": "時間帯は開始と終了の両方を入力してください "
                   "(片方だけの行があります)。その行が不要な場合は"
                   "「削除」を押してから保存してください。",
            "type": "value_error",
        })

    if not errors:
        existing = (
            await db.scalars(select(DayPattern).where(DayPattern.name == data["name"]))
        ).first()
        if existing and (instance is None or existing.id != instance.id):
            errors.append({
                "loc": ("name",), "msg": "この名前は既に使われています。",
                "type": "value_error",
            })

    if errors:
        return request.app.state.templates.TemplateResponse(
            request, "day_patterns/form.html",
            {
                "item": instance,
                "form_action": f"/day-patterns/{instance.id}" if instance else "/day-patterns/",
                "title": "入力エラー",
                "errors": errors,
                "submitted": data,
            },
            status_code=400,
        )

    assert validated is not None
    payload = validated.model_dump()

    if instance is None:
        instance = DayPattern(**payload)
        db.add(instance)
    else:
        for k, v in payload.items():
            setattr(instance, k, v)

    await db.commit()
    return RedirectResponse("/day-patterns/", status_code=status.HTTP_303_SEE_OTHER)
