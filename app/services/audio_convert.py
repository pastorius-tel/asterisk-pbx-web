"""音源変換サービス。

MP3 / M4A / OGG / WAV を Asterisk が直接再生できる形式に変換する。

Asterisk が安定して扱えるのは:
  - WAV (RIFF) PCM 16bit mono 8000Hz  → 拡張子 .wav
  - 16kHz HD voice 環境なら 16000Hz でも可

ここでは互換性最優先で 8kHz mono 16bit WAV を生成する。
ファイル名: <storage_name>.wav (拡張子なしで Asterisk からは指定する)

例: Playback(managed/moh/bgm_morning)
   → /var/lib/asterisk/sounds/ja/managed/moh/bgm_morning.wav
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"[^a-zA-Z0-9_\-]")
_ALLOWED_SOURCE_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}


def sanitize_storage_name(name: str) -> str:
    """ファイル名として安全な文字列に変換 (英数 _ -)。"""
    base = Path(name).stem
    cleaned = _NAME_RE.sub("_", base).strip("_")
    if not cleaned:
        cleaned = "audio"
    return cleaned[:60]


@dataclass
class ConversionResult:
    success: bool
    output_path: Path | None
    duration_seconds: float | None
    bytes_size: int | None
    log: str
    """ffmpeg stderr (失敗解析用)。"""


def is_ffmpeg_available() -> bool:
    return shutil.which(settings.ffmpeg_path) is not None


async def convert_to_asterisk_wav(
    source_path: Path,
    category: str,
    storage_name: str,
    sample_rate: int = 8000,
) -> ConversionResult:
    """元ファイル → Asterisk 用 WAV へ変換。

    出力先: <asterisk_sounds_dir>/<category>/<storage_name>.wav

    変換仕様 (固定):
      - PCM 16bit
      - mono
      - sample_rate (デフォルト 8000Hz, 16k 環境なら 16000)
      - WAV/RIFF
    """
    if not is_ffmpeg_available():
        return ConversionResult(
            success=False,
            output_path=None,
            duration_seconds=None,
            bytes_size=None,
            log=(
                f"ffmpeg が見つかりません (path='{settings.ffmpeg_path}')。\n"
                "apt: sudo apt install ffmpeg / brew install ffmpeg などでインストール後、"
                ".env の FFMPEG_PATH を確認してください。"
            ),
        )

    if not source_path.exists():
        return ConversionResult(
            success=False, output_path=None, duration_seconds=None,
            bytes_size=None, log=f"元ファイルが見つかりません: {source_path}",
        )

    out_dir = settings.asterisk_sounds_dir / category
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return ConversionResult(
            success=False, output_path=None, duration_seconds=None,
            bytes_size=None,
            log=(
                f"出力ディレクトリを作成できません: {out_dir}\n"
                f"  {e}\n"
                f"対処: ASTERISK_SOUNDS_DIR への書き込み権限を確認してください。"
            ),
        )

    out_path = out_dir / f"{storage_name}.wav"

    # ffmpeg コマンド組み立て
    #   -y: 上書き
    #   -i: 入力
    #   -ac 1: モノラル
    #   -ar <rate>: サンプリングレート
    #   -acodec pcm_s16le: 16bit PCM
    #   -f wav: WAV/RIFF
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-loglevel", "error",
        "-i", str(source_path),
        "-ac", "1",
        "-ar", str(sample_rate),
        "-acodec", "pcm_s16le",
        "-f", "wav",
        str(out_path),
    ]

    log.info("ffmpeg: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stderr_text = stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        return ConversionResult(
            success=False, output_path=None, duration_seconds=None,
            bytes_size=None,
            log=f"ffmpeg 失敗 (returncode={proc.returncode})\n{stderr_text}"[:1900],
        )

    # メタ情報取得
    duration = await _probe_duration(out_path)
    size = out_path.stat().st_size if out_path.exists() else None

    return ConversionResult(
        success=True,
        output_path=out_path,
        duration_seconds=duration,
        bytes_size=size,
        log=stderr_text[:1900],
    )


async def _probe_duration(path: Path) -> float | None:
    """ffprobe で長さを取得。失敗しても None を返すだけで例外は出さない。"""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return float(stdout.decode().strip())
    except (ValueError, OSError):
        return None


def asterisk_sound_reference(category: str, storage_name: str) -> str:
    """Playback/MoH から参照する音源パス (拡張子なし)。

    Asterisk は <sounds_dir>/<lang>/ 配下を自動探索するため、相対パスで指定。
    例: 'managed/moh/bgm_morning' → /var/lib/asterisk/sounds/ja/managed/moh/bgm_morning.wav
    """
    # asterisk_sounds_dir の最後のパス要素 (例: 'managed') 配下の <category>/<name>
    base = settings.asterisk_sounds_dir.name
    return f"{base}/{category}/{storage_name}"


def remove_converted_file(category: str, storage_name: str) -> bool:
    """変換後 WAV を削除。"""
    p = settings.asterisk_sounds_dir / category / f"{storage_name}.wav"
    try:
        if p.exists():
            p.unlink()
        return True
    except OSError:
        return False


def remove_source_file(storage_name: str, source_format: str) -> bool:
    """元アップロードファイルを削除。"""
    p = settings.uploads_dir / f"{storage_name}.{source_format}"
    try:
        if p.exists():
            p.unlink()
        return True
    except OSError:
        return False


def validate_upload_filename(filename: str) -> tuple[bool, str]:
    """アップロードファイル名のバリデーション。

    Returns: (ok, message)
    """
    if not filename:
        return False, "ファイル名が空です。"
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_SOURCE_EXTS:
        return False, (
            f"対応形式は {', '.join(sorted(_ALLOWED_SOURCE_EXTS))} です。"
            f" 受け取った拡張子: {ext or '(なし)'}"
        )
    return True, "ok"
