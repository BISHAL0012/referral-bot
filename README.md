# Telegram Referral & Points Bot — POWER v1

A ready-to-run Python Telegram bot for a referral/points business flow. **Upgraded: more powerful + colored buttons.**

## POWER v1 upgrades

- 🎨 **Colored inline buttons** (Bot API 9.4): 🟢 green Approve, 🔴 red Reject, 🔵 blue Join/Share buttons
- 🔥 **Daily streak**: consecutive daily bonus claims grow the reward (day 1 = base, day 2 = base+1, ...)
- 🎯 **Referral milestones**: 5 refs → +10 pts, 10 refs → +25, 25 refs → +60, 50 refs → +150, 100 refs → +400 (auto, once each)
- 🐛 **Bug fix**: referral counter now actually increments (leaderboard was stuck at 0 before)

> Note: main menu buttons (reply keyboard) can't be colored — Telegram only supports colors on inline buttons.

## Included features

- 💎 Points instead of money
- 👥 5 points per successful referral, credited immediately
- 💸 Minimum withdrawal: 120 points
- 👤 Withdrawal by Telegram username (`@username`), not bKash/Nagad
- ⏳ Withdrawal processing message: 12-72 hours
- 🔒 Withdrawal points are reserved/deducted immediately
- 👑 Admin Approve / Reject buttons
- ↩️ Reject = points returned
- ⏰ No approval within 72 hours = automatic refund + “Server Busy” message
- 📜 Point history
- 📜 Withdrawal history
- 🏆 Referral leaderboard
- 🎁 Daily bonus
- 🎡 Luck Wheel: one free spin per day; random prizes are 50, 20, 30, 20, or 10 points (20 appears twice in the prize list).
- 🏆 Every 5 days, one random user who spun during that 5-day period wins a 100-point bonus. The draw is processed when the bot receives a spin after the period ends.
- 📊 User and admin statistics
- 👤 User list
- 📢 Admin broadcast
- 📝 Admin action logs
- 🚫 Basic anti-fraud: one referral credit per new Telegram account and self-referral blocked
- 🔗 Personal referral link
- 🌈 Colorful inline group/referral buttons
- 🖼️ Referral banner image
- 📢 Telegram group button: https://t.me/realicamservice
- ⚙️ Admin settings commands

## Important

The bot does **not** automatically send money. A withdrawal is a request identified by the user's Telegram username. The admin must approve it and handle the actual payout separately.

## Run in Termux

```bash
cd ~/storage/downloads
rm -rf referral_bot
unzip -q telegram_referral_earn_bot_points.zip -d referral_bot
cd referral_bot
pip install -r requirements.txt
export BOT_TOKEN='YOUR_NEW_BOT_TOKEN'
export ADMIN_IDS='YOUR_TELEGRAM_NUMERIC_ID'
python bot.py
```

Keep your BotFather token private. If a token was posted in chat/screenshots, revoke it in BotFather and use a new token.

## Main admin commands

```text
/admin
/setref 5
/setbonus 1
/setmin 120
/setcurrency POINTS
/setgroup https://t.me/realicamservice
/setdelivery Withdrawals are processed within 12-72 hours.
/setwelcome Invite friends and earn points.
/setsupport Contact admin for support.
```


## Referral group verification update

- Referral reward is 5 points.
- A referral is completed only after the referred user joins `https://t.me/realicamservice`.
- The user must press `গ্রুপে Join করেছি — Check` after joining.
- The bot checks membership through Telegram `getChatMember`.
- For this verification to work reliably, add the bot as an administrator of the group/channel with permission to see members.
- The referral screen has Share, Copy Link, and a highlighted Join Group button.
- The referral screen does not use a banner image.
- Telegram Bot API does not let bots choose arbitrary button background colors or font size/weight; emoji and clean short text are used for visual emphasis.


### Luck Wheel 5-day bonus draw
The bot checks completed 5-day Luck Wheel periods automatically at startup and approximately every 30 seconds while running. The draw no longer depends on a user pressing Luck Wheel. If the bot was offline when a period ended, it catches up after restart. The bot must be running and have internet access to send the winner notification at that time; while offline, the draw is processed after it comes back online.
