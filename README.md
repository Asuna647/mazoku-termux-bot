# mazoku-termux-bot

A lightweight, headless [Playwright for Python](https://playwright.dev/python/docs/intro) runner for [Mazoku Legacy](https://top.gg/bot/1242388858897956906) on Discord. It handles 30-minute manual summons (`/summon`), passive channel auto-spawns, event drops, and daily pack sweeps (`/daily`, `/packs`, `/open`) while evaluating the 3 spawned cards to pick the highest-value option automatically.

Instead of leaving a desktop PC drawing 150W+ around the clock—or burning through the 2,000-minute monthly cap on private GitHub Actions runners in three days—this project is built to run 24/7 inside an Ubuntu `proot-distro` container on a spare Android phone using [Termux](https://termux.dev/en/). A phone plugged into a wall charger on home Wi-Fi draws \~3W, has built-in battery backup if the power flickers, and costs \$0/month without requiring credit card verification on cloud providers.

> **Disclaimer:** Automating a personal user account violates the [Discord Automated User Accounts (Self-Bots) Policy](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots). If you run this, use it in a private server where only you and the bot are present, keep the default randomized human delays intact, and understand that you use it at your own risk.

## What It Does

* **31-Minute Manual Summon Cycle:** Runs `/summon` every 31 minutes (`1860s` base + `15–75s` random jitter so the interval never looks robotic). If you manually summoned earlier and the bot is still on cooldown, it parses the remaining minutes/seconds from the response and reschedules automatically.

* **Passive Auto-Summon & Event Watcher:** Polls the active channel every `6–10` seconds between manual summons. Whenever an automatic channel spawn or event summon drops with interactive claim buttons, it evaluates the cards and clicks the best option within the 2-minute expiration window.

* **Daily & Free Pack Opener:** Every \~6 hours (plus random jitter), claims `/daily`, queries `/packs` to see what is actually sitting in your inventory (`daily`, `event`, `starter`, `standard`, `premium`), and opens each available pack via `/open`.

* **Anti-Sniping Chat Heartbeat:** [Mazoku Legacy](https://top.gg/bot/1242388858897956906) blocks players from claiming channel spawns if they haven't chatted recently. The runner posts a casual message every 35–55 minutes to keep your activity flag valid.

* **3-Slice Card Evaluation (Visual HSV + OCR):** Crops the composite 3-card summon image into individual Left (`Card #1`), Middle (`Card #2`), and Right (`Card #3`) slices. It combines [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) (for reading `C`, `R`, `SR`, `SSR`, `UR` tags and `v1–v4000` print numbers) with HSV saturation/brightness frame analysis so it still picks higher-tier cards even if OCR misses stylized text.

* **Webhook Logging:** Sends claim summaries and session warnings to a private channel via [Discord Incoming Webhooks](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks).

## How Card Scoring Works

When `/summon` or a channel drop spawns 3 cards (4 if your account has Premium from the [Mazoku Legacy Store](https://mazoku.cc/store)), the bot scores each card before clicking its button:

| Tier | Summon Rate | Daily Pack Rate | Print Ceiling | Burn Value (Std / Prem) | Post-Claim Cooldown | 
| ----- | ----- | ----- | ----- | ----- | ----- | 
| **Common (C)** | 88.00% | 84.89% | `v4000` | 25 / 50 Bloodstones | 120s (2m) | 
| **Rare (R)** | 10.50% | 14.70% | `v2000` | 50 / 100 Bloodstones | 300s (5m) | 
| **Super Rare (SR)** | 1.20% | 0.40% | `v500` | 1,500 / 3,000 Bloodstones | 900s (15m) | 
| **Special Super Rare (SSR)** ([Catalog](https://mazoku.cc/cards)) | 0.25% | 0.01% | `v150` | 10,000 / 20,000 Bloodstones | 3,600s (60m) | 
| **Ultra Rare (UR)** ([Catalog](https://mazoku.cc/cards)) | Event | Event | `v50` | 25,000 / 50,000 Bloodstones | 3,600s (60m) | 

1. **Rarity & Print Scarcity:** Low print numbers (`v1` through `v50`) get a steep multiplier (`(max_ver - ver) / max_ver` plus flat bonuses for `v1` and `v2–v10`), while base scores ensure an `SR` or `SSR` is never passed over for a Common.

2. **Visual Frame Fallback:** In [Mazoku Legacy](https://mazoku.cc/cards), Common cards use a darkened frame shader, whereas Rare, SR, and SSR cards render brighter ribbons, full-art backgrounds, and glowing borders in the top and bottom 20% of the card. Slicing the composite image into thirds and measuring HSV saturation/brightness across each slice gives a reliable tie-breaker when text isn't exposed in the DOM.

3. **Rarity Cooldown Sync:** Claiming a card triggers a tier-specific claim cooldown (`120s` for C up to `3,600s` for SSR). The bot records the claimed tier in SQLite (`mazoku_state.db`) and holds off on firing `/summon` until your claim cooldown clears.

## Repository Layout

```
mazoku-termux-bot/
├── mazoku_bot.py                 # Main Playwright automation script (Desktop + Termux ARM64)
├── requirements.txt              # Python dependencies
├── .env.example                  # Template for channel URL, webhook URL, and wishlist
├── .gitignore                    # Blocks discord_state.json, .env, *.db, and logs from git
└── scripts/
    └── setup_termux_ubuntu.sh    # One-shot setup script for inside Termux Ubuntu

```

## Part 1: Export Your Discord Session on Your PC (One-Time Setup)

Logging into Discord directly from a headless browser often triggers a captcha or 2FA prompt. The cleanest workaround is to log in once on your normal PC using a visible browser window, save the session state to `discord_state.json`, and transfer that file to your phone.

1. Clone this repository on your PC and install the dependencies:

   ```
   git clone https://github.com/Asuna647/mazoku-termux-bot.git
   cd mazoku-termux-bot
   pip install -r requirements.txt
   python -m playwright install chromium
   
   ```

   *(Note for Windows users: if PowerShell says `playwright is not recognized` because your Python `Scripts` folder isn't on `PATH`, always prefix commands with `python -m`, e.g., `python -m pip install ...` and `python -m playwright install chromium`.)*

2. Run the script with `--login`:

   ```
   python mazoku_bot.py --login
   
   ```

3. A Chromium window will open at `https://discord.com/login`. Log in (scanning the QR code with the Discord mobile app is fastest), wait until your server list loads, and press **Enter** in your terminal. This writes `discord_state.json` to your project folder.

4. Copy `.env.example` to `.env` and fill in your private channel URL (`https://discord.com/channels/YOUR_SERVER_ID/YOUR_CHANNEL_ID`) and optional [Discord Webhook URL](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks).

## Part 2: Turning an Old Android Phone into a 24/7 Termux Server

### 1. Install Termux & Disable Android Battery Killer

1. Download and install the APK from [Termux GitHub Releases](https://github.com/termux/termux-app/releases) (`arm64-v8a` or `universal`). Do **not** install the Google Play Store build; it is deprecated and its package mirrors are broken.

2. On the phone, open **Settings → Apps → Termux → Battery** and set it to **Unrestricted** / **Don't optimize**.

### 2. Install the Ubuntu Container

Open [Termux](https://termux.dev/en/) on the phone and run:

```
pkg update -y && pkg install -y proot-distro tmux curl
termux-wake-lock
proot-distro install ubuntu
proot-distro login ubuntu

```

Your prompt will change to `root@localhost:~#`.

### 3. Install Python, Playwright & Headless Chromium (ARM64)

Inside `root@localhost:~#`, run the setup commands below (or run `bash scripts/setup_termux_ubuntu.sh`):

```
apt update && apt install -y python3-pip python3-venv tesseract-ocr curl tmux
python3 -m venv ~/mazoku-env
source ~/mazoku-env/bin/activate
pip install playwright pillow pytesseract
playwright install --with-deps chromium
mkdir -p ~/mazoku-bot && cd ~/mazoku-bot

```

This downloads Playwright's `linux-arm64` `chromium_headless_shell` binary and required shared libraries (`libnss3`, `libgbm1`, etc.).

### 4. Beam Your Script & Session File Over Home Wi-Fi

Rather than copy-pasting a 150 KB `discord_state.json` file onto a phone screen, serve the folder temporarily over your local Wi-Fi from your PC:

1. **On your PC** (inside the folder containing `mazoku_bot.py`, `.env`, and `discord_state.json`):

   ```
   # Windows PowerShell: check your local Wi-Fi IPv4 address (e.g. 192.168.1.XX)
   ipconfig | findstr /i "IPv4"
   python -m http.server 8080
   
   ```

   *(On Linux/macOS, use `ip a` or `ifconfig` and `python3 -m http.server 8080`.)*

2. **On your phone** (at `(mazoku-env) root@localhost:~/mazoku-bot#`), replace `192.168.1.XX` with your PC's local IPv4 address:

   ```
   cd ~/mazoku-bot
   curl -f -O http://192.168.1.XX:8080/mazoku_bot.py
   curl -f -O http://192.168.1.XX:8080/discord_state.json
   curl -f -O http://192.168.1.XX:8080/.env
   ls -lh
   
   ```

   Always include `-f` on `curl` and check `ls -lh` afterward. Without `-f`, if you started `http.server` in the wrong folder on your PC, `curl` will silently save Python's 460-byte `404 File Not Found` HTML page into your file. Confirm `mazoku_bot.py` is \~25 KB and `discord_state.json` is 100 KB+.

3. Stop the file server on your PC (`Ctrl + C`). You can now shut down your PC completely.

### 5. Run 24/7 in the Background (Surviving PC Terminal Disconnects)

If you connected to your phone from a PC terminal (via SSH or ADB) and ran `python3 mazoku_bot.py --headless` directly in the foreground, closing your PC terminal window sends a `SIGHUP` (hangup) signal that immediately kills the bot.

To detach the bot completely from your terminal session—and skip typing `source ~/mazoku-env/bin/activate`—call the virtual environment's Python binary directly with `nohup` and `disown`:

```
cd ~/mazoku-bot
nohup /root/mazoku-env/bin/python3 -u mazoku_bot.py --headless > bot.log 2>&1 &
disown

```

Once detached, you can verify it is running in three ways:

* **Watch the live log stream:**

  ```
  tail -f ~/mazoku-bot/bot.log
  
  ```

  *(Press `Ctrl + C` anytime to exit the log view without stopping the background bot.)*

* **Check the background process PID:**

  ```
  ps aux | grep mazoku_bot
  
  ```

* **Check your Discord channel:** Confirm the `"Mazoku Runner Online"` embed appears via your [Discord Webhook](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks).

Leave the phone plugged into power with `(wake lock held)` showing in the Android notification drawer, and you can safely close your terminal and shut down your PC.

## Hard-Earned Debugging Notes (Why Specific Things Are Coded This Way)

If you modify `mazoku_bot.py`, keep these Discord Web and Termux quirks in mind:

* **Why the phone bot dies if you close your PC terminal without `nohup` + `disown`:** Any foreground process attached to an SSH or `adb shell` pseudo-terminal receives `SIGHUP` the moment the controlling terminal closes. Launching `/root/mazoku-env/bin/python3 -u` under `nohup ... > bot.log 2>&1 &` followed by `disown` detaches the process from the shell job table so closing the PC terminal leaves the phone running uninterrupted. Calling `/root/mazoku-env/bin/python3` by its full path also avoids `No such file or directory` typos on `source ~/mazoku-env/bin/activate`.

* **Why you can't just `keyboard.type("/summon")` and press `Enter`:** Discord's message box uses Slate.js. If you type `/summon` and press `Enter` immediately, Discord sends the literal string `"/summon"` as a regular chat message, which the bot ignores. `send_slash_command()` types `/` first, types the command name, waits for `div[id^="autocomplete-"] div[role="option"]` to mount, and clicks the autocomplete entry so Discord converts it into an actual Application Command pill.

* **Why `msg.locator("button")` clicks the wrong thing:** Every message row (`li[id^="chat-messages-"]`) in Discord Web contains hidden hover-toolbar buttons (`Add Reaction`, `Reply`, `Edit`, `More`). Querying all `<button>` elements inside a message will click "Add Reaction" instead of Card #1. `find_card_buttons()` scopes strictly to `[id^="message-accessories-"] button` and filters out reaction/toolbar `aria-label` values.

* **Why timers reset on launch:** Early iterations persisted the 24-hour `/daily` timer in SQLite before verifying the command succeeded. If the first run failed while testing, restarting the script left it sleeping for 24 hours. The bot now clears stale timers on startup (configurable via `--keep-timers`) so you immediately see `/summon` and `/daily` execute when you launch it.

* **Why `--no-sandbox` and `--disable-gpu` are mandatory on Termux:** Inside `proot-distro`, `root` is emulated via `proot` without real kernel namespace privileges. Chromium's setuid/namespace sandbox and GPU process will crash on launch unless `--no-sandbox`, `--disable-setuid-sandbox`, `--disable-dev-shm-usage`, and `--disable-gpu` are passed.

## Pushing Your Copy to GitHub Safely

Before pushing to GitHub, verify that `.gitignore` is present so you never commit your live Discord cookies or webhook token:

```
git init
git status

```

Make sure `discord_state.json`, `.env`, and `mazoku_state.db` **do not** appear under "Changes to be committed" (they should be ignored by `.gitignore`). Then commit and push:

```
git add README.md mazoku_bot.py requirements.txt .env.example .gitignore scripts/
git commit -m "Initial commit: Termux 24/7 Playwright runner for Mazoku Legacy"
git branch -M main
git remote add origin https://github.com/Asuna647/mazoku-termux-bot.git
git push -u origin main

```