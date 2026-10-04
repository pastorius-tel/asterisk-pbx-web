# Asterisk PBX Web Management System

日本のビジネスフォン運用を想定した、**Asterisk 22 用の Web 管理ツール**です。
内線・トランク・着信ルート・IVR・留守番電話・FAX・営業時間スケジュールなどを
画面から設定すると、`pjsip.conf` / `extensions.conf` などの設定ファイルを
生成して Asterisk に反映します。

SIP や Asterisk の設定ファイル記法を覚えていなくても運用できるよう、
**入力欄と選択肢だけで完結する UI** を目標にしています。画面・メッセージ・
ドキュメントはすべて日本語です。

- 言語: **Python 3.11+ / FastAPI**
- DB: **SQLite (既定) または PostgreSQL**
- UI: **Jinja2 + htmx** (外部 CDN を読み込まないので閉じた LAN でも動作)
- Asterisk 連携: 設定ファイル生成 + **AMI** 経由の `reload`
- ライセンス: **MIT**

---

## ⚠ はじめにお読みください

### 実回線での検証状況

**NTT ひかり電話オフィスA (HGW / OG 経由) を含め、実回線での動作検証は
十分に行えていません。** プリセットや設定項目は NTT の資料と一般的な構成を
もとに実装したものです。

本番回線に適用する前に、**必ず試験環境や予備回線で発着信・FAX・留守番電話の
動作を確認してください。** 設定を誤ると電話が繋がらなくなります。

### セキュリティ

このツールは **PBX の設定を丸ごと書き換えられ、留守番電話の録音も再生できます。**
画面に到達できる人 = 会社の電話を自由にできる人、と考えてください。

- `.env` に `ADMIN_PASSWORD` を設定しないと**ログイン画面は出ません**
- **インターネットに直接公開しないでください** (社外から使うなら VPN 経由)

詳細は [SECURITY.md](SECURITY.md) を参照してください。

### 第三者の音声データについて

「システム」画面からインストールできる日本語音声プロンプトは、**配布元に
ライセンスの記載がありません**。本ツールは同梱・再配布しておらず、
インストール操作はお使いのサーバーが配布元から直接ダウンロードするだけですが、
業務で使用される場合は配布元への確認をおすすめします。詳細は
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。

---

## 動作環境

配布物は **2 種類** あります。アプリの中身は同じで、入れるライブラリの構成と
インストーラの既定値だけが違います (不具合の修正は両方に同時に入ります)。

| | 通常版 | Raspberry Pi 版 |
|---|---|---|
| 配布ファイル | `asterisk-pbx-web-x.y.z.zip` | `asterisk-pbx-web-x.y.z-rpi-armv7.zip` |
| 対象 | x86_64 / arm64 のサーバー・仮想マシン | Raspberry Pi 2 Model B など 32bit ARM (armv7l) |
| OS | Ubuntu Server 26.04 / 24.04 LTS | Ubuntu Server 22.04 LTS (armhf) |
| Python | OS 標準 (26.04 は 3.14 / 24.04 は 3.12) | OS 標準の 3.10 (正式版の 3.11 があればそれ) |
| 高速化 (uvloop 等) | あり | なし (32bit ARM 向けの配布物が無いため) |
| 受信 FAX の SMB 保存 | あり | 任意 (使う場合だけ後から追加) |
| Asterisk | 22 系 (ソースビルド) | 22 系 (ソースビルド) |

各配布物に同梱の **`VERSIONS.md`** に、apt パッケージと Python ライブラリの
版数の一覧があります。実際に動いているサーバーの版数は、「システム」画面の
「バージョン情報」か `scripts/collect_versions.sh` で確認できます。

Python ライブラリは動作確認した版を `requirements/*.lock` で固定しています。
インストール・更新のどちらでもこの版で入るため、新しい版が出ても勝手には上がりません。

---

## インストール

### A. 一括インストール (新規サーバー向け・推奨)

何も入っていない Ubuntu Server に対して、日本語環境の設定から
Asterisk のビルド、本ツールの配置、systemd 登録までを一度に行います。

```bash
# リポジトリを取得して実行
sudo apt update && sudo apt install -y git
git clone https://github.com/<アカウント>/asterisk-pbx-web.git
cd asterisk-pbx-web
sudo bash scripts/install_all.sh
```

配布 zip を使う場合は、**お使いの機械に合った方の zip** と `install_all.sh` を
同じディレクトリに置いて `sudo bash install_all.sh` を実行するか、zip の場所を
明示します。

```bash
# 通常版
sudo APP_ZIP=/path/to/asterisk-pbx-web-x.y.z.zip bash install_all.sh
# Raspberry Pi 版
sudo APP_ZIP=/path/to/asterisk-pbx-web-x.y.z-rpi-armv7.zip bash install_all.sh
```

間違えて通常版を 32bit ARM に入れようとした場合は、インストーラが検出して
Raspberry Pi 版の構成に切り替えます。git clone した場合は CPU の種類から
自動で判定します。

実行される内容:

1. パッケージリストの更新
2. 日本語環境 (ロケール `ja_JP.UTF-8` / タイムゾーン `Asia/Tokyo` / vim の日本語対応)
3. 基本ツール (`unzip` `curl` `git` `build-essential` `samba` 等)
4. Python 3 環境 (使う Python を自動で選ぶ。下記「Raspberry Pi 版について」参照)
5. **Asterisk 22 をソースからビルド** (`--with-pjproject-bundled`、`res_fax_spandsp` 有効)
6. 本ツールを `/var/www/asterisk-pbx-web` へ配置し、`.env` を自動生成
   (`SECRET_KEY` / `AMI_SECRET` / **`ADMIN_PASSWORD`** をランダム生成)
7. Python 依存パッケージを venv に導入 (`requirements/*.lock` の版で固定)
8. 日本語音声パックの導入
9. systemd サービス登録 (`asterisk-pbx-web`)
10. ufw の設定

**完了時に管理画面の URL とログインパスワードが表示されます。必ず控えてください。**

所要時間は Asterisk のビルドを含めて 20〜40 分程度です
(Raspberry Pi ではさらにかかります)。

主なオプション:

```bash
# 別の場所に入れる / ポートを変える
sudo INSTALL_DIR=/opt/asterisk-pbx-web WEB_PORT=8080 bash install_all.sh

# Asterisk が導入済みならビルドを飛ばす
sudo SKIP_ASTERISK=1 bash install_all.sh

# Samba / ufw の設定を飛ばす
sudo SKIP_SAMBA=1 SKIP_UFW=1 bash install_all.sh

# 使う Python を指定する
sudo PYTHON=python3.11 bash install_all.sh
```

#### Raspberry Pi 版について

- **Python**: Ubuntu 22.04 の公式リポジトリにある `python3.11` は
  `3.11.0~rc1` (正式版より前のリリース候補版) のまま更新されていません。
  インストーラはこれを自動では選ばず、OS が保守している標準の Python 3.10 を
  使います。正式版の 3.11 を入れてある場合は自動でそちらを使います。
- **コンパイル**: `greenlet` (データベースの非同期処理に必須) だけは 32bit ARM
  向けのビルド済み配布物がどの版にも無く、インストール中にコンパイルします
  (Raspberry Pi 2 で数分程度)。それ以外は全てビルド済みの配布物で入ります。
  Rust のコンパイラは不要です。
- **高速化オプション**: `uvloop` / `httptools` は 32bit ARM 向けの配布物が無いため
  入れません。無くても標準の仕組みで動き、管理画面の用途では体感差はありません。
- **受信 FAX の SMB 保存**: 既定では入れていません。使う場合は次を実行します
  (依存ライブラリのコンパイルに数分かかります)。

  ```bash
  cd /var/www/asterisk-pbx-web
  sudo -u asterisk .venv/bin/pip install -c requirements/rpi-armv7.lock -e '.[smb]'
  sudo systemctl restart asterisk-pbx-web
  ```
- **Asterisk のビルド**: 通常のサーバーよりかなり時間がかかります。Asterisk が
  導入済みなら `SKIP_ASTERISK=1` で飛ばせます。

### B. 手動インストール (本ツールだけを入れる場合)

Asterisk が既に動いているサーバーに、本ツールだけを追加する手順です。

```bash
# 1. 必要なパッケージ
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg libtiff-tools ghostscript

# 2. 配置して依存を入れる
sudo mkdir -p /var/www
sudo chown $(whoami):$(whoami) /var/www
cd /var/www
git clone https://github.com/<アカウント>/asterisk-pbx-web.git
cd asterisk-pbx-web
# (配布 zip を使う場合は、代わりにここへ展開)

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 3. 設定ファイルを用意 (次章参照)
cp .env.example .env
nano .env

# 4. 起動
python -m app.main
```

ブラウザで `http://<サーバーのIP>:8080/` を開きます。

PostgreSQL を使う場合は `pip install -e ".[postgres]"` を実行し、
`.env` の `DATABASE_URL` を
`postgresql+asyncpg://pbx:pbx@localhost:5432/pbx` の形式に変更します。

### C. 更新 (すでに動いているサーバーに新しい版を入れる)

設定 (`.env`)・データベース (`pbx.db`)・アップロード済みの音源・バックアップは
**インストール先の中にあります**。配布 zip にはこれらが含まれていないので、
上書きしても消えません。

**お使いの機械に合った方の zip** (通常版 / Raspberry Pi 版) を用意して、
同梱の `update_app.sh` を実行します。

```bash
cd /tmp
unzip -qo ~/asterisk-pbx-web-x.y.z.zip          # Raspberry Pi なら -rpi-armv7.zip
sudo APP_ZIP=~/asterisk-pbx-web-x.y.z.zip bash /tmp/asterisk-pbx-web/scripts/update_app.sh
```

`update_app.sh` は次を順に行います。

1. `.env` と `pbx.db` をバックアップ (`.env.bak-日時` など)
2. サービスを停止して新しい版を上書き展開
3. 配布版に合わせて、**動作確認済みの版** (`requirements/*.lock`) で依存ライブラリを入れ直す
4. systemd ユニットを新しい起動方法に合わせる (必要な場合のみ)
5. サービスを起動

インストール先を変えている場合は `INSTALL_DIR=/opt/asterisk-pbx-web` を付けます。

**最後に、Web 画面を開いて左下の「変更を Asterisk へ反映」を必ず 1 回押してください。**
ダイヤルプラン (`extensions.conf` など) は、このボタンを押したときに初めて
書き出されます。押さないと、更新前の設定ファイルのまま動き続けます。

<details>
<summary>手作業で更新する場合</summary>

```bash
INSTALL_DIR=/var/www/asterisk-pbx-web
sudo cp -a "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.bak"
sudo cp -a "$INSTALL_DIR/pbx.db" "$INSTALL_DIR/pbx.db.bak"
sudo systemctl stop asterisk-pbx-web

cd /tmp && unzip -qo ~/asterisk-pbx-web-x.y.z.zip
sudo cp -r /tmp/asterisk-pbx-web/. "$INSTALL_DIR"/

cd "$INSTALL_DIR"
# 通常版
sudo .venv/bin/pip install -q -c requirements/normal.lock -e '.[fast,smb]'
# Raspberry Pi 版
sudo .venv/bin/pip install -q -c requirements/rpi-armv7.lock -e .

# v0.8.0 より前から更新する場合: 起動方法を新しいものに切り替える
sudo sed -i 's/uvicorn\.workers\.UvicornWorker/uvicorn_worker.UvicornWorker/' \
    /etc/systemd/system/asterisk-pbx-web.service
sudo systemctl daemon-reload

sudo chown -R asterisk:asterisk "$INSTALL_DIR"
sudo systemctl start asterisk-pbx-web
```

</details>

`git clone` で入れた場合は、zip の代わりに `git pull` してから `update_app.sh` を実行します。

```bash
cd "$INSTALL_DIR"
sudo -u asterisk git pull
sudo bash scripts/update_app.sh
```

---

---

## 初期設定

### 1. `.env` の設定

`.env.example` をコピーして編集します。**最低限、次の 2 つは必ず設定してください。**

```bash
# 管理画面のログインパスワード
# 未設定だとログイン画面が出ず、誰でも PBX を操作できる状態になります
ADMIN_USER=admin
ADMIN_PASSWORD=<推測されにくい文字列>

# AMI のパスワード (manager.conf に書き出されます)
AMI_SECRET=<推測されにくい文字列>
```

パスワードの作り方の例:

```bash
head -c 18 /dev/urandom | base64
```

平文を `.env` に置きたくない場合は、ハッシュで設定できます。

```bash
python -m app.auth hash
# 出力された ADMIN_PASSWORD_HASH=... の行を .env に貼る
```

主な設定項目:

| 項目 | 既定値 | 説明 |
|------|--------|------|
| `HOST` | `127.0.0.1` | 待ち受けアドレス。**LAN の他の PC から使うなら `0.0.0.0`** |
| `PORT` | `8080` | 待ち受けポート |
| `DEBUG` | `false` | `true` にすると例外時にスタックトレースが表示される (運用時は `false`) |
| `SECRET_KEY` | (空) | 空なら起動ごとにランダム生成 (再起動でログアウトされる) |
| `SESSION_MAX_AGE` | `43200` | ログインの有効時間 (秒)。既定 12 時間 |
| `ASTERISK_CONFIG_DIR` | `/etc/asterisk` | 設定ファイルの出力先 |
| `ASTERISK_SOUNDS_DIR` | `/var/lib/asterisk/sounds/ja/managed` | 生成した音源の配置先 |
| `TRUSTED_HOOK_HOSTS` | `127.0.0.1,::1` | Asterisk からの内部通知を受け付ける送信元 IP |

### 2. ディレクトリの書き込み権限

本ツールは Asterisk の設定ファイル・音源・スプールを直接読み書きします。
実行ユーザーにその権限が無いと、音源の登録や日本語音声のインストールが
失敗します。

**まず「システム」画面の「ファイル権限の診断」を見てください。** 必要な
ディレクトリの一覧と、書き込めるかどうか、書けない場合の修正コマンドが
その場に表示されます。問題があるときは画面の先頭にも警告が出ます。

まとめて直す場合:

```bash
sudo FIX_PERMISSIONS=1 bash scripts/setup_dependencies.sh
```

手で設定する場合:

```bash
# 本ツールを動かすユーザーを asterisk グループに入れる
sudo usermod -aG asterisk $(whoami)

# 設定ディレクトリ・音源ディレクトリをグループ書き込み可に
sudo chgrp -R asterisk /etc/asterisk /var/lib/asterisk/sounds
sudo chmod -R g+w /etc/asterisk /var/lib/asterisk/sounds

# FAX・留守番電話・音響モデルを使う場合
sudo mkdir -p /var/spool/asterisk/fax /var/lib/asterisk-pbx-web/fax \
              /var/lib/asterisk-pbx-web/hts-voice
sudo chgrp -R asterisk /var/spool/asterisk /var/lib/asterisk-pbx-web
sudo chmod -R g+w /var/spool/asterisk /var/lib/asterisk-pbx-web
```

グループの変更を反映するには、一度ログアウトして入り直してください。

本ツールが使うディレクトリは次のとおりです。

| 用途 | 既定のパス | 必要な権限 |
|---|---|---|
| Asterisk 設定ファイル | `/etc/asterisk` | 書き込み |
| 音源の変換先 | `/var/lib/asterisk/sounds/ja/managed` | 書き込み |
| 日本語音声プロンプト | `/var/lib/asterisk/sounds` | 書き込み |
| 日時読み上げ音声 | `/var/lib/asterisk/sounds/ja/digits` | 書き込み |
| TTS 音響モデル | `/var/lib/asterisk-pbx-web/hts-voice` | 書き込み |
| アップロード一時置き場 | `./uploads` | 書き込み |
| FAX スプール | `/var/spool/asterisk/fax` | 書き込み |
| FAX 保存先 | `/var/lib/asterisk-pbx-web/fax` | 書き込み |
| 留守番電話スプール | `/var/spool/asterisk/voicemail` | 読み書き |
| バックアップ | `./backups` | 書き込み |
| データベース | `./pbx.db` | 書き込み |

> TTS の音響モデル (声) は、apt で入る標準の声が `/usr/share/hts-voice`
> (root 所有) にあり書き込めないため、画面から追加する声は
> `/var/lib/asterisk-pbx-web/hts-voice` に保存します。一覧表示では
> 両方を探すので、apt で入れた声もそのまま選べます。

### 3. 最初に行う設定の順番

ログイン後、次の順で設定していきます。

1. **トランク** — 回線 (ひかり電話など) を登録
2. **内線** — 電話機を登録 (SIP パスワードは自動生成されます)
3. **発信ルート** — 外線発信のパターンとトランクの対応付け
4. **着信ルート** — 着信番号 (DID) ごとの転送先
5. 必要に応じて **IVR / リンググループ / スケジュール / 留守番電話 / FAX**
6. **左下の「変更を Asterisk へ反映」を押す**

最後の「変更を Asterisk へ反映」を押すまで、設定は Asterisk に反映されません。
このボタンは設定ファイルを生成し、AMI 経由で `pjsip reload` / `dialplan reload`
を実行します。

### 4. 電話機側の設定

内線一覧に表示される情報をそのまま電話機・ソフトフォンに入力します。

| 電話機の項目 | 入力する値 |
|------------|-----------|
| SIP サーバー / ドメイン | Asterisk サーバーの IP |
| ユーザー名 / 認証 ID | 内線番号 (例 `201`) |
| パスワード | 内線一覧に表示される SIP パスワード |
| ポート | `5060` (「システム」画面で変更可) |

登録できたか確認するコマンド:

```bash
sudo asterisk -rx "pjsip show endpoints"
```

---

## 生成されるダイヤルプラン

「変更を Asterisk へ反映」を押すと、`ASTERISK_CONFIG_DIR` に
次のファイルが生成されます。**手で編集しても次回の反映で上書きされます。**

```
extensions.conf    ダイヤルプラン
pjsip.conf         内線・トランク・トランスポート
queues.conf        キュー
voicemail.conf     留守番電話
musiconhold.conf   保留音
res_parking.conf   コールパーク
res_fax.conf       FAX の通信速度 (モデム) と ECM
features.conf      通話中の機能キー
manager.conf       AMI
rtp.conf           RTP ポート範囲
```

### 初期状態の特番一覧

内線から直接ダイヤルできる番号です。

| 番号 | 機能 |
|------|------|
| `700` | コールパーク (保留してスロットに預ける) |
| `701`–`720` | パークしたスロットの取り出し |
| `*8` | 直前にパークした通話の取り出し |
| `*97` | 自分の留守番電話を聞く |
| `*98` | 内線番号を指定して留守番電話を聞く |
| `*43` | エコーテスト (音声の往復確認) |
| `*7` + 2〜3 桁 | 短縮ダイヤル (電話帳で割り当てた番号) |

パークの番号・スロット範囲は「コールパーク」画面で変更できます。

### 主なコンテキストの構成

| コンテキスト | 役割 |
|------------|------|
| `from-internal` | 内線からの発信すべての入口。特番・内線同士・外線発信ルートを含む |
| `from-trunk-<トランク名>` | トランクからの着信の入口。着信番号 (DID) を取り出して振り分ける |
| `did-<トランク名>` | 着信番号ごとの転送先 |
| `ivr-<IVR名>` | IVR (音声ガイダンス) のメニュー |
| `ring-group-<番号>` | リンググループ (複数内線の同時呼び出し) |
| `queue-<番号>` | キュー |
| `timecond-<名前>` | 営業時間による振り分け |
| `fax-receive` | FAX 受信 |
| `nuisance-blocklist-check` | 迷惑電話の判定 |

### 内線番号のルール

日本の電話番号は、外線発信も特番もすべて **0 か 1 で始まります**
(携帯 090/080/070、固定 03/06、フリーダイヤル 0120、110・119・104 など)。

これを利用して、既定では次のようになっています。

- 内線同士のダイヤルプランは **3〜5 桁で、先頭が 2〜9 の番号にだけ反応する**
  (`_NXX` / `_NXXX` / `_NXXXX`)
- 0 または 1 で始まる番号は内線パターンに一切マッチしないので、
  **確実に外線発信ルート側に回る**
- 内線を作るときに 0/1 で始まる番号を入れるとエラーになる

「システム」画面の「内線番号 / 外線発信ポリシー」で OFF にすると、
0〜9 すべてにマッチする動作 (`_XXX` / `_XXXX` / `_XXXXX`) に戻ります。

### ダイヤルパターンの記法

発信ルートや迷惑電話ブロックでパターンを書くときに使います。

| 記号 | 意味 |
|-----|------|
| `_` | パターン指定の先頭につけるマーカー |
| `X` | 0〜9 |
| `Z` | 1〜9 |
| `N` | 2〜9 |
| `[2-9]` | 範囲指定 |
| `.` | 1 文字以上の任意の桁 |
| `!` | 0 文字以上 |

例:

```
_0X.              0 で始まる番号すべて (外線全般)
_0[789]0XXXXXXXX  携帯電話 (070/080/090)
_0120XXXXXX       フリーダイヤル
```

---

## 使い方

### 内線

電話機 1 台につき 1 件登録します。SIP パスワードは自動生成されるので、
そのまま電話機に設定してください。

主な設定項目:

- **留守番電話** — 有効にすると、応答がないときに録音します
- **応答しなかったときの動作** — 留守番電話へ / 切断 を選べます
- **応答しなかったときに流す音源** — 「音源」画面で作った案内を選びます
  (未設定だと案内なしで録音が始まります)
- **呼び出し秒数** — 何秒鳴らしてから留守番電話に回すか

### トランク

回線 (ひかり電話、IP 電話事業者など) を登録します。
プリセットを選ぶとコーデックや NAT 設定が自動で入ります。

登録後の確認:

```bash
sudo asterisk -rx "pjsip show registrations"
```

`Registered` と表示されれば認証できています。

### 発信ルート

外線にかけるときのパターンとトランクの対応付けです。
すべての外線をひとつのトランクに流すなら、パターン `_0X.` を 1 件作るだけで足ります。

- **先頭から削る桁数** — 「0 発信」の 0 を取り除きたい場合などに使います
- **発信者番号の上書き** — 相手に通知する番号を変えたいとき

### 着信ルート

着信番号 (DID) ごとに、どこへ繋ぐかを決めます。
転送先には内線・リンググループ・キュー・IVR・留守番電話・FAX 受信・
営業時間による振り分け (時間条件) を選べます。

### IVR (音声ガイダンス)

「1 を押すと営業部、2 を押すと総務」のような自動音声応答です。

1. 「音源」画面でガイダンス音声を用意 (MP3 アップロードか音声合成)
2. 「IVR」画面で新規作成し、ガイダンス音源を選ぶ
3. キー (0〜9、`*`、`#`) ごとに転送先を設定
4. タイムアウト時・無効キー時の動作を設定

### スケジュール (営業時間)

「平日 9:00〜17:30 は IVR、時間外は留守番電話」といった振り分けを設定します。
設定は 1 画面にまとまっており、上から順に進めます。

1. **年間カレンダー** — 平日・休日・祝日・指定休日を色で確認。
   日付をクリックすると、その日だけ別の営業時間や終日休業を割り当てられます
2. **日次パターン** — 1 日の営業時間帯のテンプレート
   (`09:00-12:00,13:00-18:00` のようにカンマ区切りで複数指定可)。
   **空欄にすると終日休業のパターン**になります
3. **曜日パターン** — 曜日ごとに日次パターンを割り当て、
   営業時間内／時間外それぞれの転送先を決めます。
   「祝日」「指定休日」にチェックを入れると曜日設定より優先されます
4. **指定休日** — 年末年始・お盆・GW など。「毎年」にすると年を無視して毎年適用されます
5. **祝日データの取得** — 内閣府が公開している「国民の祝日」CSV を取り込みます。
   成人の日や春分の日のように毎年日付が変わる祝日も自動で反映されます。
   年に一度、翌年分が公開されたら取り込み直してください。
   サーバーがインターネットに出られない場合は CSV を手動アップロードできます

判定の優先順位は **指定休日 → 祝日 → 曜日** です。

### 留守番電話

録音されたメッセージを画面から再生・ダウンロード・削除できます。
内線ごとにメール通知先を設定すると、録音時に音声を添付して送信します
(メールの送信設定は「FAX 送受信」→「メール設定」で行います)。

電話機からは `*97` で自分のメッセージを聞けます。

### FAX 送受信

Asterisk の `res_fax_spandsp` を使って FAX を送受信します。

- **受信** — TIFF を PDF に変換して保存。メール添付・SMB 共有への保存も可能
- **送信** — PDF をアップロードすると FAX 用 TIFF に変換して送信

`tiff2pdf` (libtiff-tools) と `gs` (Ghostscript) が必要です。

#### 出ても問題ない警告

受信が成功していても、Asterisk のコンソールに次の警告が出ることがあります。

```
WARNING res_fax_spandsp.c: spandsp_log: WARNING T.30 Non-ECM carrier not found
```

送信側は「受信してよい」の合図 (CFR) を受け取ったあと、画像データの前に
短い同期信号 (トレーニング) を送ります。回線上の遅れや揺らぎで、その
**最初の数百ミリ秒が乱れると受信側の同期が 1 度失敗し**、この警告が出ます。
直後に送られてくる本来の同期信号で同期し直せば、受信はそのまま続きます。

判断は**最終結果**で行ってください。

```
NoOp(FAXSTATUS=SUCCESS PAGES=1 ERROR= DETAIL=OK ...)
```

`FAXSTATUS=SUCCESS` で、ログに `Page quality is perfect` / `Bad rows = 0` が
出ていれば問題ありません。「FAX 送受信」画面の履歴も成功になります。

ただし、`FAXSTATUS=FAILED` で `ERROR=` に `Carrier lost during fax receive`
が出て失敗している場合は、同期し直す前に時間切れになっています。
最大ボーレートを下げてください (下記「受信できないとき」の 2)。

#### FAX が「留守番電話に録音される」「電話機が鳴るだけ」になるとき

まず Asterisk のコンソールに次の NOTICE が出ていないか確認してください。

```
chan_pjsip.c: FAX CNG detected on 'PJSIP/...' but no fax extension in '<コンテキスト名>'
```

これは「FAX の発信音は検知したが、その時点で通話が居たコンテキストに
FAX への入口が無かった」という意味で、その着信は FAX として受け取られず、
そのまま元の処理 (留守番電話の録音など) に流れてしまいます。

`FAXSTATUS` の行自体が出ないのが特徴です。失敗ではなく、**FAX 受信が
そもそも始まっていない**ためです。

本ツールは着信が通りうる全てのコンテキストに FAX の入口を入れており、
「変更を Asterisk へ反映」を押すと、取りこぼす場所が残っていないかを
自動で点検して画面に警告します。この NOTICE が出る場合は、まず
**反映をやり直して**ください。それでも出るときは、警告の内容を添えて
お知らせください。

> 音声と FAX を 1 つの番号で共用し、着信ルートを時間条件やリンググループ
> 経由にしている構成で起きやすい症状です。

#### 受信できないとき (`FAXSTATUS=FAILED` / `PAGES=0`)

**1. まず失敗の理由を読む**

受信が終わると、Asterisk のコンソールに次の行が出ます。

```
NoOp(FAXSTATUS=FAILED PAGES=0 ERROR=... DETAIL=... MODE=audio RATE=... REMOTE=...)
```

`ERROR` が spandsp から返ってきた実際の失敗理由で、同じ内容は
「FAX 送受信」画面の履歴にも残ります。`MODE` は実際に使われた方式
(`audio` = G.711 / `T38`) です。

| `ERROR` の内容 | 意味と対処 |
|---|---|
| `Unexpected message received` | 通信中に順番どおりでないメッセージが届いた。**最大ボーレートを下げる** (下記 2) |
| `Failed to train with any of the compatible modems` | モデムの折衝に失敗。同じく最大ボーレートを下げる |
| `Timed out waiting for the first message` | 相手の音が届いていない。コーデック (`ulaw`) と HGW 側の設定を確認 |
| `The CED tone exceeded 5s` | 相手が FAX として応答していない。相手番号・回線を確認 |

**2. 最大ボーレートを下げる (もっとも効果が大きい)**

「FAX 送受信」→「設定」の**最大ボーレート**を `9600`、それでも駄目なら
`4800` にします。変更後は「変更を Asterisk へ反映」を押してください。

この設定は、使用する FAX モデムを `res_fax.conf` の `modems=` として
書き出します。

| 最大ボーレート | 生成される `modems` | 備考 |
|---|---|---|
| 14400 / 12000 | `v17,v27,v29` | Asterisk 既定。音声回線ではもっとも不安定 |
| 9600 / 7200 | `v27,v29` | 音声 (G.711) 回線で安定しやすい |
| 4800 / 2400 | `v27` | もっとも確実だが遅い |

> Asterisk 22 の `res_fax_spandsp` は `FAXOPT(maxrate)` を spandsp へ
> 渡していないため、ダイヤルプラン側の指定だけでは速度は変わりません。
> 実際に制限しているのは `res_fax.conf` の `modems=` で、本ツールは
> この値から導出して生成しています。

**3. 設定画面で「T.38 を使う」を無効にしてみる**

ひかり電話の HGW/OG は T.38 に対応しておらず、Asterisk のログに
`refused to negotiate T.38` が毎回出ます。通常は自動的に音声 (G.711)
モードへ切り替わりますが、機種によってはこの切り替えの提案のあと音声が
届かなくなり、受信できなくなることがあります。無効にすると最初から
音声モードで送受信します。

**4. 複数ページの 2 ページ目以降が欠ける場合**

設定画面の「ECM (エラー訂正) を使う」を無効にしてみてください
(ログに `T.30 ECM carrier not found` が多発している場合が該当します)。

**5. T.30 のやりとりを詳しく見る**

本ツールは `ReceiveFAX` / `SendFAX` に常にデバッグ指定を付けており、
「変更を Asterisk へ反映」を押すと `logger.conf` に `fax` ログレベルも
書き出すので、通常は何もしなくても DIS/DCS/TCF の交換過程が
コンソールに出ます。

```
console => notice,warning,error,fax
```

Asterisk は FAX のトレースを専用のログレベルへ出力しているため、この
指定が無いとどこにも出力されません (`fax set debug on` を打っても同じ)。

`logger.conf` を手で書き換えている場合は、本ツールは上書きしません
(先頭に `AUTO-GENERATED` の行があるファイルだけ更新します)。その場合は
上の 1 行を自分で足して `sudo asterisk -rx "logger reload"` してください。

**6. FAX モジュールが入っているか確認する**

`ERROR` に上記のような T.30 のメッセージが出ていれば `res_fax_spandsp`
は動いています。`FAXSTATUS` 自体が出ない場合だけ確認してください。

```bash
sudo asterisk -rx "fax show capabilities"
sudo asterisk -rx "module show like fax"
```

`res_fax_spandsp.so` が無い場合は、Asterisk のビルド時に有効化されて
いません。`menuselect` で `res_fax_spandsp` を有効にして再ビルドするか、
`scripts/update_asterisk.sh` を実行してください
(`libspandsp-dev` が入っていないとこのモジュールは作られません)。

### 電話帳と短縮ダイヤル

よく使う番号を登録し、**2〜3 桁の短縮番号**を割り当てられます。
電話機から `*7` + 短縮番号 でダイヤルできます
(例: 短縮番号 `10` → `*710`)。

発着信履歴の「⋯」メニューからも電話帳に登録できます。
**短縮番号を設定しないと `*7` からは発信できません**ので、
発信に使いたい相手には短縮番号を割り当ててください。

### 発着信履歴

着信と発信を分けて一覧表示します。番号・日時・着信先 (内線 / 留守番電話 / FAX 等)・
通話時間・結果が記録されます。

各行の「⋯」メニューから、その番号を**電話帳**または**迷惑電話ブロック**に登録できます。

履歴の削除は、留守番電話・FAX の画面と同じ操作です。

- **1 件ずつ削除** — 各行の「削除」ボタン
- **まとめて削除** — 行の左端にチェックを入れて「選択した項目を削除」。
  見出しのチェックで表示中の全件を選べます
- **古い履歴を削除** — 一覧の下の「90 日より古い履歴を削除」

削除した後も、表示期間と番号の絞り込みはそのまま保たれます。

### 迷惑電話ブロック

番号またはパターンを登録して、該当する着信を自動で処理します。

```
0312349999        この番号だけ
_0120XXXXXX       フリーダイヤル全般
_0[789]0XXXXXXXX  携帯電話全般
```

処理は 3 種類から選べます。

| 処理 | 動作 | 相手の通話料 |
|------|------|------------|
| 応答せずに即切断 | 呼び出し音が鳴る前に切断 | かからない |
| メッセージを流してから切断 | 応答して案内を再生し、再生後に切断 | **かかる** |
| 留守番電話へ転送 | 指定内線の留守番電話で用件を録音 | かかる |

「メッセージを流してから切断」で流す音源は、先に「音源」画面で用意して
おきます (MP3 のアップロード、または文章からの音声合成)。

この判定は**着信の一番最初**、着信ルートの振り分けより前に行われます。
そのため、ブロックした番号で内線が鳴ることはありません。
非通知 (発信者番号なし) の着信は判定の対象外で、通常どおり着信します。

### 音源

案内音声を用意する方法は 2 つあります。

- **MP3 等をアップロード** — ffmpeg が Asterisk 用の WAV に自動変換します
- **文章から音声を作る** — Open JTalk による日本語音声合成。
  声の種類・速度・高さを選べます
- **声を追加する** — 女性の声 (東北 f01 / メイ) を画面からダウンロードして
  追加できます。手持ちの `.htsvoice` をアップロードして使うこともできます

### 日本語音声プロンプト

「システム」画面のボタンから、日本語の音声プロンプト (約 540 ファイル) を
インストールできます。`sudo` は不要で、サービス実行ユーザーのまま実行
できます。失敗する場合は同じ画面の「ファイル権限の診断」を確認してください
(`/var/lib/asterisk/sounds` への書き込み権限が必要です)。

コマンドから実行することもできます。

```bash
sudo bash scripts/install_japanese_sounds.sh
```

### 保留音・コールパーク

- **保留音クラス** — アップロードした音源をまとめて保留音として使います
- **コールパーク** — `700` にダイヤルすると通話を預けられ、
  アナウンスされたスロット番号 (`701` 等) を別の電話機からダイヤルすると取り出せます

### バックアップ

「システム」画面から、DB と `.env` をまとめた zip を作成・ダウンロードできます。
復元はアップロードしたファイルを検証して待機させ、画面に表示されるコマンドを
実行して確定します (稼働中の DB を直接上書きしない方式です)。

---

## NTT ひかり電話オフィスA の設定

> **⚠ 実回線での動作検証は行えていません。**
> 以下は NTT の資料と一般的な構成をもとにした実装・手順です。
> 本番回線に適用する前に、必ず試験環境や予備回線で発着信を確認してください。

HGW 経由と OG 経由を別のプリセットとして用意しています。

```
HGW 経由:
  [Asterisk]── LAN ──[HGW: PR-500MI / PR-600 系]── 光回線 ──[NTT 網]

OG 経由:
  [Asterisk]── LAN ──[OG410Xa / OG420Xa / OG820Xa]── 光回線 ──[NTT 網]
                          └─ ビジネスホン (アナログ / ISDN)
```

### 1. HGW / OG 側の準備

管理画面 (`http://ntt.setup` / 既定 `192.168.1.1`) で行います。

1. **電話設定 → 内線設定** (HGW) または **電話設定 → IP端末/GW収容設定** (OG) で内線端末を追加
2. **ダイジェスト認証**を「行う」に変更
3. **内線番号** (HGW は 1 桁、OG は 2 桁が典型) と任意の**パスワード**を設定
4. 内線に対応する**ユーザID** (4 桁、例 `0003`) をメモ
5. **着信番号設定**で、その内線に主番号と追加番号をすべて割り当て

> **「内線番号」と「ユーザID」は別物です。**
> 内線番号は 1〜2 桁 (例 `3`、`10`)、ユーザID は 4 桁ゼロ埋め (例 `0003`、`0010`) です。
> SIP REGISTER の宛先には内線番号を、認証ユーザー名にはユーザID を使います。

### 2. 本ツールでの登録

「トランク」→ 新規追加 → プリセットで
**「ひかり電話オフィスA / HGW 経由」** または **「/ OG 経由」** を選びます。

| 項目 | 入力値 |
|-----|-------|
| トランク名 | 任意の英数字 (例 `hikari_main`) |
| 接続先ホスト | HGW / OG の LAN 側 IP (例 `192.168.1.1`) |
| HGW/OG 内線番号 | HGW なら `3`、OG なら `10` 等 (1〜2 桁) |
| 認証ユーザID | HGW / OG が表示する 4 桁 ID (例 `0003`) |
| パスワード | HGW / OG で設定した端末パスワード |
| 主番号 | 契約電話番号 (例 `0312345678`) |
| 追加番号 | カンマ区切り (例 `0312345679,0312345680`) |
| 同時通話数 | 契約チャネル数 |

「NTT 系オプション」の `disable_rport` / `trust_id_inbound` / `send_pai` は、
HGW / OG 経由なら OFF のままで構いません
(OG 経由で発信時に `400 Bad Request` が出る場合のみ `disable_rport` を ON に)。

### 3. 着信番号 (DID) の振り分け

ひかり電話オフィスA は、**追加番号宛の着信も主番号宛で INVITE される**仕様です。
そのため本ツールは、SIP の `To` ヘッダから実際の着信番号を取り出して
振り分けるダイヤルプランを自動生成します。

```ini
[from-trunk-hikari_main]
exten => _X!,1,NoOp(Hikari INBOUND raw exten=${EXTEN})
 same => n,Set(TO_DID=${PJSIP_HEADER(read,To)})
 same => n,Set(TO_DID=${CUT(TO_DID,@,1)})
 same => n,Set(TO_DID=${CUT(TO_DID,:,2)})
 same => n,Goto(did-hikari_main,${TO_DID},1)
```

「着信ルート」で番号ごとの転送先を登録すれば、この仕組みに自動で組み込まれます。

### 4. 確認

```bash
sudo asterisk -rvvv
> pjsip show registrations      # Registered になっているか
> dialplan show did-hikari_main # 着信番号ごとの転送先
```

認証できない場合は、内線番号 (1〜2 桁) とユーザID (4 桁) を取り違えていないか、
HGW / OG 側で「ダイジェスト認証 = 行う」になっているかを確認してください。

---

## よく使うコマンド

```bash
# 本ツールの状態・ログ
sudo systemctl status asterisk-pbx-web
sudo journalctl -u asterisk-pbx-web -f
sudo systemctl restart asterisk-pbx-web

# Asterisk のコンソール (通話中の動きが流れます)
sudo asterisk -rvvv

# 登録状況
sudo asterisk -rx "pjsip show endpoints"
sudo asterisk -rx "pjsip show registrations"

# ダイヤルプランの確認
sudo asterisk -rx "dialplan show from-internal"
```

---

## ライセンス

MIT License — Copyright (c) 2026 TNC

全文は [LICENSE](LICENSE) を参照してください。
商用・改変・再配布は自由です。**無保証**です。

**MIT が適用されるのは、本リポジトリに含まれる自作のコードとドキュメントだけ
です。** 本ツールが利用する第三者のソフトウェア・音声データには、それぞれの
権利者のライセンスが適用されます。一覧と条件は
**[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)** にまとめています。

### 同梱している第三者のソフトウェア

| ソフトウェア | ライセンス | 用途 |
|---|---|---|
| [htmx](https://htmx.org/) 2.0.4 (`app/static/js/htmx.min.js`) | Zero-Clause BSD (0BSD) | 画面の非同期通信 |

音声ファイル・音響モデルの類は一切同梱していません。

### ⚠ 日本語音声プロンプトを使う場合の注意

「システム」画面からインストールできる日本語音声プロンプト
([takao-t/asterisk-sound-ja](https://github.com/takao-t/asterisk-sound-ja)) は、
**配布元にライセンスの記載がありません** (2026-10-02 時点)。

ライセンスが示されていない著作物は、既定では著作権者が全ての権利を留保して
いる状態です。本ツールはこの音声を同梱・再配布しておらず、インストール操作は
お使いのサーバーが配布元から直接ダウンロードするだけですが、
**業務で使用される場合は配布元へ利用可否をご確認ください。**

確認が取れない場合は、「音源」→「文章から音声を作る」の音声合成 (Open JTalk)
で必要なアナウンスを生成する方法もあります。

### 音声合成の声 (音響モデル) について

画面から追加できる「東北 f01」「メイ」は、いずれも**クリエイティブ・コモンズ
表示 (CC BY) 系**のライセンスです。生成した音声を外部へ配布・公開する場合は、
権利者名とライセンスの表示が必要です。詳細は
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。

### 外部プログラムとの関係

Asterisk 本体 (GPLv2)、FFmpeg、Ghostscript (AGPL v3)、Open JTalk などは
本ツールに同梱しておらず、OS のパッケージとして入っているものを別プロセスと
して呼び出すだけです (ライブラリとしてリンクしていません)。本ツールは
Asterisk の設定ファイルを生成し、AMI (ネットワーク経由) で `reload` を指示する
だけなので、GPL の派生物にはあたりません。

> これらのバイナリを同梱して配布すると、配布物全体にそれぞれのライセンス
> 条件が及ぶおそれがあります。配布形態を変える場合はご注意ください。

### 商標

「ひかり電話」「ひかり電話オフィスA」は NTT および NTT 東日本 / 西日本の、
「Asterisk」は Sangoma Technologies Corporation の商標または登録商標です。
本ツールは各社と提携・後援関係にはなく、接続対象を示す目的でのみ名称を
使用しています。

> 本リポジトリのライセンス関係の記述は、公開情報をもとに整理したもので
> 法的な助言ではありません。商用で配布・提供される場合は、最新の条件を
> ご確認のうえ、必要に応じて専門家にご相談ください。
