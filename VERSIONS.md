# バージョン一覧 — 通常版 (v0.8.2)

x86_64 / arm64 の Ubuntu Server 向けの配布物です。
動作確認: **Ubuntu Server 26.04 LTS (Python 3.14)** / 24.04 LTS (Python 3.12)。

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
| Python | `3.14.4-1ubuntu0.2` (26.04) / `3.12.3-1ubuntu0.17` (24.04) | OS 標準の `python3` が指す本体 (`python3.14` / `python3.12` パッケージ) |

## apt パッケージ

| パッケージ | 用途 | Ubuntu 26.04 | Ubuntu 24.04 |
|---|---|---|---|
| `python3` | Python (`python3` コマンドの提供元) | `3.14.3-0ubuntu2` | `3.12.3-0ubuntu2.1` |
| `python3.14` | Python 本体 (26.04) | `3.14.4-1ubuntu0.2` | — |
| `python3.12` | Python 本体 (24.04) | — | `3.12.3-1ubuntu0.17` |
| `python3-venv` | 仮想環境 | `3.14.3-0ubuntu2` | `3.12.3-0ubuntu2.1` |
| `python3-dev` | Python のヘッダー | `3.14.3-0ubuntu2` | `3.12.3-0ubuntu2.1` |
| `python3-pip` | pip | `25.1.1+dfsg-1ubuntu2` | `24.0+dfsg-1ubuntu1.3` |
| `libtiff-tools` | FAX: TIFF → PDF (tiff2pdf) | `4.7.0-3ubuntu5` | `4.5.1+git230720-4ubuntu2.5` |
| `ghostscript` | FAX: PDF → TIFF (gs) | `10.06.0~dfsg-3ubuntu1.1` | `10.02.1~dfsg1-0ubuntu7.9` |
| `libspandsp2t64` | FAX: spandsp 実行時ライブラリ | `0.0.6+dfsg-2.2build2` | `0.0.6+dfsg-2.1build1` |
| `libspandsp-dev` | FAX: Asterisk のビルドに必要 | `0.0.6+dfsg-2.2build2` | `0.0.6+dfsg-2.1build1` |
| `ffmpeg` | 音源の変換 | `7:8.0.1-3ubuntu2` | `7:6.1.1-3ubuntu5` |
| `open-jtalk` | 音声合成 | `1.11-5build1` | `1.11-5` |
| `open-jtalk-mecab-naist-jdic` | 音声合成の辞書 | `1.11-5build1` | `1.11-5` |
| `hts-voice-nitech-jp-atr503-m001` | 音声合成の標準の声 (multiverse) | `1.05-8build1` | `1.05-8` |
| `curl` | Asterisk → 本ツールへの通知 | `8.18.0-1ubuntu2.7` | `8.5.0-2ubuntu10.15` |
| `sqlite3` | データベース (CLI・任意) | `3.46.1-9ubuntu0.3` | `3.45.1-1ubuntu2.8` |
| `build-essential` | コンパイラ | `12.12ubuntu2.26.04.2` | `12.10ubuntu1` |
| `libffi-dev` | コンパイル用 | `3.5.2-4` | `3.4.6-1build1` |
| `pkg-config` | コンパイル用 | `2.5.1-4` | `1.8.1-2build1` |
| `libssl-dev` | Asterisk のビルド | `3.5.5-1ubuntu3.7` | `3.0.13-0ubuntu3.16` |
| `libedit-dev` | Asterisk のビルド | `3.1-20251016-1` | `3.1-20230828-1build1` |
| `libjansson-dev` | Asterisk のビルド | `2.14-2build4` | `2.14-2build2` |
| `libsqlite3-dev` | Asterisk のビルド | `3.46.1-9ubuntu0.3` | `3.45.1-1ubuntu2.8` |
| `libxml2-dev` | Asterisk のビルド | `2.15.2+dfsg-0.1ubuntu0.2` | `2.9.14+dfsg-1.3ubuntu3.9` |
| `uuid-dev` | Asterisk のビルド | `2.41.3-3ubuntu2.2` | `2.39.3-9ubuntu6.6` |
| `language-pack-ja` | 日本語環境 | `1:26.04+20260818` | `1:24.04+20260905` |
| `tzdata` | タイムゾーン | `2026c-0ubuntu0.26.04.1` | `2026c-0ubuntu0.24.04.1` |
| `postfix` | メール送信 (任意) | `3.10.6-4ubuntu2.1` | `3.8.6-1ubuntu0.1` |
| `mailutils` | メール送信 (任意) | `1:3.20-3build1` | `1:3.17-1.1build3` |
| `libsasl2-modules` | メール送信の認証 (任意) | `2.1.28+dfsg1-9ubuntu3` | `2.1.28+dfsg1-5ubuntu3.1` |
| `samba` | ファイル共有 (任意) | `2:4.23.6+dfsg-1ubuntu2.2` | `2:4.19.5+dfsg-4ubuntu9.7` |
| `ufw` | ファイアウォール (任意) | `0.36.2-9build1` | `0.36.2-6` |

`libspandsp2t64` と `libspandsp2` は同じものです (Ubuntu 24.04 以降で名前が変わりました)。
`hts-voice-nitech-jp-atr503-m001` は multiverse にあります。無効にしている場合は入りません
(画面から別の声を追加すれば音声合成は使えます)。

## Python ライブラリ

動作確認した版を `requirements/normal.lock` で固定しています。インストーラと
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
| `greenlet` | 3.5.6 | SQLAlchemy の非同期処理 (C 拡張) |  |
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
| `sqlalchemy` | 2.0.54 | データベース操作 | Python 3.10 |
| `sqlalchemy` | 2.1.3 | データベース操作 | Python 3.11 以上 |
| `starlette` | 1.7.0 | FastAPI の土台 |  |
| `typing-extensions` | 4.16.0 | 依存ライブラリ |  |
| `typing-inspection` | 0.4.4 | 依存ライブラリ |  |
| `uvicorn` | 0.54.0 | ASGI サーバー |  |
| `uvicorn-worker` | 0.4.0 | gunicorn 用ワーカー |  |

### 高速化 (`[fast]`)

| ライブラリ | 版 | 用途 | 条件 |
|---|---|---|---|
| `httptools` | 0.8.0 | 高速化 (HTTP 解析) |  |
| `pyyaml` | 6.0.3 | uvicorn の付属 |  |
| `uvloop` | 0.23.0 | 高速化 (イベントループ) |  |
| `watchfiles` | 1.3.0 | uvicorn の付属 |  |
| `websockets` | 16.1.1 | uvicorn の付属 | Python 3.10 |
| `websockets` | 17.2 | uvicorn の付属 | Python 3.11 以上 |

### 受信 FAX の SMB 保存 (`[smb]`)

| ライブラリ | 版 | 用途 | 条件 |
|---|---|---|---|
| `cffi` | 2.1.1 | cryptography の土台 (C 拡張) |  |
| `cryptography` | 50.0.2 | SMB の暗号化 (Rust 拡張) |  |
| `pycparser` | 3.0 | cffi の土台 |  |
| `pyspnego` | 0.12.3 | SMB の認証 |  |
| `smbprotocol` | 1.17.0 | 受信 FAX の SMB 保存 |  |
| `sspilib` | 0.6.0 | SMB の認証 (Windows のみ) | Windows のみ |

