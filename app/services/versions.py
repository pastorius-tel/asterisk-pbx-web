"""動作環境のバージョン情報を集める (システム画面の「バージョン情報」用)。

OS / Python / Asterisk / Python ライブラリ / apt パッケージの版数を
その場で調べて返す。Python ライブラリは requirements/*.lock (動作確認した
版) と比べ、食い違っているものを示す。

コマンドラインから同じ一覧を出す場合:
    .venv/bin/python -m app.services.versions
(Web 画面が動いていなくても使える scripts/collect_versions.sh もある)
"""

from __future__ import annotations

import importlib.metadata as md
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.edition import current as current_edition

_ROOT = Path(__file__).resolve().parent.parent.parent

# 本ツールが使う apt パッケージ (インストーラが入れるもの)
APT_PACKAGES: list[tuple[str, str]] = [
    ("python3", "Python (OS 標準)"),
    ("python3-venv", "Python 仮想環境"),
    ("python3.11", "Python 3.11 (入れた場合)"),
    ("libtiff-tools", "FAX: TIFF → PDF (tiff2pdf)"),
    ("ghostscript", "FAX: PDF → TIFF (gs)"),
    ("libspandsp2t64", "FAX: spandsp (26.04 / 24.04)"),
    ("libspandsp2", "FAX: spandsp (22.04)"),
    ("ffmpeg", "音源の変換"),
    ("open-jtalk", "音声合成"),
    ("open-jtalk-mecab-naist-jdic", "音声合成の辞書"),
    ("hts-voice-nitech-jp-atr503-m001", "音声合成の標準の声"),
    ("curl", "Asterisk → 本ツールへの通知"),
    ("sqlite3", "データベース (CLI)"),
    ("build-essential", "コンパイラ (Asterisk / greenlet のビルド)"),
    ("libffi-dev", "コンパイル用 (Raspberry Pi 版の SMB)"),
    ("postfix", "メール送信 (任意)"),
    ("samba", "ファイル共有 (任意)"),
]


@dataclass
class LibRow:
    name: str
    installed: str | None
    locked: str | None

    @property
    def state(self) -> str:
        if self.installed is None:
            return "未導入"
        if self.locked is None:
            return "固定外"
        return "一致" if self.installed == self.locked else "相違"


@dataclass
class VersionReport:
    edition_key: str
    edition_label: str
    os_name: str
    arch: str
    kernel: str
    python: str
    python_path: str
    python_prerelease: bool
    asterisk: str | None
    libs: list[LibRow] = field(default_factory=list)
    apt: list[tuple[str, str, str | None]] = field(default_factory=list)
    lock_name: str = ""

    @property
    def lib_mismatch(self) -> list[LibRow]:
        return [r for r in self.libs if r.state == "相違"]

    def as_text(self) -> str:
        """メール等にそのまま貼れるテキスト。"""
        lines = [
            f"エディション : {self.edition_label} ({self.edition_key})",
            f"OS           : {self.os_name}",
            f"CPU / カーネル: {self.arch} / {self.kernel}",
            f"Python       : {self.python} ({self.python_path})"
            + ("  ※リリース候補版" if self.python_prerelease else ""),
            f"Asterisk     : {self.asterisk or '(取得できず)'}",
            "",
            f"[Python ライブラリ]  (固定版: requirements/{self.lock_name})",
        ]
        for r in self.libs:
            if r.installed is None:
                continue  # 任意機能 (SMB 等) の依存で、入れていないもの
            mark = f"  ← 固定版 {r.locked} と相違" if r.state == "相違" else ""
            lines.append(f"  {r.name:24s} {r.installed}{mark}")
        lines += ["", "[apt パッケージ]"]
        for name, label, ver in self.apt:
            lines.append(f"  {name:34s} {ver or '未導入':34s} {label}")
        return "\n".join(lines)


def _os_name() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        pass
    return platform.platform()


def _asterisk_version() -> str | None:
    """asterisk -V はデーモンに接続せずバイナリの版数だけを返す。"""
    try:
        out = subprocess.run(
            ["asterisk", "-V"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _apt_versions(names: list[str]) -> dict[str, str]:
    """dpkg に入っている版数。入っていないものは含まれない。"""
    try:
        out = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${db:Status-Status}\n", *names],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    result: dict[str, str] = {}
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] == "installed":
            result[parts[0]] = parts[1]
    return result


def _locked_versions(lock_file: Path) -> dict[str, str]:
    """requirements/*.lock の「名前==版」を読む (環境マーカーを考慮)。"""
    locked: dict[str, str] = {}
    try:
        text = lock_file.read_text(encoding="utf-8")
    except OSError:
        return locked
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if "==" not in line:
            continue
        spec, _, marker = line.partition(";")
        name, _, ver = spec.strip().partition("==")
        if marker.strip() and not _marker_ok(marker.strip()):
            continue
        locked[_norm(name)] = ver.strip()
    return locked


def _marker_ok(marker: str) -> bool:
    """よく出る環境マーカーだけを評価する (packaging に依存しないため)。"""
    env = {
        "python_full_version": platform.python_version(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
        "platform_python_implementation": platform.python_implementation(),
        "implementation_name": sys.implementation.name,
    }

    def as_tuple(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    for clause in re.split(r"\s+and\s+", marker):
        m = re.match(r"(\w+)\s*(==|!=|<=|>=|<|>)\s*'([^']*)'", clause.strip())
        if not m:
            continue
        key, op, val = m.groups()
        left = env.get(key, "")
        if key.startswith("python"):
            a, b = as_tuple(left), as_tuple(val)
            ok = {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b,
                  ">": a > b, ">=": a >= b}[op]
        else:
            ok = (left == val) if op == "==" else (left != val) if op == "!=" else True
        if not ok:
            return False
    return True


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def collect() -> VersionReport:
    ed = current_edition()
    locked = _locked_versions(_ROOT / "requirements" / ed.lock)

    installed: dict[str, str] = {}
    for dist in md.distributions():
        name = dist.metadata.get("Name")
        if name:
            installed[_norm(name)] = dist.version
    installed.pop("asterisk-pbx-web", None)
    for tool in ("pip", "setuptools", "wheel"):
        installed.pop(tool, None)

    names = sorted(set(installed) | set(locked))
    libs = [LibRow(n, installed.get(n), locked.get(n)) for n in names]
    # 固定版にあって入っていないもの (= その環境では不要なマーカー外) は除く
    libs = [r for r in libs if r.installed is not None or r.locked is not None]

    apt = _apt_versions([n for n, _ in APT_PACKAGES])
    py = platform.python_version()
    return VersionReport(
        edition_key=ed.key,
        edition_label=ed.label,
        os_name=_os_name(),
        arch=platform.machine(),
        kernel=platform.release(),
        python=py,
        python_path=sys.executable,
        python_prerelease=bool(re.search(r"(a|b|rc)\d*$", py)),
        asterisk=_asterisk_version(),
        libs=libs,
        apt=[(n, label, apt.get(n)) for n, label in APT_PACKAGES],
        lock_name=ed.lock,
    )


if __name__ == "__main__":
    print(collect().as_text())
