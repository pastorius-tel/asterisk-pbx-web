"""配布版 (エディション) の判別。

本ツールは同じソースから 2 つの配布物を作る:

  normal     … 通常版。x86_64 / arm64 の Ubuntu Server (24.04 / 26.04) 向け。
               uvloop などの高速化オプションと SMB 外部保存を含めて入れる。
  rpi-armv7  … Raspberry Pi 2 (32bit ARM / armv7l) の Ubuntu Server 22.04 向け。
               32bit ARM ではビルド済み配布物の無いライブラリが多いため、
               コンパイル無しで入る構成 (greenlet だけはコンパイルが必要) に
               絞り、Python は OS 標準の 3.10 でも動くようにしてある。

アプリのコード自体は両方で同じで、違うのは「どのライブラリを、どの版で
入れるか」(requirements/*.lock) とインストーラの既定値だけ。
不具合の修正は両方に同時に入る。

どちらの版かは、配布 zip に同梱される EDITION ファイルで決まる。
git clone した場合など EDITION が無いときは CPU の種類から推定する。
ただし 32bit ARM の機械では EDITION が normal でも Pi 版として扱う。
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Edition:
    key: str
    label: str
    lock: str
    """requirements/ 以下の固定版ファイル名。"""
    extras: str
    """pip install -e ".<extras>" に渡す extras (空なら "")。"""
    target: str
    """想定している OS / ハードウェア (画面表示用)。"""


EDITIONS: dict[str, Edition] = {
    "normal": Edition(
        key="normal",
        label="通常版",
        lock="normal.lock",
        extras="[fast,smb]",
        target="Ubuntu Server 24.04 / 26.04 (x86_64 / arm64)",
    ),
    "rpi-armv7": Edition(
        key="rpi-armv7",
        label="Raspberry Pi 版 (32bit ARM)",
        lock="rpi-armv7.lock",
        extras="",
        target="Raspberry Pi 2 Model B / Ubuntu Server 22.04 (armhf, 32bit)",
    ),
}

# 32bit ARM と判定する uname -m の値
_ARM32 = {"armv7l", "armv7", "armv6l", "armhf", "arm"}


def detect_key() -> str:
    """EDITION ファイル、無ければ CPU の種類からエディションを決める。

    32bit ARM の機械では EDITION が "normal" でも Raspberry Pi 版として扱う。
    通常版のライブラリ構成 (uvloop 等) は 32bit ARM に入らないため、
    インストーラ (install_all.sh / update_app.sh) も同じ判定で Pi 版の
    構成を入れている。GitHub から取得したソースに通常版の EDITION が
    入っていても、画面の表示と実際に入っている構成が食い違わないようにする。
    """
    is_arm32 = platform.machine().lower() in _ARM32
    try:
        key = (_ROOT / "EDITION").read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if key == "normal" and is_arm32:
        return "rpi-armv7"
    if key in EDITIONS:
        return key
    return "rpi-armv7" if is_arm32 else "normal"


def current() -> Edition:
    return EDITIONS[detect_key()]
