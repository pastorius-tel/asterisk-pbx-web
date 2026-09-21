"""音源 (AudioFile) 管理ルーター。

  GET  /audio/                  一覧
  GET  /audio/new               アップロードフォーム
  POST /audio/                  アップロード + ffmpeg 変換実行
  POST /audio/{id}/delete       削除 (DB + ファイル両方)
  GET  /audio/{id}/preview      プレビュー (変換後 WAV をストリーミング)
  POST /audio/{id}/reconvert    再変換
  GET  /audio/tts               文章から音声を作る (TTS) フォーム
  POST /audio/tts               TTS 実行 + 音源として登録
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AudioFile
from app.models.base import AudioCategory
from app.routers._helpers import all_enum_choices
from app.services import tts_service
from app.services.audio_convert import (
    convert_to_asterisk_wav,
    is_ffmpeg_available,
    remove_converted_file,
    remove_source_file,
    sanitize_storage_name,
    validate_upload_filename,
)

log = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["audio"])

# アップロード音源 1 件あたりの最大サイズ (バイト)
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


@router.get("/", response_class=HTMLResponse)
async def list_audio(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (await db.scalars(select(AudioFile).order_by(AudioFile.category, AudioFile.name))).all()
    return request.app.state.templates.TemplateResponse(
        request,
        "audio/list.html",
        {
            "items": rows,
            "title": "音源管理",
            "ffmpeg_ok": is_ffmpeg_available(),
            "sounds_dir": str(settings.asterisk_sounds_dir),
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def new_form(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "audio/form.html",
        {
            "title": "音源のアップロード",
            "ffmpeg_ok": is_ffmpeg_available(),
            **all_enum_choices(),
        },
    )


@router.post("/", response_class=HTMLResponse, response_model=None)
async def create_audio(
    request: Request,
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    category: str = Form(...),
    note: str | None = Form(None),
    file: UploadFile = File(...),
) -> HTMLResponse | RedirectResponse:
    # 拡張子チェック
    ok, msg = validate_upload_filename(file.filename or "")
    if not ok:
        return _render_form_error(request, msg, name, category, note)

    # カテゴリ妥当性
    try:
        cat = AudioCategory(category)
    except ValueError:
        return _render_form_error(request, "カテゴリが不正です。", name, category, note)

    # ストレージ名重複回避
    storage_name = sanitize_storage_name(file.filename or name)
    existing = (
        await db.scalars(select(AudioFile).where(AudioFile.storage_name == storage_name))
    ).first()
    if existing is not None:
        # 末尾に番号付加して回避
        i = 2
        while True:
            candidate = f"{storage_name}_{i}"
            ex2 = (
                await db.scalars(select(AudioFile).where(AudioFile.storage_name == candidate))
            ).first()
            if ex2 is None:
                storage_name = candidate
                break
            i += 1

    src_ext = Path(file.filename or "x.mp3").suffix.lower().lstrip(".")
    # アップロード元を uploads_dir に保存
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    src_path = settings.uploads_dir / f"{storage_name}.{src_ext}"
    try:
        content = await file.read()
        # アップロードは丸ごとメモリに読み込むため、上限を設けないと
        # 巨大なファイル 1 つでサーバーのメモリを食い潰せてしまう。
        # 案内音声は長くても数分なので 32MB あれば十分。
        if len(content) > MAX_UPLOAD_BYTES:
            return _render_form_error(
                request,
                f"ファイルが大きすぎます ({len(content) // (1024 * 1024)}MB)。"
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 以下にしてください。",
                name, category, note,
            )
        src_path.write_bytes(content)
    except OSError as e:
        return _render_form_error(request, f"ファイル保存失敗: {e}", name, category, note)

    # DB に pending で登録
    obj = AudioFile(
        name=name,
        category=cat.value,
        original_filename=file.filename or "",
        storage_name=storage_name,
        source_format=src_ext,
        bytes_size=len(content),
        conversion_status="pending",
        note=note or None,
    )
    db.add(obj)
    await db.flush()

    # ffmpeg で変換
    result = await convert_to_asterisk_wav(src_path, cat.value, storage_name)
    obj.conversion_status = "ok" if result.success else "failed"
    obj.conversion_log = result.log
    obj.duration_seconds = result.duration_seconds
    if result.output_path is not None:
        obj.bytes_size = result.bytes_size

    await db.commit()

    if not result.success:
        # 失敗時はエラーを画面に表示
        return request.app.state.templates.TemplateResponse(
            request,
            "audio/form.html",
            {
                "title": "変換失敗",
                "error_message": "ffmpeg による変換に失敗しました。",
                "error_detail": result.log,
                "submitted": {"name": name, "category": category, "note": note},
                "ffmpeg_ok": is_ffmpeg_available(),
                **all_enum_choices(),
            },
            status_code=400,
        )

    return RedirectResponse("/audio/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# TTS (文章から音声を作る)
# ---------------------------------------------------------------------------


@router.get("/tts", response_class=HTMLResponse)
async def tts_form(
    request: Request, edit: int | None = None,
    install_message: str | None = None, install_ok: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """文章から音声を作るフォーム。

    edit=<音源ID> を付けると、その音源の文章・声・話速を読み込んだ状態で
    開き、作り直せる (TTS で作った音源のみ)。
    """
    ok, reason = tts_service.tts_available()
    done, total, _missing = tts_service.datetime_sounds_status()
    submitted = None
    edit_obj = None
    if edit is not None:
        edit_obj = await db.get(AudioFile, edit)
        if edit_obj is not None and edit_obj.tts_text:
            submitted = {
                "name": edit_obj.name,
                "category": edit_obj.category,
                "text": edit_obj.tts_text,
                "speed": str(edit_obj.tts_speed or 1.0),
                "voice": edit_obj.tts_voice or "",
                "pitch": str(edit_obj.tts_pitch if edit_obj.tts_pitch is not None else 0.0),
                "tone": ("" if edit_obj.tts_tone is None else str(edit_obj.tts_tone)),
                "gain": str(edit_obj.tts_gain if edit_obj.tts_gain is not None else 0.0),
                "clarity": str(edit_obj.tts_clarity if edit_obj.tts_clarity is not None else 0.0),
                "note": edit_obj.note,
            }
        else:
            edit_obj = None
    return request.app.state.templates.TemplateResponse(
        request, "audio/tts.html",
        {
            "title": "文章から音声を作り直す" if edit_obj else "文章から音声を作る",
            "tts_ok": ok,
            "tts_reason": reason,
            "dt_done": done,
            "dt_total": total,
            "voices": [
                {**v, "custom": tts_service.is_custom_voice(v["key"])}
                for v in tts_service.list_voices()
            ],
            "voice_packs": [
                {
                    "key": k,
                    "installed": tts_service.voice_pack_installed(k),
                    **v,
                }
                for k, v in tts_service.VOICE_PACKS.items()
            ],
            "install_message": install_message,
            "install_ok": install_ok,
            "param_ranges": tts_service.PARAM_RANGES,
            "submitted": submitted,
            "edit_id": edit_obj.id if edit_obj else None,
            **all_enum_choices(),
        },
    )


@router.post("/tts", response_class=HTMLResponse, response_model=None)
async def tts_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    category: str = Form(...),
    text: str = Form(...),
    speed: str = Form("1.0"),
    voice: str = Form(""),
    pitch: str = Form("0.0"),
    tone: str = Form(""),
    gain: str = Form("0.0"),
    clarity: str = Form("0.0"),
    note: str | None = Form(None),
    edit_id: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """入力された日本語テキストから音声を合成し、音源として登録する。

    edit_id が指定されている場合は、その音源を作り直す (上書き更新)。
    """
    ok, reason = tts_service.tts_available()
    if not ok:
        return _render_tts_error(request, f"音声合成を利用できません: {reason}",
                                 name, category, text, note, speed, voice, edit_id)
    try:
        cat = AudioCategory(category)
    except ValueError:
        return _render_tts_error(request, "カテゴリが不正です。",
                                 name, category, text, note, speed, voice, edit_id)

    if not text.strip():
        return _render_tts_error(request, "読み上げる文章を入力してください。",
                                 name, category, text, note, speed, voice, edit_id)
    try:
        speed_f = float(speed)
        if not (0.5 <= speed_f <= 2.0):
            raise ValueError
    except ValueError:
        return _render_tts_error(request, "話速は 0.5〜2.0 の範囲で指定してください。",
                                 name, category, text, note, speed, voice, edit_id)

    def _num(v: str, default: float | None) -> float | None:
        """フォームの数値入力を解析する。空欄なら default。"""
        v = (v or "").strip()
        if not v:
            return default
        try:
            return float(v)
        except ValueError:
            return default

    # 範囲外の値は tts_service 側で安全な範囲に丸められる
    pitch_f = _num(pitch, 0.0)
    tone_f = _num(tone, None)      # 空欄なら音響モデル既定を使う
    gain_f = _num(gain, 0.0)
    clarity_f = _num(clarity, 0.0)

    # 既存音源の作り直しか、新規作成かを判定
    obj: AudioFile | None = None
    if edit_id:
        try:
            obj = await db.get(AudioFile, int(edit_id))
        except ValueError:
            obj = None

    if obj is not None:
        # 作り直し: 元の保存先を使う (参照している IVR 等の設定を壊さない)
        storage_name = obj.storage_name
        # カテゴリを変更した場合は古い WAV を消す
        if obj.category != cat.value:
            remove_converted_file(obj.category, obj.storage_name)
    else:
        storage_name = sanitize_storage_name(name)
        existing = (
            await db.scalars(select(AudioFile).where(AudioFile.storage_name == storage_name))
        ).first()
        if existing is not None:
            i = 2
            while True:
                candidate = f"{storage_name}_{i}"
                ex2 = (
                    await db.scalars(
                        select(AudioFile).where(AudioFile.storage_name == candidate)
                    )
                ).first()
                if ex2 is None:
                    storage_name = candidate
                    break
                i += 1

    out_path = settings.asterisk_sounds_dir / cat.value / f"{storage_name}.wav"
    try:
        tts_service.synthesize_to_wav(
            text, out_path, speed=speed_f, voice=voice or None,
            pitch=pitch_f or 0.0, tone=tone_f,
            gain=gain_f or 0.0, clarity=clarity_f or 0.0,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("TTS 生成に失敗: %s", e)
        return _render_tts_error(request, f"音声の生成に失敗しました: {e}",
                                 name, category, text, note, speed, voice, edit_id)

    size = out_path.stat().st_size if out_path.exists() else 0
    voice_label = voice or "(既定)"
    if obj is None:
        obj = AudioFile(
            name=name,
            category=cat.value,
            original_filename="(音声合成)",
            storage_name=storage_name,
            source_format="tts",
            bytes_size=size,
            conversion_status="ok",
            note=note or None,
        )
        db.add(obj)
    else:
        obj.name = name
        obj.category = cat.value
        obj.bytes_size = size
        obj.conversion_status = "ok"
        obj.note = note or None

    obj.conversion_log = (
        f"Open JTalk で合成 (声={voice_label}, 話速={speed_f}, "
        f"ピッチ={pitch_f}, 声質={tone_f if tone_f is not None else '既定'}, "
        f"音量={gain_f}dB, 明瞭さ={clarity_f})"
    )
    obj.tts_text = text
    obj.tts_speed = speed_f
    obj.tts_voice = voice or None
    obj.tts_pitch = tts_service.clamp_param("pitch", pitch_f)
    obj.tts_tone = tts_service.clamp_param("tone", tone_f)
    obj.tts_gain = tts_service.clamp_param("gain", gain_f)
    obj.tts_clarity = tts_service.clamp_param("clarity", clarity_f)
    await db.commit()
    return RedirectResponse("/audio/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/tts/install-voice")
async def install_voice_pack(
    request: Request,
    pack: str = Form(...),
) -> RedirectResponse:
    """追加の声 (音響モデル) をダウンロードして導入する。

    Ubuntu 標準リポジトリには男性の声しか無いため、配布元から直接
    取得する。処理には時間がかかる (特にメイは約 100MB)。
    """
    ok, message = await asyncio.to_thread(tts_service.install_voice_pack, pack)
    return RedirectResponse(
        f"/audio/tts?install_message={quote(message)}&install_ok={'1' if ok else '0'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/tts/upload-voice")
async def upload_voice(
    request: Request,
    voice_file: UploadFile = File(...),
) -> RedirectResponse:
    """手持ちの .htsvoice ファイルをアップロードして音響モデルに追加する。

    配布元が限られるため画面から導入できないモデルでも、ファイルさえ
    あればこれで使えるようになる。
    """
    try:
        data = await voice_file.read()
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            f"/audio/tts?install_message={quote(f'読み込みに失敗しました: {e}')}&install_ok=0",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    ok, message = tts_service.save_uploaded_voice(voice_file.filename or "", data)
    return RedirectResponse(
        f"/audio/tts?install_message={quote(message)}&install_ok={'1' if ok else '0'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/tts/delete-voice")
async def delete_voice(
    request: Request,
    key: str = Form(...),
) -> RedirectResponse:
    """アップロードで追加した音響モデルを削除する。"""
    ok, message = tts_service.delete_voice(key)
    return RedirectResponse(
        f"/audio/tts?install_message={quote(message)}&install_ok={'1' if ok else '0'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/tts/datetime-sounds")
async def generate_datetime_sounds(
    request: Request,
    overwrite: str = Form(""),
    dt_voice: str = Form(""),
) -> RedirectResponse:
    """留守番電話の日時読み上げに必要な音声ファイルを一括生成する。

    Asterisk の日本語モードは digits/ji (時)・digits/fun (分) 等の
    専用ファイルを必要とするが、一般的な日本語音声パックには含まれて
    いないため、ここで合成して補う (詳細は tts_service のコメント参照)。

    時・分で使う数字 (digits/1 等) も同じ声で生成する。元の音声パックの
    数字をそのまま使うと、「〇月〇日」(合成音声) と「〇時〇分」(音声
    パック) で話者が変わってしまい聞き取りにくくなるため。
    """
    created, skipped, errors = await asyncio.to_thread(
        tts_service.generate_datetime_sounds,
        overwrite=(overwrite == "on"),
        voice=dt_voice or None,
    )
    if errors:
        msg = f"{created} 件生成しましたが、{len(errors)} 件失敗しました。"
        ok_flag = "0"
    elif created == 0 and skipped > 0:
        msg = (
            f"すべて生成済みのためスキップしました ({skipped} 件)。"
            "声を変えて作り直すには「既存のファイルも作り直す」を有効にしてください。"
        )
        ok_flag = "1"
    else:
        msg = f"日時読み上げ用の音声を {created} 件生成しました。"
        ok_flag = "1"
    return RedirectResponse(
        f"/audio/tts?install_message={quote(msg)}&install_ok={ok_flag}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _render_tts_error(request, msg: str, name, category, text, note,
                      speed="1.0", voice="", edit_id=""):
    ok, reason = tts_service.tts_available()
    done, total, _missing = tts_service.datetime_sounds_status()
    return request.app.state.templates.TemplateResponse(
        request, "audio/tts.html",
        {
            "title": "音声合成エラー",
            "error_message": msg,
            "submitted": {
                "name": name, "category": category, "text": text,
                "note": note, "speed": speed, "voice": voice,
            },
            "param_ranges": tts_service.PARAM_RANGES,
            "tts_ok": ok,
            "tts_reason": reason,
            "dt_done": done,
            "dt_total": total,
            "voices": [
                {**v, "custom": tts_service.is_custom_voice(v["key"])}
                for v in tts_service.list_voices()
            ],
            "edit_id": int(edit_id) if str(edit_id).isdigit() else None,
            **all_enum_choices(),
        },
        status_code=400,
    )


@router.post("/{audio_id}/reconvert")
async def reconvert(
    audio_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(AudioFile, audio_id)
    if obj is None:
        raise HTTPException(404)
    if obj.source_format == "tts":
        # 音声合成で作った音源は元ファイル (uploads/*.mp3 等) が存在しない
        # ため再変換できない。文章を編集して作り直す画面へ誘導する。
        return RedirectResponse(
            f"/audio/tts?edit={obj.id}", status_code=status.HTTP_303_SEE_OTHER
        )
    src_path = settings.uploads_dir / f"{obj.storage_name}.{obj.source_format}"
    result = await convert_to_asterisk_wav(src_path, obj.category, obj.storage_name)
    obj.conversion_status = "ok" if result.success else "failed"
    obj.conversion_log = result.log
    obj.duration_seconds = result.duration_seconds
    if result.bytes_size:
        obj.bytes_size = result.bytes_size
    await db.commit()
    return RedirectResponse("/audio/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{audio_id}/delete")
async def delete_audio(
    audio_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(AudioFile, audio_id)
    if obj is None:
        raise HTTPException(404)
    remove_converted_file(obj.category, obj.storage_name)
    remove_source_file(obj.storage_name, obj.source_format)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/audio/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{audio_id}/preview")
async def preview(audio_id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    """変換後 WAV をブラウザで再生できるよう返す。"""
    obj = await db.get(AudioFile, audio_id)
    if obj is None:
        raise HTTPException(404)
    wav_path = settings.asterisk_sounds_dir / obj.category / f"{obj.storage_name}.wav"
    if not wav_path.exists():
        raise HTTPException(404, detail="変換後ファイルが見つかりません。")
    return FileResponse(wav_path, media_type="audio/wav", filename=f"{obj.name}.wav")


def _render_form_error(request, msg: str, name, category, note):
    return request.app.state.templates.TemplateResponse(
        request,
        "audio/form.html",
        {
            "title": "アップロードエラー",
            "error_message": msg,
            "submitted": {"name": name, "category": category, "note": note},
            "ffmpeg_ok": is_ffmpeg_available(),
            **all_enum_choices(),
        },
        status_code=400,
    )
