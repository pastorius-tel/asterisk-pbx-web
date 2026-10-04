# 第三者ソフトウェア・データのライセンス表示

本ツール (Asterisk PBX Web Management) 自体は MIT ライセンスです
([LICENSE](LICENSE))。**MIT が適用されるのは本リポジトリに含まれる自作の
コードとドキュメントだけです。** 以下に挙げる第三者のソフトウェア・データには
それぞれの権利者のライセンスが適用されます。

調査日: 2026-10-02

---

## 1. リポジトリに同梱しているもの

| 名称 | バージョン | ライセンス | 場所 |
|---|---|---|---|
| [htmx](https://htmx.org/) | 2.0.4 | Zero-Clause BSD (0BSD) | `app/static/js/htmx.min.js` |

0BSD は条件なしで使用・複製・改変・配布を許諾するライセンスで、帰属表示の
義務はありませんが、出所を明らかにするためここに記載しています。

`app/static/css/app.css` と `app/static/favicon.svg` は本ツールの自作物で、
MIT が適用されます。

**音声ファイル・音響モデルの類は一切同梱していません。**

---

## 2. 実行時に利用者のサーバーへダウンロードするもの

本ツールはこれらを**再配布していません**。画面やスクリプトからの操作で、
利用者のサーバーが配布元から直接ダウンロードします。ライセンスの遵守は
ダウンロードした利用者の責任になります。

### 2.1 日本語音声プロンプト ⚠ 要注意

- 配布元: <https://github.com/takao-t/asterisk-sound-ja> (GitHub アカウント: takao-t)
- 取得対象: `core-sound-ja.tgz` (約 540 ファイルの wav)
- 内容: Google Cloud Text-to-Speech で生成された日本語音声
- 使用箇所: `scripts/install_japanese_sounds.sh` / 「システム」画面

> **ライセンスの記載がありません。**
> 配布元のリポジトリに LICENSE ファイルが無く、`readme.txt` にも利用条件の
> 記述がありません (2026-10-02 時点)。GitHub の About にもライセンスの
> 表示がありません。
>
> ライセンスが示されていない著作物は、既定では**著作権者が全ての権利を
> 留保**している状態です。再配布・改変・業務での使用が許されるかは、
> 配布元に確認しない限り保証されません。
>
> **業務で使用する場合は、配布元へ利用可否を確認してください。**
> 確認が取れない場合は、本ツールの音声合成機能 (Open JTalk) で必要な
> アナウンスを生成する方法もあります。

音源は Google Cloud Text-to-Speech の出力でもあるため、生成物の利用条件に
ついては Google Cloud の利用規約も併せてご確認ください。

### 2.2 音響モデル「東北 f01」

- 配布元: <https://github.com/icn-lab/htsvoice-tohoku-f01>
- 権利者: 東北大学 Intelligent Communication Network (伊藤・能勢) 研究室
- ライセンス: クリエイティブ・コモンズ 表示 4.0 国際 (CC BY 4.0)
  <https://creativecommons.org/licenses/by/4.0/deed.ja>

CC BY 4.0 は商用利用も認められますが、**権利者名・ライセンス名・ライセンスへの
リンクの表示が必要**です。この音響モデルで生成した音声を外部へ配布・公開する
場合は、上記の表示を行ってください。

### 2.3 音響モデル「メイ」(MMDAgent)

- 配布元: <https://www.mmdagent.jp/> (MMDAgent_Example)
- 権利者: 名古屋工業大学 MMDAgent プロジェクト
- ライセンス: MMDAgent 本体は Modified BSD。同梱コンテンツのうち
  **音響モデル (.htsvoice) は CC BY**
  <https://creativecommons.org/licenses/by/3.0/deed.ja>

本ツールが取得するのは `.htsvoice` の音響モデルのみです。
**メイの 3D モデルは CC BY-NC (非営利限定) ですが、本ツールは使用しません。**
業務で使用される場合は、配布元の最新の条件をご確認ください。

### 2.4 国民の祝日データ

- 出典: 内閣府「国民の祝日について」
  <https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html>
- 取得対象: `syukujitsu.csv` (祝日の日付と名称)
- 使用箇所: 「スケジュール設定」画面の「祝日データの取得」/ `app/services/holiday_service.py`

祝日の日付は「国民の祝日に関する法律」で定まる事実の情報です。本ツールは
データを同梱せず、取り込み操作の時点で内閣府のサイトから取得します。
同じデータを掲載している e-Gov データポータルは「公共データ利用規約
(第1.0版)」(PDL1.0) を適用しており出典の記載を求めているため、画面にも
出典を表示しています。

---

## 3. 別途インストールして利用する外部プログラム

本ツールはこれらを同梱せず、OS のパッケージとして入っているものを
**別プロセスとして呼び出す**だけです (ライブラリとしてリンクしていません)。
そのため、これらのライセンスが本ツールのコードへ及ぶことはない、という
整理で MIT を選択しています。

| 名称 | ライセンス | 本ツールでの使われ方 |
|---|---|---|
| [Asterisk](https://www.asterisk.org/) | GPLv2 (他ライセンスの部分を含む) | 設定ファイルを生成し、AMI (TCP) 経由で reload を指示 |
| [FFmpeg](https://ffmpeg.org/) | LGPL / GPL (ビルド構成による) | `ffmpeg` / `ffprobe` コマンドで音源を変換 |
| [Ghostscript](https://www.ghostscript.com/) | AGPL v3 | `gs` コマンドで FAX 送信用 PDF を処理 |
| [libtiff (tiff2pdf)](http://www.libtiff.org/) | libtiff license (BSD 系) | 受信 FAX の TIFF → PDF 変換 |
| [Open JTalk](https://open-jtalk.sourceforge.net/) | Modified BSD | `open_jtalk` コマンドで音声合成 |
| hts-voice-nitech-jp-atr503-m001 | Modified BSD | 標準の音響モデル (apt で導入) |
| [MeCab / NAIST-jdic](https://taku910.github.io/mecab/) | BSD / 修正 BSD | Open JTalk の辞書 (apt で導入) |

> Ghostscript (AGPL v3) と FFmpeg (GPL 構成の場合) は、**バイナリを本ツールに
> 同梱して配布すると、配布物全体にそれぞれのライセンス条件が及ぶおそれが
> あります**。本ツールは同梱せず OS のパッケージを使う方式にしてあります。
> 配布形態を変える場合はご注意ください。

---

## 4. Python ライブラリ (pip で導入)

いずれも寛容型ライセンスで、GPL / AGPL のものは含まれていません。
版数は `requirements/normal.lock` (通常版) と `requirements/rpi-armv7.lock`
(Raspberry Pi 版) で固定しています。ライセンスは各パッケージのメタデータ
(`License-Expression` / 同梱の LICENSE) で確認しました。

| 名称 | ライセンス | 入る版 |
|---|---|---|
| FastAPI | MIT | 両方 |
| Starlette | BSD-3-Clause | 両方 |
| Uvicorn | BSD-3-Clause | 両方 |
| uvicorn-worker | BSD-3-Clause | 両方 |
| Gunicorn | MIT | 両方 |
| SQLAlchemy | MIT | 両方 |
| greenlet | MIT AND PSF-2.0 | 両方 |
| aiosqlite | MIT | 両方 |
| Pydantic / pydantic-core / pydantic-settings | MIT | 両方 |
| python-dotenv | BSD-3-Clause | 両方 |
| Jinja2 | BSD-3-Clause | 両方 |
| MarkupSafe | BSD-3-Clause | 両方 |
| python-multipart | Apache-2.0 | 両方 |
| itsdangerous | BSD-3-Clause | 両方 |
| anyio | MIT | 両方 |
| h11 | MIT | 両方 |
| click | BSD-3-Clause | 両方 |
| idna | BSD-3-Clause | 両方 |
| typing-extensions | PSF-2.0 | 両方 |
| typing-inspection / annotated-types / annotated-doc | MIT | 両方 |
| opentelemetry-api | Apache-2.0 | 両方 (FastAPI の依存) |
| exceptiongroup | MIT | Python 3.10 のみ |
| uvloop | Apache-2.0 / MIT (二重) | 通常版 |
| httptools | MIT | 通常版 |
| watchfiles | MIT | 通常版 |
| websockets | BSD-3-Clause | 通常版 |
| PyYAML | MIT | 通常版 |
| smbprotocol | MIT | 通常版 (Pi 版は任意) |
| pyspnego | MIT | 通常版 (Pi 版は任意) |
| cryptography | Apache-2.0 OR BSD-3-Clause | 通常版 (Pi 版は任意) |
| cffi | MIT-0 | 通常版 (Pi 版は任意) |
| pycparser | BSD-3-Clause | 通常版 (Pi 版は任意) |
| (任意) asyncpg | Apache-2.0 | PostgreSQL を使う場合のみ |

## 5. 商標について

本ドキュメントおよび画面中の以下の名称は、各権利者の商標または登録商標です。
本ツールは各社と提携・後援関係にはなく、接続対象の機器・サービスを示す
目的でのみ名称を使用しています。

- 「ひかり電話」「ひかり電話オフィスA」および HGW / OG の型番は
  日本電信電話株式会社 (NTT) および NTT 東日本 / 西日本の商標です
- 「Asterisk」は Sangoma Technologies Corporation の登録商標です
- その他、記載の会社名・製品名は各社の商標です

---

## 6. 免責

本ドキュメントは、公開されている情報をもとに調査日時点で整理したもので、
法的な助言ではありません。ライセンスは変更されることがあります。
商用での配布・提供を行う場合は、最新の条件をご自身でご確認のうえ、
必要に応じて専門家にご相談ください。
