"""日本語 TTS (音声合成) サービス。

Open JTalk を使って日本語テキストから音声ファイルを生成し、Asterisk が
再生できる形式 (8kHz mono 16bit PCM WAV) に変換する。

用途は 2 つ:
  1. 留守番電話・IVR 等の案内メッセージを、音源ファイルを用意せずに
     文章から直接作る
  2. Asterisk 日本語モードの日時読み上げに必要な音声ファイル
     (digits/ji, digits/fun 等) を一括生成する
     (一般的な日本語音声パックには含まれていないため。詳細は
      generate_datetime_sounds() のコメント参照)
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import settings
from app.services.permissions import describe_permission_error as _perm_hint

logger = logging.getLogger(__name__)

OPEN_JTALK_BIN = "open_jtalk"

# Open JTalk の辞書・音響モデルの探索候補 (ディストリビューションにより異なる)
_DIC_CANDIDATES = [
    "/var/lib/mecab/dic/open-jtalk/naist-jdic",
    "/usr/share/mecab/dic/open-jtalk/naist-jdic",
    "/usr/lib/x86_64-linux-gnu/open-jtalk/dic",
]
_VOICE_CANDIDATES = [
    "/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice",
    "/usr/share/hts-voice/mei/mei_normal.htsvoice",
]

def _exists(path: Path) -> bool:
    """存在するかを調べる。調べられない場合は False。

    親ディレクトリに検索 (x) 権限が無いと Path.exists() は
    PermissionError を投げる。画面表示の途中でこれが起きると
    ページ全体が開かなくなるため、権限を判定する箇所以外では
    常にこの関数を通す。
    """
    try:
        return path.exists()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 音響モデル (.htsvoice) の置き場所
#
# /usr/share/hts-voice は apt で入る標準の声の置き場だが root 所有のため、
# サービス実行ユーザー (既定 asterisk) で動く本ツールからは書き込めない。
# 以前はここだけを使っていたので、画面からの声パック導入と .htsvoice
# アップロードは必ず PermissionError で失敗していた。
#
# そこで 2 箇所を併用する:
#   HTS_VOICE_SYSTEM_DIR … apt 等が入れた標準の声 (読み取り専用で参照)
#   hts_voice_dir (設定)  … 本ツールが追加する声 (読み書き)
# 一覧表示は両方を走査し、追加・削除は書き込み可能な側だけを対象にする。
# ---------------------------------------------------------------------------
HTS_VOICE_SYSTEM_DIR = Path("/usr/share/hts-voice")

# 後方互換: 以前のコード・ドキュメントが参照していた名前。
HTS_VOICE_DIR = HTS_VOICE_SYSTEM_DIR


def hts_voice_write_dir() -> Path:
    """本ツールが音響モデルを追加する (書き込み可能な) ディレクトリ。"""
    return settings.hts_voice_dir


def _hts_voice_dirs() -> list[Path]:
    """音響モデルを探索するディレクトリ (重複を除いた順序付きリスト)。"""
    dirs: list[Path] = []
    for d in (HTS_VOICE_SYSTEM_DIR, hts_voice_write_dir()):
        if d not in dirs:
            dirs.append(d)
    return dirs

# 音響モデル (声) の表示名。ファイル名からの推測に使う。
# ここに無いモデルも、.htsvoice ファイルさえあれば自動的に検出される。
_VOICE_LABELS = {
    "nitech_jp_atr503_m001": "標準 (男性)",
    "mei_normal": "メイ (女性・通常)",
    "mei_happy": "メイ (女性・明るめ)",
    "mei_sad": "メイ (女性・落ち着き)",
    "mei_angry": "メイ (女性・強め)",
    "mei_bashful": "メイ (女性・控えめ)",
    "takumi_normal": "タクミ (男性・通常)",
    "takumi_happy": "タクミ (男性・明るめ)",
    "takumi_sad": "タクミ (男性・落ち着き)",
    "takumi_angry": "タクミ (男性・強め)",
    "tohoku-f01-neutral": "東北 f01 (女性・通常)",
    "tohoku-f01-happy": "東北 f01 (女性・明るめ)",
    "tohoku-f01-sad": "東北 f01 (女性・落ち着き)",
    "tohoku-f01-angry": "東北 f01 (女性・強め)",
}


def list_voices() -> list[dict[str, str]]:
    """インストール済みの音響モデル (.htsvoice) を一覧する。

    標準の置き場 (/usr/share/hts-voice) と本ツールの置き場の両方を
    再帰的に探し、見つかったモデルを
    {"value": <絶対パス>, "label": <表示名>, "key": <ファイル名(拡張子なし)>}
    の形で返す。パッケージ追加や手動配置で増えたモデルも自動的に現れる。

    同じキーが両方にある場合は、後から走査する本ツール側 (利用者が
    自分で入れたもの) を優先する。
    """
    found: dict[str, dict[str, str]] = {}
    for base in _hts_voice_dirs():
        if not _exists(base):
            continue
        try:
            paths = sorted(base.rglob("*.htsvoice"))
        except OSError:
            continue
        for p in paths:
            key = p.stem
            found[key] = {
                "value": str(p),
                "label": _VOICE_LABELS.get(key, key),
                "key": key,
            }
    return [found[k] for k in sorted(found)]


def resolve_voice(voice: str | None) -> str | None:
    """指定された音響モデル (絶対パスまたはキー名) を実在するパスに解決する。
    指定が無い/見つからない場合は既定のモデルにフォールバックする。"""
    if voice:
        p = Path(voice)
        if _exists(p) and p.suffix == ".htsvoice":
            return str(p)
        # キー名で指定された場合
        for v in list_voices():
            if v["key"] == voice:
                return v["value"]
    return _default_voice()


def _default_voice() -> str | None:
    """既定の音響モデル。標準の声が無ければ、導入済みの何かを使う。

    標準の声 (apt の nitech / mei) が入っていなくても、画面から追加した
    声だけで TTS が使えるようにするため、最後に list_voices() へ回す。
    """
    found = _find_first_existing(_VOICE_CANDIDATES)
    if found:
        return found
    voices = list_voices()
    return voices[0]["value"] if voices else None


def _find_first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if _exists(Path(p)):
            return p
    return None


def tts_available() -> tuple[bool, str]:
    """TTS が利用可能かと、利用不可の場合の理由を返す。"""
    if shutil.which(OPEN_JTALK_BIN) is None:
        return (False, "open_jtalk コマンドが見つかりません")
    if _find_first_existing(_DIC_CANDIDATES) is None:
        return (False, "Open JTalk の辞書 (naist-jdic) が見つかりません")
    if _default_voice() is None:
        return (False, "Open JTalk の音響モデル (.htsvoice) が見つかりません")
    if shutil.which(settings.ffmpeg_path) is None:
        return (False, f"{settings.ffmpeg_path} が見つかりません")
    return (True, "")


# 各調整パラメータの安全な範囲。open_jtalk 自体はもっと広い値を
# 受け付けるが、極端な値は聞き取れない音声や音割れを招くため制限する。
# (実測: -g 8 以上でクリッピングが発生した)
PARAM_RANGES = {
    "speed": (0.5, 2.0, 1.0),    # 話速
    "pitch": (-6.0, 6.0, 0.0),   # ピッチ (半音単位)
    "tone": (0.0, 1.0, None),    # 声質 (オールパス係数。None は音響モデル既定)
    "gain": (-6.0, 6.0, 0.0),    # 音量 (dB)
    # 明瞭さ: 0.5 以上で音割れ (クリッピング) が発生したため 0.4 までに制限
    "clarity": (0.0, 0.4, 0.0),
}


def clamp_param(name: str, value: float | None) -> float | None:
    """調整パラメータを安全な範囲に収める。None ならそのまま (既定値扱い)。"""
    if value is None:
        return None
    lo, hi, _default = PARAM_RANGES[name]
    return max(lo, min(hi, value))


def synthesize_to_wav(
    text: str, out_path: Path, speed: float = 1.0, pitch: float = 0.0,
    voice: str | None = None, tone: float | None = None,
    gain: float = 0.0, clarity: float = 0.0,
) -> None:
    """日本語テキストから Asterisk 用 WAV (8kHz mono 16bit) を生成する。

    speed:   話速 (1.0 が標準。小さいほどゆっくり)      → open_jtalk -r
    pitch:   声の高さ (半音単位。+ で高く)               → open_jtalk -fm
    tone:    声質 (オールパス係数 0〜1)。None で既定     → open_jtalk -a
    gain:    音量 (dB。+ で大きく)                       → open_jtalk -g
    clarity: 明瞭さ (ポストフィルタ係数 0〜1)            → open_jtalk -b
    voice:   音響モデル (.htsvoice の絶対パスかキー名)。未指定なら既定の声。

    生成に失敗した場合は RuntimeError を送出する。
    """
    ok, reason = tts_available()
    if not ok:
        raise RuntimeError(f"TTS を利用できません: {reason}")

    dic = _find_first_existing(_DIC_CANDIDATES)
    voice_path = resolve_voice(voice)
    if voice_path is None:
        raise RuntimeError("利用できる音響モデル (.htsvoice) が見つかりません")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        txt_file = tmp / "input.txt"
        raw_wav = tmp / "raw.wav"
        # Open JTalk は UTF-8 のテキストファイルを入力に取る
        txt_file.write_text(text, encoding="utf-8")

        cmd = [
            OPEN_JTALK_BIN,
            "-x", str(dic),
            "-m", str(voice_path),
            "-r", str(clamp_param("speed", speed)),
            "-fm", str(clamp_param("pitch", pitch)),
            "-g", str(clamp_param("gain", gain)),
            "-b", str(clamp_param("clarity", clarity)),
        ]
        # -a (声質) は未指定だと音響モデル既定の値が使われる。
        # 明示指定した場合だけ渡す。
        tone_v = clamp_param("tone", tone)
        if tone_v is not None:
            cmd += ["-a", str(tone_v)]
        cmd += ["-ow", str(raw_wav), str(txt_file)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not raw_wav.exists():
            raise RuntimeError(
                f"音声合成に失敗しました: {proc.stderr.strip() or proc.stdout.strip()}"
            )

        # Asterisk が扱える 8kHz mono 16bit PCM に変換
        conv = subprocess.run(
            [
                settings.ffmpeg_path, "-y", "-i", str(raw_wav),
                "-ar", "8000", "-ac", "1", "-acodec", "pcm_s16le",
                "-f", "wav", str(out_path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        if conv.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"WAV 変換に失敗しました: {conv.stderr.strip()}")


# ---------------------------------------------------------------------------
# Asterisk 日本語日時読み上げ用の音声ファイル一括生成
# ---------------------------------------------------------------------------

# Asterisk の say.c (ast_say_date_with_format_ja) が参照するファイル名と
# 読み上げテキストの対応。一般的な日本語音声パック
# (takao-tlasterisk-sound-ja 等) にはこれらが含まれていないため、
# 日時アナウンス (voicemail.conf の envelope=yes) を使うには自前で
# 用意する必要がある。
_MONTH_NAMES = [
    "いちがつ", "にがつ", "さんがつ", "しがつ", "ごがつ", "ろくがつ",
    "しちがつ", "はちがつ", "くがつ", "じゅうがつ", "じゅういちがつ", "じゅうにがつ",
]
_DAY_OF_WEEK_NAMES = [
    "にちようび", "げつようび", "かようび", "すいようび",
    "もくようび", "きんようび", "どようび",
]
# 1〜20日 + 30日の「〜にち」の読み (h-<n>_2)。
# 日本語の日付は読みが不規則なため個別に定義する。
_DAY_OF_MONTH_READINGS = {
    1: "ついたち", 2: "ふつか", 3: "みっか", 4: "よっか", 5: "いつか",
    6: "むいか", 7: "なのか", 8: "ようか", 9: "ここのか", 10: "とおか",
    11: "じゅういちにち", 12: "じゅうににち", 13: "じゅうさんにち",
    14: "じゅうよっか", 15: "じゅうごにち", 16: "じゅうろくにち",
    17: "じゅうしちにち", 18: "じゅうはちにち", 19: "じゅうくにち",
    20: "はつか", 30: "さんじゅうにち",
}


# 日時読み上げで Asterisk が参照する数字ファイル (digits/<n>)。
# 時 = 1〜20 と 20 (21時以降は「20 + 残り」で読む)
# 分 = 0〜20 と 30/40/50 (21分以降は「十の位 + 一の位」で読む)
# これらは元の日本語音声パックにも入っているが、話者が異なると
# 「〇月〇日」(合成音声) と「〇時〇分」(音声パック) で声が混ざって
# 聞き取りにくくなるため、同じ声で作り直せるようにしている。
_DATETIME_NUMBER_READINGS = {
    0: "ゼロ", 1: "いち", 2: "に", 3: "さん", 4: "よん", 5: "ご",
    6: "ろく", 7: "なな", 8: "はち", 9: "きゅう", 10: "じゅう",
    11: "じゅういち", 12: "じゅうに", 13: "じゅうさん", 14: "じゅうよん",
    15: "じゅうご", 16: "じゅうろく", 17: "じゅうなな", 18: "じゅうはち",
    19: "じゅうきゅう", 20: "にじゅう", 30: "さんじゅう",
    40: "よんじゅう", 50: "ごじゅう",
}


def datetime_sound_specs(include_numbers: bool = True) -> dict[str, str]:
    """日時読み上げに必要な「ファイル名 (拡張子なし) → 読み上げテキスト」
    の対応表を返す。digits/ からの相対パスで表す。

    include_numbers=True のとき、時・分で使う数字 (digits/1 等) も含める。
    元の音声パックの数字と話者が違うと声が混ざって聞こえるため、既定で
    含めている。
    """
    specs: dict[str, str] = {
        "ji": "じ",
        "fun": "ふん",
        "byou": "びょう",
        "nen": "ねん",
        "nichi": "にち",
        "oh": "まる",
        "thousand": "せん",
        "a-m": "ごぜん",
        "p-m": "ごご",
        "today": "きょう",
        "yesterday": "きのう",
        "9_2": "ここのか",
    }
    for i, name in enumerate(_MONTH_NAMES):
        specs[f"mon-{i}"] = name
    for i, name in enumerate(_DAY_OF_WEEK_NAMES):
        specs[f"day-{i}"] = name
    for day, reading in _DAY_OF_MONTH_READINGS.items():
        specs[f"h-{day}_2"] = reading
    if include_numbers:
        for n, reading in _DATETIME_NUMBER_READINGS.items():
            specs[str(n)] = reading
    return specs


def datetime_sounds_status() -> tuple[int, int, list[str]]:
    """日時読み上げ用音声の生成状況を返す。

    (生成済み件数, 必要な総数, 未生成のファイル名リスト)
    """
    specs = datetime_sound_specs(include_numbers=True)
    digits_dir = _japanese_digits_dir()
    missing = []
    for name in specs:
        # digits/ に検索権限が無いと exists() 自体が PermissionError を
        # 投げる。この関数はシステム画面・TTS 画面の表示で必ず呼ばれる
        # ため、ここで落ちると画面そのものが開かなくなる。
        try:
            found = (digits_dir / f"{name}.wav").exists()
        except OSError:
            found = False
        if not found:
            missing.append(name)
    return (len(specs) - len(missing), len(specs), missing)


def _japanese_digits_dir() -> Path:
    """日本語音声の digits/ ディレクトリ。

    Asterisk は <sounds_dir>/<language>/digits/ を探すため、
    音源アップロード先 (managed/) ではなく言語ディレクトリ直下に置く。
    """
    # asterisk_sounds_dir は .../sounds/ja/managed を指しているので、
    # その親 (= .../sounds/ja) の下の digits/ を使う。
    return settings.asterisk_sounds_dir.parent / "digits"


def generate_datetime_sounds(
    overwrite: bool = False, speed: float = 1.0,
    voice: str | None = None, include_numbers: bool = True,
) -> tuple[int, int, list[str]]:
    """日時読み上げに必要な音声ファイルを一括生成する。

    overwrite=False なら既存ファイルはスキップする。
    voice を指定すると、その音響モデル (声) で生成する。声を統一しないと
    「〇月〇日」と「〇時〇分」で話者が変わって聞き取りにくくなるため、
    作り直すときは overwrite=True で全て同じ声にするとよい。
    include_numbers=True なら、時・分で使う数字も同じ声で生成する。

    戻り値: (生成した件数, スキップした件数, エラーメッセージのリスト)
    """
    specs = datetime_sound_specs(include_numbers=include_numbers)
    digits_dir = _japanese_digits_dir()
    try:
        digits_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return (0, 0, [_perm_hint(digits_dir, e)])

    # 書き込めない場合は、1 件ずつ失敗を積むのではなく最初にまとめて
    # 知らせる。target.exists() 自体も (検索権限が無いと) 例外を投げる
    # ため、ここで止めないと 500 になって原因が分からなかった。
    if not os.access(digits_dir, os.W_OK | os.X_OK):
        return (
            0, 0,
            [_perm_hint(
                digits_dir,
                PermissionError(errno.EACCES, "Permission denied", str(digits_dir)),
            )],
        )

    created = 0
    skipped = 0
    errors: list[str] = []
    for name, text in specs.items():
        target = digits_dir / f"{name}.wav"
        try:
            already = target.exists()
        except OSError:
            already = False
        if already and not overwrite:
            skipped += 1
            continue
        try:
            synthesize_to_wav(text, target, speed=speed, voice=voice)
            created += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("日時音声の生成に失敗 %s: %s", name, e)
            errors.append(f"{name}: {e}")
    return (created, skipped, errors)


# ---------------------------------------------------------------------------
# 追加の声 (音響モデル) のインストール
# ---------------------------------------------------------------------------

# Web 画面から導入できる声パックの定義。
# Ubuntu の標準リポジトリには男性の声しか無いため、配布元から直接
# ダウンロードして hts_voice_dir へ配置する (本ツールは同梱しない)。
#
# 【帰属表示について】
# どちらも Creative Commons 表示 (CC BY) 系のライセンスで、利用には
# 著作権者・ライセンス名・ライセンスへのリンクの表示が必要になる。
# 以前は "icn-lab / htsvoice-tohoku-f01" のような短い記載しか出して
# いなかったが、それでは CC BY の要件を満たさないため、権利者名・
# ライセンス名・リンクを揃えて画面と THIRD_PARTY_NOTICES.md に出す。
VOICE_PACKS: dict[str, dict[str, str]] = {
    "tohoku-f01": {
        "label": "東北 f01 (女性)",
        "description": "通常・明るめ・落ち着き・強めの4種類。軽量ですぐ導入できます。",
        "size_hint": "約 3MB",
        "credit": "東北大学 Intelligent Communication Network (伊藤・能勢) 研究室",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/deed.ja",
        "source_url": "https://github.com/icn-lab/htsvoice-tohoku-f01",
    },
    "mei": {
        "label": "メイ (女性)",
        "description": "通常・明るめ・落ち着き・強め・控えめの5種類。名古屋工業大学 MMDAgent の音声です。",
        "size_hint": "約 100MB (ダウンロードに数分かかります)",
        "credit": "名古屋工業大学 MMDAgent プロジェクト",
        "license": "CC BY (音響モデル)",
        "license_url": "https://creativecommons.org/licenses/by/3.0/deed.ja",
        "source_url": "https://www.mmdagent.jp/",
    },
}

_TOHOKU_BASE = "https://raw.githubusercontent.com/icn-lab/htsvoice-tohoku-f01/master"
_TOHOKU_VARIANTS = ["neutral", "happy", "sad", "angry"]
_MMDAGENT_URL = (
    "https://sourceforge.net/projects/mmdagent/files/MMDAgent_Example/"
    "MMDAgent_Example-1.8/MMDAgent_Example-1.8.zip/download"
)


_PACK_MARKERS: dict[str, str] = {
    "tohoku-f01": "tohoku-f01/tohoku-f01-neutral.htsvoice",
    "mei": "mei/mei_normal.htsvoice",
}


def voice_pack_installed(pack: str) -> bool:
    """指定した声パックが既に導入済みかを返す。

    標準の置き場と本ツールの置き場の両方を見る (apt で入れた場合と
    画面から入れた場合のどちらでも「導入済み」と判定する)。
    """
    marker = _PACK_MARKERS.get(pack)
    if marker is None:
        return False
    return any(_exists(base / marker) for base in _hts_voice_dirs())


def install_voice_pack(pack: str) -> tuple[bool, str]:
    """声パックをダウンロードして導入する。

    戻り値: (成功したか, メッセージ)
    ネットワークからのダウンロードを伴うため時間がかかる。
    """
    import urllib.request
    import zipfile

    if pack not in VOICE_PACKS:
        return (False, f"不明な声パックです: {pack}")
    if voice_pack_installed(pack):
        return (True, f"{VOICE_PACKS[pack]['label']} は既に導入済みです。")

    root = hts_voice_write_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return (False, _perm_hint(root, e))

    if pack == "tohoku-f01":
        target_dir = root / "tohoku-f01"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return (False, _perm_hint(target_dir, e))
        got = 0
        for v in _TOHOKU_VARIANTS:
            url = f"{_TOHOKU_BASE}/tohoku-f01-{v}.htsvoice"
            dest = target_dir / f"tohoku-f01-{v}.htsvoice"
            try:
                with urllib.request.urlopen(url, timeout=120) as r:
                    dest.write_bytes(r.read())
                got += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("tohoku-f01-%s の取得に失敗: %s", v, e)
        if got == 0:
            return (False, "ダウンロードに失敗しました。サーバーがインターネットに接続できるか確認してください。")
        return (True, f"東北 f01 (女性) を導入しました ({got} 種類)。")

    # pack == "mei"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        zip_path = tmp / "mmdagent.zip"
        try:
            with urllib.request.urlopen(_MMDAGENT_URL, timeout=900) as r:
                zip_path.write_bytes(r.read())
        except Exception as e:  # noqa: BLE001
            return (False, f"ダウンロードに失敗しました: {e}")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                members = [
                    m for m in zf.namelist()
                    if "/Voice/mei/" in m and m.endswith(".htsvoice")
                ]
                if not members:
                    return (False, "アーカイブ内に音声ファイルが見つかりませんでした。")
                target_dir = root / "mei"
                target_dir.mkdir(parents=True, exist_ok=True)
                for m in members:
                    data = zf.read(m)
                    (target_dir / Path(m).name).write_bytes(data)
        except PermissionError as e:
            return (False, _perm_hint(root / "mei", e))
        except (zipfile.BadZipFile, OSError) as e:
            return (False, f"展開に失敗しました: {e}")
        return (True, f"メイ (女性) を導入しました ({len(members)} 種類)。")


CUSTOM_VOICE_DIR_NAME = "custom"


def save_uploaded_voice(filename: str, data: bytes) -> tuple[bool, str]:
    """アップロードされた .htsvoice ファイルを保存する。

    /usr/share/hts-voice/custom/ に置く。配布元が限られる音響モデルを
    利用者が独自に用意した場合に使う。
    戻り値: (成功したか, メッセージ)
    """
    name = Path(filename).name
    if not name.lower().endswith(".htsvoice"):
        return (False, "拡張子が .htsvoice のファイルを選んでください。")
    # ファイル名に使える文字を制限 (パス区切りや空白を排除)
    safe = "".join(c for c in name if c.isalnum() or c in "-_.")
    if not safe.lower().endswith(".htsvoice"):
        return (False, "ファイル名に使用できない文字が含まれています。")
    if not data:
        return (False, "ファイルが空です。")
    # htsvoice ファイルは先頭が "[GLOBAL]" で始まるテキストヘッダを持つ
    if not data.lstrip()[:8].startswith(b"[GLOBAL]"):
        return (False, "Open JTalk の音響モデル (.htsvoice) ではないようです。")

    target_dir = hts_voice_write_dir() / CUSTOM_VOICE_DIR_NAME
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / safe).write_bytes(data)
    except OSError as e:
        return (False, _perm_hint(target_dir, e))
    return (True, f"音響モデル「{Path(safe).stem}」を追加しました。")


def delete_voice(key: str) -> tuple[bool, str]:
    """アップロードで追加した音響モデルを削除する。

    安全のため custom/ 配下のもののみ削除できる (パッケージや配布元から
    導入した標準の声は消せない)。
    """
    target_dir = hts_voice_write_dir() / CUSTOM_VOICE_DIR_NAME
    if not _exists(target_dir):
        return (False, "削除できる音響モデルがありません。")
    for p in target_dir.glob("*.htsvoice"):
        if p.stem == key:
            try:
                p.unlink()
            except OSError as e:
                return (False, f"削除に失敗しました: {e}")
            return (True, f"音響モデル「{key}」を削除しました。")
    return (False, "指定された音響モデルが見つかりません (追加した声のみ削除できます)。")


def is_custom_voice(key: str) -> bool:
    """アップロードで追加した (＝削除可能な) 音響モデルかを返す。"""
    return _exists(hts_voice_write_dir() / CUSTOM_VOICE_DIR_NAME / f"{key}.htsvoice")
