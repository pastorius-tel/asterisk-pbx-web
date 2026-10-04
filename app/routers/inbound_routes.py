"""着信ルート CRUD。

外線からの着信 (DID) をどこへ流すかを設定する。
転送先: 内線 / リンググループ / キュー / IVR / ボイスメール / FAX / 切断
内線転送時は「応答なし時のフォールバック」(ボイスメール等) も設定可能。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Extension, InboundRoute, Ivr, Queue, RingGroup, TimeCondition
from app.routers._helpers import all_enum_choices
from app.schemas.route import InboundRouteForm

router = APIRouter(prefix="/inbound-routes", tags=["inbound_routes"])


async def _dest_choices(db: AsyncSession) -> dict[str, list[dict[str, str]]]:
    """転送先タイプごとの候補リストをまとめて返す。

    フォームの JS が destination_type に応じて出し分ける。
    """
    exts = (
        await db.scalars(
            select(Extension).where(Extension.enabled.is_(True)).order_by(Extension.extension)
        )
    ).all()
    ivrs = (await db.scalars(select(Ivr).order_by(Ivr.name))).all()
    queues = (await db.scalars(select(Queue).order_by(Queue.name))).all()
    rgs = (await db.scalars(select(RingGroup).order_by(RingGroup.name))).all()
    tcs = (
        await db.scalars(
            select(TimeCondition).where(TimeCondition.enabled.is_(True))
            .order_by(TimeCondition.name)
        )
    ).all()

    return {
        "ext_choices": [
            {"value": e.extension, "label": f"{e.extension} {e.display_name}"}
            for e in exts
        ],
        "vm_choices": [
            {"value": e.extension, "label": f"{e.extension} {e.display_name}"}
            for e in exts
            if e.voicemail_enabled
        ],
        "ivr_choices": [
            # IVR のダイヤルプラン上のコンテキスト名は ivr-{name} (id ではない) なので
            # Goto(ivr-{val},s,1) が正しく解決できるよう value は name にする。
            {"value": i.name, "label": i.name} for i in ivrs
        ],
        "queue_choices": [
            # queues.conf のセクション名は [queue_number] (id ではない) なので
            # Queue(val) が正しいキューに繋がるよう value は queue_number にする。
            {"value": str(q.queue_number), "label": q.name} for q in queues
        ],
        "rg_choices": [
            # リンググループのダイヤルプラン上のコンテキスト名は
            # ring-group-{group_number} (id ではない) なので、
            # Goto(ring-group-{val},s,1) が正しく解決できるよう
            # value は group_number にする。
            {"value": r.group_number, "label": f"{r.group_number} {r.name}"} for r in rgs
        ],
        "tc_choices": [
            # 時間条件のダイヤルプラン上のコンテキスト名は timecond-{name}
            # (id ではない) なので、Goto(timecond-{val},s,1) が正しく解決
            # できるよう value は name にする。
            {"value": t.name, "label": t.name} for t in tcs
        ],
    }


@router.get("/", response_class=HTMLResponse)
async def list_routes(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    rows = (
        await db.scalars(
            select(InboundRoute).order_by(InboundRoute.priority, InboundRoute.id)
        )
    ).all()
    return request.app.state.templates.TemplateResponse(
        request,
        "inbound_routes/list.html",
        {"items": rows, "title": "着信ルート一覧"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "inbound_routes/form.html",
        {
            "item": None,
            "form_action": "/inbound-routes/",
            "title": "着信ルートの追加",
            **all_enum_choices(),
            **await _dest_choices(db),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    raw = dict(await request.form())
    return await _save(request, db, raw, instance=None)


@router.get("/{rid}/edit", response_class=HTMLResponse)
async def edit_form(
    rid: int, request: Request, db: AsyncSession = Depends(get_db)
):
    obj = await db.get(InboundRoute, rid)
    if obj is None:
        raise HTTPException(404)
    return request.app.state.templates.TemplateResponse(
        request,
        "inbound_routes/form.html",
        {
            "item": obj,
            "form_action": f"/inbound-routes/{rid}",
            "title": f"着信ルートの編集 — {obj.name}",
            **all_enum_choices(),
            **await _dest_choices(db),
        },
    )


@router.post("/{rid}", response_class=HTMLResponse)
async def update(
    rid: int, request: Request, db: AsyncSession = Depends(get_db)
):
    obj = await db.get(InboundRoute, rid)
    if obj is None:
        raise HTTPException(404)
    raw = dict(await request.form())
    return await _save(request, db, raw, instance=obj)


@router.post("/{rid}/delete")
async def delete(rid: int, db: AsyncSession = Depends(get_db)):
    obj = await db.get(InboundRoute, rid)
    if obj is None:
        raise HTTPException(404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse(
        "/inbound-routes/", status_code=status.HTTP_303_SEE_OTHER
    )


async def _save(
    request, db: AsyncSession, data: dict, instance: InboundRoute | None
):
    data["record_call"] = "record_call" in data
    data["enabled"] = "enabled" in data
    # 空文字 → None 正規化。
    # 併せて、文字列 "None" もここで弾く。過去のテンプレートのバグで
    # 編集フォームの未入力欄に None がリテラル表示され、気付かず
    # そのまま保存すると DB に本物の文字列 "None" が入ってしまっていた
    # (v0.3.7 でフォーム側は修正済みだが、保存経路側にも保険を置く)。
    for k in (
        "did_number", "caller_id_pattern", "cid_prefix", "note",
        "no_answer_type", "no_answer_value",
    ):
        if data.get(k) in ("", "None", None):
            data[k] = None
    for k in ("ring_seconds", "priority"):
        if k in data:
            try:
                data[k] = int(data[k])
            except (TypeError, ValueError):
                pass

    try:
        validated = InboundRouteForm.model_validate(data)
    except ValidationError as ve:
        return request.app.state.templates.TemplateResponse(
            request,
            "inbound_routes/form.html",
            {
                "item": instance,
                "form_action": (
                    f"/inbound-routes/{instance.id}"
                    if instance
                    else "/inbound-routes/"
                ),
                "title": "入力エラー",
                "errors": ve.errors(),
                "submitted": data,
                **all_enum_choices(),
                **await _dest_choices(db),
            },
            status_code=400,
        )

    payload = validated.model_dump()
    # RouteAction enum → 値(str) に
    dt = payload.get("destination_type")
    if hasattr(dt, "value"):
        payload["destination_type"] = dt.value

    if instance is None:
        instance = InboundRoute(**payload)
        db.add(instance)
    else:
        for k, v in payload.items():
            setattr(instance, k, v)
    await db.commit()
    return RedirectResponse(
        "/inbound-routes/", status_code=status.HTTP_303_SEE_OTHER
    )
