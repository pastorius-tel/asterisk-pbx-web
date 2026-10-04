"""祝日 / 会社指定休日の管理。

- 国民の祝日 (NationalHoliday): 内閣府が公開している CSV から取り込む
- 会社指定休日 (CompanyHoliday): 年末年始・お盆・GW 等をカレンダーで登録

どちらも「スケジュール設定」画面から操作する。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import CalendarException, CompanyHoliday, NationalHoliday
from app.routers._helpers import form_int
from app.services import holiday_service

router = APIRouter(prefix="/holidays", tags=["holidays"])


def _back(message: str, ok: bool) -> RedirectResponse:
    return RedirectResponse(
        f"/schedules/?hol_message={quote(message)}&hol_ok={'1' if ok else '0'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _store_holidays(
    db: AsyncSession, holidays: list[tuple], keep_years: int = 3
) -> tuple[int, int]:
    """取り込んだ祝日を DB に保存する。

    ダイヤルプランに載せるのは今年・来年ぶんだけなので、DB にも
    「今年から keep_years 年ぶん」だけを保持して肥大化を防ぐ。
    戻り値: (保存件数, 対象年数)
    """
    this_year = datetime.now().year
    target_years = list(range(this_year, this_year + keep_years))
    # 対象年ぶんを入れ替える (再取り込みで重複しないよう一度消す)
    await db.execute(
        delete(NationalHoliday).where(NationalHoliday.year.in_(target_years))
    )
    saved = 0
    for d, name in holidays:
        if d.year not in target_years:
            continue
        db.add(NationalHoliday(holiday_date=d, name=name, year=d.year))
        saved += 1
    await db.commit()
    return (saved, len(target_years))


@router.post("/national/fetch")
async def fetch_national_holidays(
    request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    """内閣府のサイトから祝日 CSV を取得して取り込む。"""
    ok, info, data = await asyncio.to_thread(holiday_service.fetch_holiday_csv)
    if not ok:
        return _back(
            "祝日データを取得できませんでした。サーバーがインターネットに"
            f"接続できるか確認してください ({info})。"
            "接続できない場合は、CSV ファイルを手動でアップロードできます。",
            False,
        )
    holidays, warnings = holiday_service.parse_holiday_csv(data)
    if not holidays:
        return _back("CSV を読み取れませんでした。" + " ".join(warnings[:2]), False)
    saved, years = await _store_holidays(db, holidays)
    return _back(f"祝日データを取り込みました ({saved} 件 / 今年から {years} 年ぶん)。", True)


@router.post("/national/upload")
async def upload_national_holidays(
    request: Request,
    db: AsyncSession = Depends(get_db),
    csv_file: UploadFile = File(...),
) -> RedirectResponse:
    """祝日 CSV を手動アップロードして取り込む。

    サーバーが直接インターネットへ出られない環境向け。内閣府のページから
    ダウンロードした syukujitsu.csv をそのままアップロードする。
    """
    try:
        data = await csv_file.read()
    except Exception as e:  # noqa: BLE001
        return _back(f"ファイルを読み込めませんでした: {e}", False)
    holidays, warnings = holiday_service.parse_holiday_csv(data)
    if not holidays:
        return _back(
            "CSV を読み取れませんでした。内閣府の syukujitsu.csv を"
            "そのままアップロードしてください。" + " ".join(warnings[:2]),
            False,
        )
    saved, years = await _store_holidays(db, holidays)
    return _back(f"祝日データを取り込みました ({saved} 件 / 今年から {years} 年ぶん)。", True)


@router.get("/national/list", response_class=HTMLResponse)
async def national_holiday_list(
    request: Request, year: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """取り込んだ祝日の一覧 (別ウィンドウ表示用)。"""
    rows = (
        await db.scalars(
            select(NationalHoliday).order_by(
                NationalHoliday.year, NationalHoliday.holiday_date
            )
        )
    ).all()
    years = sorted({h.year for h in rows})
    return request.app.state.templates.TemplateResponse(
        request, "schedules/holiday_list.html",
        {"title": "取り込んだ祝日一覧", "holidays": rows, "years": years},
    )


@router.post("/national/clear")
async def clear_national_holidays(
    db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    await db.execute(delete(NationalHoliday))
    await db.commit()
    return _back("祝日データを削除しました。", True)


# ---------------------------------------------------------------------------
# 会社指定休日
# ---------------------------------------------------------------------------


@router.post("/company/save")
async def save_company_holidays(
    request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    """表に並んだ指定休日をまとめて保存する。id が空の行は新規追加。"""
    raw = await request.form()
    ids = raw.getlist("h_id")
    names = raw.getlist("h_name")
    starts = raw.getlist("h_start_date")
    ends = raw.getlist("h_end_date")
    recurrings = raw.getlist("h_recurring")
    enableds = raw.getlist("h_enabled")

    errors: list[str] = []
    for idx, hid in enumerate(ids):
        name = (names[idx] if idx < len(names) else "").strip()
        start_s = (starts[idx] if idx < len(starts) else "").strip()
        end_s = (ends[idx] if idx < len(ends) else "").strip()
        if not name and not start_s:
            continue
        if not name or not start_s or not end_s:
            errors.append(f"「{name or '(名前未入力)'}」は名前・開始日・終了日をすべて入力してください。")
            continue
        try:
            sy, sm, sd = (int(x) for x in start_s.split("-"))
            _ey, em, ed = (int(x) for x in end_s.split("-"))
            # 13月45日のような存在しない日付を弾く
            # (書式は合っていても実在しない値は後で設定生成を壊す)
            from datetime import date as _d
            _d(sy, sm, sd)
            _d(_ey, em, ed)
        except ValueError:
            errors.append(
                f"「{name}」の日付が正しくありません "
                "(実在する日付を選んでください)。"
            )
            continue

        recurring = str(idx) in recurrings
        enabled = str(idx) in enableds
        year = None if recurring else sy

        if hid:
            hid_int = form_int(hid)
            obj = await db.get(CompanyHoliday, hid_int) if hid_int else None
            if obj is None:
                continue
            obj.name = name
            obj.start_month, obj.start_day = sm, sd
            obj.end_month, obj.end_day = em, ed
            obj.year = year
            obj.enabled = enabled
        else:
            db.add(CompanyHoliday(
                name=name, start_month=sm, start_day=sd,
                end_month=em, end_day=ed, year=year, enabled=enabled,
            ))
    await db.commit()
    if errors:
        return _back(" / ".join(errors[:3]), False)
    return _back("指定休日を保存しました。", True)


@router.post("/company/{h_id}/delete")
async def delete_company_holiday(
    h_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(CompanyHoliday, h_id)
    if obj is not None:
        await db.delete(obj)
        await db.commit()
    return _back("指定休日を削除しました。", True)


# ---------------------------------------------------------------------------
# カレンダー表示 (年間カレンダーで平日/休日/祝日/指定休日を確認・設定)
# ---------------------------------------------------------------------------


@router.get("/calendar")
async def calendar_data(
    year: int | None = None,
    tc: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """指定年の各日の区分を返す (カレンダー描画用)。

    tc に時間条件名を渡すと、その時間条件の曜日設定を使って
    平日/休日を判定する。省略時は土日を休みとみなす。

    返す区分:
      weekday  : 平日 (営業日)
      weekend  : 曜日設定で休み
      holiday  : 国民の祝日
      company  : 会社指定休日
    """
    from calendar import monthrange
    from datetime import date as _date

    from app.models import TimeCondition

    year = year or datetime.now().year

    # 祝日 (日付 → 名称)
    nh_rows = (
        await db.scalars(
            select(NationalHoliday).where(NationalHoliday.year == year)
        )
    ).all()
    holiday_map = {h.holiday_date.isoformat(): h.name for h in nh_rows}

    # 指定休日 (期間を日付に展開)
    ch_rows = (
        await db.scalars(select(CompanyHoliday).where(CompanyHoliday.enabled.is_(True)))
    ).all()
    company_map: dict[str, str] = {}
    company_pattern: dict[str, str] = {}   # 日付 → 適用パターンの表示名
    for ch in ch_rows:
        pat_label = ch.pattern.display_name if ch.pattern else ""
        # 年をまたぐ期間 (12/29〜1/3 等) は、前年から続く分も表示年に
        # 反映する必要がある。そのため「表示年に始まる期間」だけでなく
        # 「前年に始まって表示年に食い込む期間」も展開する。
        # (毎年繰り返しの場合は前年ぶんも同じ月日で発生する)
        start_years = []
        if ch.year:
            # 年指定ありは、その年に始まる期間のみ
            if ch.year in (year, year - 1):
                start_years.append(ch.year)
        else:
            start_years = [year - 1, year]

        for sy in start_years:
            try:
                start = _date(sy, ch.start_month, ch.start_day)
            except ValueError:
                continue
            # 終了月が開始月より小さければ翌年へまたぐ
            end_year = sy if ch.end_month >= ch.start_month else sy + 1
            try:
                end = _date(end_year, ch.end_month, ch.end_day)
            except ValueError:
                continue
            cur = start
            while cur <= end:
                if cur.year == year:
                    company_map[cur.isoformat()] = ch.name
                    company_pattern[cur.isoformat()] = pat_label
                cur = _date.fromordinal(cur.toordinal() + 1)

    # カレンダー例外 (CalendarException) も同じ色でカレンダーに出す。
    #
    # 経緯: 「指定休日」(CompanyHoliday) と「カレンダー例外」
    # (CalendarException) は、どちらもダイヤルプラン生成側で休業日として
    # 扱われるのに、カレンダーは前者しか描いていなかった。そのため
    # 「カレンダー例外」で登録した休みはこの画面のどこにも現れないのに
    # 電話の挙動だけが変わる、という一番分かりにくい状態になっていた。
    # 両方を表示して、画面と実際の動作を一致させる。
    ce_rows = (
        await db.scalars(
            select(CalendarException).where(CalendarException.enabled.is_(True))
        )
    ).all()
    for ce in ce_rows:
        pat_label = ce.pattern.display_name if ce.pattern else ""
        if ce.year:
            start_years = [ce.year] if ce.year in (year, year - 1) else []
        else:
            start_years = [year - 1, year]
        for sy in start_years:
            try:
                start = _date(sy, ce.start_month, ce.start_day)
                end_year = sy if ce.end_month >= ce.start_month else sy + 1
                end = _date(end_year, ce.end_month, ce.end_day)
            except ValueError:
                continue
            cur = start
            while cur <= end:
                if cur.year == year:
                    key = cur.isoformat()
                    company_map.setdefault(key, ce.name)
                    company_pattern.setdefault(key, pat_label)
                cur = _date.fromordinal(cur.toordinal() + 1)

    # 曜日ごとの営業/休みを時間条件から取得 (無ければ土日休み)
    weekday_off = {5, 6}  # 月=0 … 土=5, 日=6
    tc_name = None
    if tc:
        tc_row = (
            await db.scalars(select(TimeCondition).where(TimeCondition.name == tc))
        ).first()
        if tc_row is not None:
            tc_name = tc_row.name
            fields = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            weekday_off = {
                i for i, f in enumerate(fields)
                if getattr(tc_row, f"{f}_pattern_id") is None
            }

    days = []
    for month in range(1, 13):
        for day in range(1, monthrange(year, month)[1] + 1):
            d = _date(year, month, day)
            iso = d.isoformat()
            if iso in company_map:
                kind, label = "company", company_map[iso]
            elif iso in holiday_map:
                kind, label = "holiday", holiday_map[iso]
            elif d.weekday() in weekday_off:
                kind, label = "weekend", ""
            else:
                kind, label = "weekday", ""
            days.append({
                "date": iso, "kind": kind, "label": label,
                "pattern": company_pattern.get(iso, ""),
            })

    # 割り当て可能な日次パターン一覧も返す (カレンダー上での変更用)
    from app.models import DayPattern
    patterns = (
        await db.scalars(
            select(DayPattern).where(DayPattern.enabled.is_(True))
            .order_by(DayPattern.name)
        )
    ).all()
    return {
        "year": year, "days": days, "time_condition": tc_name,
        "patterns": [
            {"id": p.id, "name": p.display_name, "ranges": p.on_ranges or ""}
            for p in patterns
        ],
    }


@router.post("/calendar/set-pattern")
async def calendar_set_pattern(
    request: Request,
    date_str: str = Form(...),
    pattern_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """カレンダー上で選んだ日に、日次パターンを割り当てる。

    その日を単日の指定休日として登録し、指定されたパターンを適用する。
    pattern_id が空なら「終日休業」として登録する。
    既にその日の単日指定休日があれば、パターンだけを差し替える。
    """
    from datetime import date as _date

    try:
        y, m, d = (int(x) for x in date_str.split("-"))
        target = _date(y, m, d)
    except ValueError:
        return {"ok": False, "message": "日付の形式が正しくありません。"}

    pid = int(pattern_id) if pattern_id.strip().isdigit() else None

    rows = (
        await db.scalars(
            select(CompanyHoliday).where(
                CompanyHoliday.start_month == m,
                CompanyHoliday.start_day == d,
                CompanyHoliday.end_month == m,
                CompanyHoliday.end_day == d,
            )
        )
    ).all()
    single = next((r for r in rows if r.year in (None, y)), None)

    if single is not None:
        single.pattern_id = pid
        single.enabled = True
        await db.commit()
        return {"ok": True, "action": "updated", "date": date_str}

    db.add(CompanyHoliday(
        name=f"{target.month}月{target.day}日",
        start_month=m, start_day=d, end_month=m, end_day=d,
        year=y, pattern_id=pid, enabled=True,
    ))
    await db.commit()
    return {"ok": True, "action": "added", "date": date_str}


@router.post("/calendar/toggle")
async def calendar_toggle(
    request: Request,
    date_str: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """カレンダー上でクリックした日を、指定休日に追加/解除する。

    その日を含む単日の指定休日があれば削除、無ければ追加する。
    期間で登録された指定休日 (年末年始等) はここでは操作しない
    (誤って期間全体を消さないようにするため)。
    """
    from datetime import date as _date

    try:
        y, m, d = (int(x) for x in date_str.split("-"))
        target = _date(y, m, d)
    except ValueError:
        return {"ok": False, "message": "日付の形式が正しくありません。"}

    # 同じ日を指す単日の指定休日を探す
    rows = (
        await db.scalars(
            select(CompanyHoliday).where(
                CompanyHoliday.start_month == m,
                CompanyHoliday.start_day == d,
                CompanyHoliday.end_month == m,
                CompanyHoliday.end_day == d,
            )
        )
    ).all()
    single = next((r for r in rows if r.year in (None, y)), None)

    if single is not None:
        await db.delete(single)
        await db.commit()
        return {"ok": True, "action": "removed", "date": date_str}

    db.add(CompanyHoliday(
        name=f"{target.month}月{target.day}日 休業",
        start_month=m, start_day=d, end_month=m, end_day=d,
        year=y, enabled=True,
    ))
    await db.commit()
    return {"ok": True, "action": "added", "date": date_str}
