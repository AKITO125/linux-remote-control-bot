# Discord Linux Remote Control Bot

Discord から Linux PC をリモートコントロールするためのBotです。  
コマンド実行・ファイル管理・スクリーンショット・電源操作・デュアルブートなど多機能を備えています。

---

## 機能一覧

| カテゴリ | 主な機能 |
|---|---|
| シェル実行 | リアルタイム出力・バックグラウンドジョブ・stdin送信 |
| ターミナルセッション | 複数の永続セッション（cd / export が引き継がれる） |
| ファイル管理 | 一覧・表示・削除・ダウンロード・アップロード・検索 |
| システム情報 | CPU/メモリ/ディスク/GPU/温度/プロセス一覧 |
| ネットワーク | IP情報・ping・curl |
| 電源管理 | シャットダウン・再起動・スリープ・デュアルブート切替 |
| Docker | コンテナ一覧・起動/停止/再起動・ログ |
| ログ監視 | tail・リアルタイム監視・inotify変更通知・アラート |
| 画面操作 | スクリーンショット・自動定期送信・Webカメラ |
| ウィンドウ管理 | 一覧・フォーカス・個別クローズ（wmctrl） |
| 入力操作 | マウスクリック・テキスト入力・キー送信（xdotool） |
| クリップボード | 取得・設定 |
| スラッシュコマンド | `/run` `/sysinfo` `/screenshot` `/ps` `/log` |

---

## 必要環境

- Python 3.11 以上
- Linux（Ubuntu 22.04 / Debian 12 推奨）
- Discord Bot Token

---

## セットアップ

### 1. Discord Bot を作成する

1. [Discord Developer Portal](https://discord.com/developers/applications) を開く
2. **New Application** → 名前を入力 → **Create**
3. 左メニューの **Bot** をクリック
4. **Add Bot** → **Yes, do it!**
5. **Token** の **Reset Token** をクリックしてトークンをコピー（後で使用）
6. 同ページの **Privileged Gateway Intents** で以下を **ON** にする
   - **Message Content Intent**
7. 左メニューの **OAuth2 → URL Generator** をクリック
   - **SCOPES**: `bot`, `applications.commands` にチェック
   - **BOT PERMISSIONS**: `Send Messages`, `Read Message History`, `Attach Files`, `Embed Links`, `Manage Messages` にチェック
8. 生成されたURLをブラウザで開き、Botをサーバーに招待

> **ユーザーIDの確認方法**  
> Discord の設定 → 詳細設定 → 開発者モードをON  
> 自分のアイコンを右クリック → **IDをコピー**

---

### 2. リポジトリをクローン

```bash
git clone https://github.com/<あなたのユーザー名>/discord-linux-bot.git
cd discord-linux-bot
```

---

### 3. 依存関係をインストール

```bash
pip install -r requirements.txt
```

---

### 4. .env を設定

```bash
cp .env.example .env
nano .env   # または好きなエディタで編集
```

```env
# Botトークン（Developer Portal からコピー）
DISCORD_TOKEN=your_bot_token_here

# 操作を許可するDiscordユーザーIDをカンマ区切りで指定（空欄=全員許可）
ALLOWED_USER_IDS=123456789012345678

# コマンドを受け付けるチャンネルIDをカンマ区切りで指定（空欄=全チャンネル許可）
ALLOWED_CHANNEL_IDS=

# コマンドタイムアウト（秒）
COMMAND_TIMEOUT=30

# コマンドの作業ディレクトリ
WORK_DIR=/home/youruser

# ブロックするコマンド（カンマ区切り）
BLOCKED_COMMANDS=rm -rf /,mkfs,dd if=/dev/zero of=,:(){ :|:& };:,sudo rm,sudo dd

# Discord認証パスワード（shutdown/reboot/kill 等の危険操作時にDMで要求）
BOT_AUTH_PASSWORD=your_discord_auth_password_here

# Linux sudo パスワード（設定しない場合は sudoers に NOPASSWD が必要）
SUDO_PASSWORD=your_linux_sudo_password_here

# 認証失敗の上限回数（超えるとロック）
AUTH_MAX_FAILURES=3

# 認証ロック時間（分）
AUTH_COOLDOWN_MINUTES=5
```

---

### 5. Bot を起動

```bash
python bot.py
```

起動に成功すると以下のようなログが出ます：

```
起動: MyBot#1234  作業Dir: /home/youruser
```

---

### 6. スラッシュコマンドを同期する（初回のみ）

Botを起動後、Discord でコマンドを送信します：

```
!sync
```

`5 個のスラッシュコマンドを同期しました。` のように表示されたら成功です。  
反映まで最大1時間かかる場合があります（通常は数分）。

---

## Discord サーバー設定

### コマンド専用チャンネルを作る（推奨）

1. Discordでチャンネルを作成（例: `#pc-control`）
2. そのチャンネルのIDを取得（チャンネル名を右クリック → **IDをコピー**）
3. `.env` の `ALLOWED_CHANNEL_IDS` に設定する
4. そのチャンネル以外では Bot がコマンドを無視するようになる

### コマンド一覧をピン留めする

1. Botが起動した状態で `#pc-control` チャンネルで `!cmds` と送信
2. Botがコマンド一覧を返信する
3. その返信メッセージを右クリック → **ピン留め**
4. チャンネルのピン留めからいつでも確認できるようになる

### スラッシュコマンド（`/` コマンド）の使い方

スラッシュコマンドはDiscordのUI上で補完が効く便利なコマンドです。  
`!sync` を実行後、チャンネルで `/` を入力すると候補が出ます。

| スラッシュコマンド | 説明 |
|---|---|
| `/run` | シェルコマンドを実行 |
| `/sysinfo` | CPU・メモリ・ディスク情報 |
| `/screenshot` | スクリーンショットを送信 |
| `/ps` | プロセス一覧 |
| `/log` | ログファイルの末尾を表示 |

---

## コマンドリファレンス

### シェル実行

| コマンド | 説明 |
|---|---|
| `!run <cmd>` | コマンドを実行（リアルタイムストリーミング） |
| `!r <cmd>` | `!run` のエイリアス |
| `!bg <cmd>` | バックグラウンドで実行（完了時に通知） |
| `!jobs` | 実行中のバックグラウンドジョブ一覧 |
| `!stop <id>` | バックグラウンドジョブを停止 |
| `!input <id> <text>` | ジョブに標準入力を送信 |

```
!run df -h
!bg python train.py
!stop 1
!input 1 yes
```

---

### ターミナルセッション

cd・export などの状態が引き継がれる永続シェルセッションです。

| コマンド | 説明 |
|---|---|
| `!session start [名前]` | セッションを開始（デフォルト名: `main`） |
| `!session end [名前]` | セッションを終了 |
| `!session list` | 起動中のセッション一覧 |
| `!s <cmd>` | `main` セッションでコマンドを実行 |
| `!s <名前> <cmd>` | 指定セッションでコマンドを実行 |

```
!session start work
!s work cd /var/log
!s work ls -la          # /var/log の内容が表示される
!session end work
```

---

### ファイル管理

| コマンド | 説明 |
|---|---|
| `!ls [パス]` | ディレクトリ内容を表示 |
| `!cat <パス>` | ファイル内容を表示 |
| `!download <パス>` | ファイルをDiscordに送信（画像も可） |
| `!upload [保存先]` | 添付ファイルをLinuxに保存 |
| `!rm <パス>` | ファイル/ディレクトリを削除（確認あり） |
| `!find <パターン> [パス]` | ファイル名で検索 |
| `!grep <パターン> [パス]` | ファイル内容で検索 |

```
!ls /var/log
!cat /etc/os-release
!download /home/user/photo.jpg
!find *.log /var/log
```

---

### システム情報

| コマンド | 説明 |
|---|---|
| `!sysinfo` / `!sys` | CPU・メモリ・ディスク・稼働時間 |
| `!meminfo` | メモリ詳細（キャッシュ・Swap・Top5プロセス） |
| `!temp` | CPU/GPU温度 |
| `!gpu` | GPU使用率・VRAM・温度（NVIDIA/AMD） |
| `!ps [件数]` | プロセス一覧（CPU使用率順） |
| `!kill <PID>` | プロセスをPIDで終了（認証あり） |
| `!killapp <名前>` | アプリ名でプロセスを終了（認証あり） |
| `!service <act> <name>` | systemdサービスを操作 |

```
!ps 20
!kill 1234
!killapp firefox
!service restart nginx
!service status nginx
```

---

### ネットワーク

| コマンド | 説明 |
|---|---|
| `!netinfo` | IP・インターフェース・ルート情報 |
| `!ping <host> [回数]` | 疎通確認 |
| `!curl <URL> [オプション]` | HTTPリクエストを送信 |

```
!ping google.com 4
!curl http://localhost:8080/api/status
```

---

### 電源管理

| コマンド | 説明 |
|---|---|
| `!shutdown [分]` | シャットダウン（0=即時、N=N分後） |
| `!reboot` | 通常再起動 |
| `!reboot <OS名>` | 指定OSで再起動（デュアルブート） |
| `!sleep` | スリープ（サスペンド） |
| `!bootentries` | GRUBの起動エントリ一覧を表示 |
| `!bootinto <エントリ>` | 次回のみ指定OSで起動してreboot |

```
!reboot windows         # Windowsで再起動
!reboot ubuntu          # Ubuntuで再起動
!bootentries            # エントリ番号を確認
!bootinto 0             # エントリ0で再起動
!shutdown 5             # 5分後にシャットダウン
```

---

### Docker

| コマンド | 説明 |
|---|---|
| `!docker ps` | コンテナ一覧 |
| `!docker start <名前>` | コンテナを起動 |
| `!docker stop <名前>` | コンテナを停止 |
| `!docker restart <名前>` | コンテナを再起動 |
| `!docker logs <名前>` | ログを表示（末尾50行） |

---

### ログ・監視

| コマンド | 説明 |
|---|---|
| `!log <パス> [行数]` | ログの末尾N行を送信 |
| `!watch <パス>` | ログをリアルタイム監視 |
| `!unwatch <パス>` | 監視を停止 |
| `!watches` | 監視中のファイル一覧 |
| `!iwatch <パス>` | ファイル変更をinotifyで通知 |
| `!iunwatch <パス>` | inotify監視を停止 |
| `!alert <cpu\|mem\|disk> <%>` | しきい値アラートを設定 |
| `!alerts` | アラート設定一覧 |
| `!unalert <cpu\|mem\|disk>` | アラートを解除 |

```
!log /var/log/syslog 100
!watch /var/log/nginx/access.log
!alert cpu 90
!iwatch /home/user/downloads
```

---

### 画面・カメラ

| コマンド | 説明 |
|---|---|
| `!ss` / `!screenshot` | 画面全体のスクリーンショット |
| `!ss window` | フォーカス中のウィンドウ |
| `!ss <アプリ名>` | アプリ名でウィンドウを指定 |
| `!autoshot [秒]` | 定期的にスクリーンショットを自動送信 |
| `!stopauto` | 自動送信を停止 |
| `!camera [デバイス]` | Webカメラで撮影して送信 |

```
!ss
!ss window
!ss firefox
!autoshot 60
!camera /dev/video0
```

---

### ウィンドウ管理

wmctrl が必要です（`sudo apt install wmctrl`）。

| コマンド | 説明 |
|---|---|
| `!windows` / `!wins` | 開いているウィンドウ一覧（ID・タイトル） |
| `!focus <タイトル\|ID>` | ウィンドウを最前面に表示 |
| `!wclose <タイトル\|ID>` | 特定のウィンドウだけを閉じる |

```
!windows
!focus Terminal
!focus 0x04000003
!wclose GitHub — Firefox
!wclose 0x04000001
```

---

### クリップボード・入力操作

xdotool が必要です（`sudo apt install xdotool`）。

| コマンド | 説明 |
|---|---|
| `!clip get` | クリップボードの内容をDiscordに送信 |
| `!clip set <テキスト>` | クリップボードにテキストをセット |
| `!click <x> <y> [btn]` | マウスを移動してクリック（btn: 1=左 2=中 3=右） |
| `!type <テキスト>` | テキストをキーボード入力 |
| `!key <キー>` | キーを送信 |

```
!click 960 540
!click 960 540 3
!type Hello World
!key ctrl+c
!key Return
!key alt+F4
```

---

### メタコマンド

| コマンド | 説明 |
|---|---|
| `!cmds` | コマンド一覧をDiscordに表示 |
| `!sync` | スラッシュコマンドをDiscordに同期（初回のみ実行） |

---

## セキュリティ

### 認証の仕組み

`!reboot` `!shutdown` `!sleep` `!kill` `!killapp` `!service start/stop/restart` などの危険な操作を実行する際、**DM経由でパスワードが要求されます**。

1. コマンドを送信するとBotがDMを送ってくる
2. DMに `.env` の `BOT_AUTH_PASSWORD` を送信する
3. パスワードが一致すれば実行される
4. 送信したDMメッセージは自動的に削除される
5. 規定回数（デフォルト3回）失敗すると一定時間ロックされる

### ALLOWED_USER_IDS

`.env` に自分のDiscordユーザーIDのみを設定することで、他のユーザーがコマンドを実行できないようになります。

### BLOCKED_COMMANDS

`.env` で危険なコマンドをブロックできます。  
先頭一致でチェックされます。

---

## オプション依存パッケージ

基本機能以外に以下のツールが必要な場合があります：

```bash
# スクリーンショット（X11）
sudo apt install scrot

# スクリーンショット（Wayland）
sudo apt install grim

# ウィンドウ管理・マウス/キーボード操作
sudo apt install wmctrl xdotool

# Webカメラ
sudo apt install fswebcam
# または
sudo apt install ffmpeg

# ファイル変更監視
sudo apt install inotify-tools

# CPU温度センサー
sudo apt install lm-sensors

# X11 クリップボード
sudo apt install xclip
```

---

## sudo の設定

`shutdown` `reboot` `grub-reboot` などは sudo が必要です。

**方法1: `.env` に SUDO_PASSWORD を設定**

```env
SUDO_PASSWORD=your_linux_sudo_password
```

**方法2: sudoers に NOPASSWD を設定（推奨）**

```bash
sudo visudo
```

末尾に追加：

```
youruser ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/reboot, /usr/sbin/grub-reboot, /bin/systemctl
```

---

## systemd でサービスとして登録（常時起動）

```bash
sudo nano /etc/systemd/system/discord-bot.service
```

```ini
[Unit]
Description=Discord Linux Remote Control Bot
After=network.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/discord-linux-bot
ExecStart=/usr/bin/python3 /home/youruser/discord-linux-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable discord-bot
sudo systemctl start discord-bot
sudo systemctl status discord-bot
```

---

## ライセンス

MIT License
