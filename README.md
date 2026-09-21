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

---

## 動作環境

| 項目 | 内容 |
|------|------|
| OS | Ubuntu Server 22.04 / 24.04 (Raspberry Pi の Ubuntu も可) |
| Asterisk | 22 系 (ソースビルド) |
| Python | 3.11 以上 |
| 必須コマンド | `ffmpeg` (音源変換)、`tiff2pdf`・`gs` (FAX を使う場合) |

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

配布 zip を使う場合は、zip と `install_all.sh` を同じディレクトリに置いて
`sudo bash install_all.sh` を実行するか、zip の場所を明示します。

```bash
sudo APP_ZIP=/path/to/asterisk-pbx-web.zip bash install_all.sh
```

実行される内容:

1. パッケージリストの更新
2. 日本語環境 (ロケール `ja_JP.UTF-8` / タイムゾーン `Asia/Tokyo` / vim の日本語対応)
3. 基本ツール (`unzip` `curl` `git` `build-essential` `samba` 等)
4. Python 3 環境
5. **Asterisk 22 をソースからビルド** (`--with-pjproject-bundled`、`res_fax_spandsp` 有効)
6. 本ツールを `/var/www/asterisk-pbx-web` へ配置し、`.env` を自動生成
   (`SECRET_KEY` / `AMI_SECRET` / **`ADMIN_PASSWORD`** をランダム生成)
7. Python 依存パッケージを venv に導入
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
```

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

本ツールが Asterisk の設定ファイルと音源を書き込めるようにします。

```bash
# 本ツールを動かすユーザーを asterisk グループに入れる
sudo usermod -aG asterisk $(whoami)

# 設定ディレクトリ・音源ディレクトリをグループ書き込み可に
sudo chgrp -R asterisk /etc/asterisk /var/lib/asterisk/sounds
sudo chmod -R g+w /etc/asterisk /var/lib/asterisk/sounds

# FAX・留守番電話を使う場合
sudo mkdir -p /var/spool/asterisk/fax /var/lib/asterisk-pbx-web/fax
sudo chgrp -R asterisk /var/spool/asterisk /var/lib/asterisk-pbx-web
sudo chmod -R g+w /var/spool/asterisk /var/lib/asterisk-pbx-web
```

グループの変更を反映するには、一度ログアウトして入り直してください。

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
features.conf      通話中の機能キー
manager.conf       AMI
rtp.conf           RTP ポート範囲
cdr.conf           通話記録
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

### 迷惑電話ブロック

番号またはパターンを登録して、着信を切断するか留守番電話に回します。

```
0399999999        この番号だけ
_0120XXXXXX       フリーダイヤル全般
_0[789]0XXXXXXXX  携帯電話全般
```

### 音源

案内音声を用意する方法は 2 つあります。

- **MP3 等をアップロード** — ffmpeg が Asterisk 用の WAV に自動変換します
- **文章から音声を作る** — Open JTalk による日本語音声合成。
  声の種類・速度・高さを選べます

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

### 同梱している第三者のソフトウェア

| ソフトウェア | ライセンス | 用途 |
|---|---|---|
| [htmx](https://htmx.org/) 2.0.4 (`app/static/js/htmx.min.js`) | MIT (Zero-Clause BSD) | 画面の非同期通信 |

Asterisk 本体 (GPLv2) は本ツールに同梱しておらず、別プロセスとして動作します。
本ツールは Asterisk の設定ファイルを生成し、AMI (ネットワーク経由) で
`reload` を指示するだけなので、GPL の派生物にはあたりません。
