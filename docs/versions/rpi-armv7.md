# バージョン一覧 — Raspberry Pi 版 (v0.8.2)

Raspberry Pi 2 Model B (32bit ARM / armv7l) の **Ubuntu Server 22.04 LTS (armhf)** 向けの配布物です。

> この一覧は、本ツールが動作確認した版と、Ubuntu の公式リポジトリから入る版を
> 2026-10-04 時点でまとめたものです。`apt upgrade` で版数は上がっていきます。
> **実際にお使いのサーバーの版数**は、「システム」画面の「バージョン情報」か、
> 次のコマンドで確認できます。
>
> ```bash
> bash /var/www/asterisk-pbx-web/scripts/collect_versions.sh
> ```

## 本体

| 項目 | 版 | 備考 |
|---|---|---|
| Asterisk PBX Web Management | 0.8.2 | |
| Asterisk | 22 系 | `install_all.sh` がインストール時点の最新 22.x をソースからビルド。動作確認: 22.10.1 / 22.11.0 |
| Python | `3.10.12-1~22.04.18` (OS 標準の 3.10) | 正式版の 3.11 があればそちらを優先 (下記) |

### Python について (重要)

Ubuntu 22.04 の公式リポジトリにある `python3.11` は **`3.11.0~rc1-1~22.04.1`** で、
**3.11.0 の正式版より前のリリース候補版 (rc1) のまま更新されていません。**
そのためインストーラは自動では選ばず、OS が保守している標準の Python 3.10 を使います。

正式版の Python 3.11 を別途入れている場合は自動でそちらを使います。
明示する場合は次のようにします。

```bash
sudo PYTHON=python3.11 bash install_all.sh
```

本ツールは Python 3.10 / 3.11 のどちらでも同じ版のライブラリで動作確認しています。

## apt パッケージ

| パッケージ | 用途 | Ubuntu 22.04 |
|---|---|---|
| `python3` | Python (`python3` コマンドの提供元) | `3.10.6-1~22.04.1` |
| `python3.10` | Python 本体 (22.04) | `3.10.12-1~22.04.18` |
| `python3.11` | Python 3.11 (22.04 は rc1) | `3.11.0~rc1-1~22.04.1` |
| `python3-venv` | 仮想環境 | `3.10.6-1~22.04.1` |
| `python3-dev` | Python のヘッダー | `3.10.6-1~22.04.1` |
| `python3-pip` | pip | `22.0.2+dfsg-1ubuntu0.7` |
| `libtiff-tools` | FAX: TIFF → PDF (tiff2pdf) | `4.3.0-6ubuntu0.13` |
| `ghostscript` | FAX: PDF → TIFF (gs) | `9.55.0~dfsg1-0ubuntu5.14` |
| `libspandsp2` | FAX: spandsp 実行時ライブラリ | `0.0.6+dfsg-2` |
| `libspandsp-dev` | FAX: Asterisk のビルドに必要 | `0.0.6+dfsg-2` |
| `ffmpeg` | 音源の変換 | `7:4.4.2-0ubuntu0.22.04.1` |
| `open-jtalk` | 音声合成 | `1.11-1.1` |
| `open-jtalk-mecab-naist-jdic` | 音声合成の辞書 | `1.11-1.1` |
| `hts-voice-nitech-jp-atr503-m001` | 音声合成の標準の声 (multiverse) | `1.05-5` |
| `curl` | Asterisk → 本ツールへの通知 | `7.81.0-1ubuntu1.29` |
| `sqlite3` | データベース (CLI・任意) | `3.37.2-2ubuntu0.8` |
| `build-essential` | コンパイラ | `12.9ubuntu3` |
| `libffi-dev` | コンパイル用 | `3.4.2-4` |
| `pkg-config` | コンパイル用 | `0.29.2-1ubuntu3` |
| `libssl-dev` | Asterisk のビルド | `3.0.2-0ubuntu1.30` |
| `libedit-dev` | Asterisk のビルド | `3.1-20210910-1build1` |
| `libjansson-dev` | Asterisk のビルド | `2.13.1-1.1build3` |
| `libsqlite3-dev` | Asterisk のビルド | `3.37.2-2ubuntu0.8` |
| `libxml2-dev` | Asterisk のビルド | `2.9.13+dfsg-1ubuntu0.13` |
| `uuid-dev` | Asterisk のビルド | `2.37.2-4ubuntu3.6` |
| `language-pack-ja` | 日本語環境 | `1:22.04+20240902` |
| `tzdata` | タイムゾーン | `2026c-0ubuntu0.22.04.1` |
| `postfix` | メール送信 (任意) | `3.6.4-1ubuntu1.4` |
| `mailutils` | メール送信 (任意) | `1:3.14-1` |
| `libsasl2-modules` | メール送信の認証 (任意) | `2.1.27+dfsg2-3ubuntu1.2` |
| `samba` | ファイル共有 (任意) | `2:4.15.13+dfsg-0ubuntu1.13` |
| `ufw` | ファイアウォール (任意) | `0.36.1-4ubuntu0.1` |

Raspberry Pi (armhf) 向けのパッケージは amd64 と同じソースから作られるため、版数は同じです。
`libspandsp2` は 24.04 以降の `libspandsp2t64` と同じものです。
`hts-voice-nitech-jp-atr503-m001` は multiverse にあります。無効にしている場合は入りません
(画面から別の声を追加すれば音声合成は使えます)。

## Python ライブラリ

動作確認した版を `requirements/rpi-armv7.lock` で固定しています。インストーラと
`update_app.sh` はこの版で入れるため、新しい版が出ても勝手には上がりません。

### 本体

| ライブラリ | 版 | 用途 | 条件 |
|---|---|---|---|
| `aiosqlite` | 0.22.1 | SQLite の非同期ドライバ |  |
| `annotated-doc` | 0.0.5 | 依存ライブラリ |  |
| `annotated-types` | 0.8.0 | 依存ライブラリ |  |
| `anyio` | 4.15.1 | 依存ライブラリ |  |
| `click` | 8.5.0 | 依存ライブラリ |  |
| `exceptiongroup` | 1.3.1 | 依存ライブラリ | Python 3.10 |
| `fastapi` | 0.142.2 | Web フレームワーク |  |
| `greenlet` | 3.5.6 | SQLAlchemy の非同期処理 (C 拡張) **※コンパイル** |  |
| `gunicorn` | 26.2.0 | 常駐プロセスの管理 |  |
| `h11` | 0.16.0 | 依存ライブラリ |  |
| `idna` | 3.20 | 依存ライブラリ |  |
| `itsdangerous` | 2.2.0 | セッション Cookie の署名 |  |
| `jinja2` | 3.1.6 | 画面テンプレート |  |
| `markupsafe` | 3.0.4 | テンプレートのエスケープ |  |
| `opentelemetry-api` | 1.45.0 | 依存ライブラリ |  |
| `pydantic` | 2.13.5 | 入力値の検証 |  |
| `pydantic-core` | 2.46.5 | pydantic の本体 (Rust 拡張) |  |
| `pydantic-settings` | 2.15.0 | .env の読み込み |  |
| `python-dotenv` | 1.2.4 | .env の読み込み |  |
| `python-multipart` | 0.0.32 | フォーム・ファイルの受信 |  |
| `sqlalchemy` | 2.0.54 | データベース操作 |  |
| `starlette` | 1.7.0 | FastAPI の土台 |  |
| `typing-extensions` | 4.16.0 | 依存ライブラリ |  |
| `typing-inspection` | 0.4.4 | 依存ライブラリ |  |
| `uvicorn` | 0.54.0 | ASGI サーバー |  |
| `uvicorn-worker` | 0.4.0 | gunicorn 用ワーカー |  |

### 受信 FAX の SMB 保存 (任意・既定では入れない)

| ライブラリ | 版 | 用途 | 条件 |
|---|---|---|---|
| `cffi` | 2.1.1 | cryptography の土台 (C 拡張) **※コンパイル** |  |
| `cryptography` | 50.0.2 | SMB の暗号化 (Rust 拡張) |  |
| `pycparser` | 3.0 | cffi の土台 |  |
| `pyspnego` | 0.12.3 | SMB の認証 |  |
| `smbprotocol` | 1.17.0 | 受信 FAX の SMB 保存 |  |
| `sspilib` | 0.6.0 | SMB の認証 (Windows のみ) | Windows のみ |

### 32bit ARM での入り方

Python のライブラリには C や Rust で書かれた部分を持つものがあり、
CPU ごとにビルド済みの配布物 (wheel) が用意されていないと、その場で
コンパイルすることになります。Raspberry Pi 2 ではこれに長い時間がかかったり、
メモリ不足で失敗したりするため、本版はビルド済みの配布物で入る構成にしてあります。

| ライブラリ | armv7 の配布物 | 対応 |
|---|---|---|
| `pydantic-core` (Rust) | あり | そのまま入る |
| `markupsafe` (C) | あり | そのまま入る |
| `greenlet` (C) | **どの版にも無い** | ソースからコンパイル (数分程度)。SQLAlchemy の非同期処理に必須 |
| `uvloop` / `httptools` (C) | 無い | **入れない** (無くても動く。通常版のみ) |
| `pyyaml` (C) | 無い | **入れない** (uvicorn の付属。不要) |
| `cryptography` (Rust) | あり | SMB 保存を使う場合のみ |
| `cffi` (C) | 無い | SMB 保存を使う場合のみコンパイル (`libffi-dev` が必要) |

Rust のコンパイラ (`cargo`) は不要です。以前のインストーラは 32bit ARM のときに
`cargo` を入れていましたが、必要なものは全てビルド済みの配布物で入るため外しました。

SQLAlchemy は 2.1 が Python 3.11 以上を要求するため、3.10 / 3.11 のどちらでも
同じ版で動くよう 2.0 系 (2.0.54) に固定しています。

