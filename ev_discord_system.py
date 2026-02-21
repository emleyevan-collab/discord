"""
╔══════════════════════════════════════════════════════════════════╗
║         🏆 COMPLETE +EV BETTING DISCORD SYSTEM 🏆                ║
╚══════════════════════════════════════════════════════════════════╝

COMMANDS (run each in a separate Colab cell):
  main()             -> Start the bot
  post_welcome()     -> Post welcome message to #general (once only)
  update_results()   -> Mark bets as win/loss/push
  force_summary()    -> Post daily summary to Discord right now
"""

import requests
import json
import time
import schedule
import os
from datetime import datetime, timezone

# ══════════════════════════════════════════════
#  YOUR SETTINGS — EDIT THESE
# ══════════════════════════════════════════════

ODDS_API_KEY            = "d13f32352cfc65707bba470ccfa2a020"
DISCORD_WEBHOOK_BETS    = "https://discord.com/api/webhooks/1474636223451496558/MVQLFJK7gFCJOFp_wStE7_bl0XEfE0F_jIkH6FCvq7hNxL4x7BwzrPr8ty4BPt8ilcV7"
DISCORD_WEBHOOK_SUMMARY = "https://discord.com/api/webhooks/1474637690560577671/xhmyDlNrl7Beny9wv8cOuoZea--Bgp7Xi1ugnP0uMocW6lVeM_Z6gm-59jCFBEWossrI"
DISCORD_WEBHOOK_GENERAL = "YOUR_GENERAL_CHANNEL_WEBHOOK"  # <- paste #general webhook here

BANKROLL          = 1000  # Your bankroll in dollars
MIN_EV_PCT        = 4.8   # Minimum EV% to post
KELLY_FRACTION    = 0.25  # Quarter Kelly bet sizing
SCAN_EVERY_MINS   = 10    # How often to scan
MAX_BETS_PER_SCAN = 5     # Max bets posted per scan
MIN_ODDS          = -300  # Ignore odds below this
MAX_ODDS          = 300   # Ignore odds above this
MAX_SPREAD_POINTS = 20    # Ignore spreads bigger than this

SPORTS = [
    "americanfootball_nfl",
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "americanfootball_ncaaf",
    "basketball_ncaab",
    "mma_mixed_martial_arts",
]

SHARP_BOOKS = ["pinnacle"]

SOFT_BOOKS = [
    "draftkings", "fanduel", "betmgm", "caesars",
    "pointsbetus", "williamhill_us", "betonlineag",
    "bovada", "mybookieag", "unibet_us", "espnbet",
]

BOOK_LABELS = {
    "draftkings":     "DraftKings",
    "fanduel":        "FanDuel",
    "betmgm":         "BetMGM",
    "caesars":        "Caesars",
    "pointsbetus":    "PointsBet",
    "williamhill_us": "William Hill",
    "betonlineag":    "BetOnline",
    "bovada":         "Bovada",
    "mybookieag":     "MyBookie",
    "unibet_us":      "Unibet",
    "espnbet":        "ESPN Bet",
}

SPORT_EMOJIS = {
    "americanfootball_nfl":   "🏈",
    "americanfootball_ncaaf": "🏈",
    "basketball_nba":         "🏀",
    "basketball_ncaab":       "🏀",
    "baseball_mlb":           "⚾",
    "icehockey_nhl":          "🏒",
    "mma_mixed_martial_arts": "🥊",
}

BASE_URL = "https://api.the-odds-api.com/v4"
TRACKER_FILE = "bet_tracker.json"

# ══════════════════════════════════════════════
#  MATH
# ══════════════════════════════════════════════

def american_to_decimal(o):
    return (o / 100) + 1 if o > 0 else (100 / abs(o)) + 1

def implied_prob(o):
    return 1 / american_to_decimal(o)

def remove_vig(o_a, o_b):
    p_a, p_b = implied_prob(o_a), implied_prob(o_b)
    t = p_a + p_b
    return p_a / t, p_b / t

def calc_ev(fair_prob, book_odds):
    return (fair_prob * american_to_decimal(book_odds) - 1) * 100

def kelly_bet(fair_prob, book_odds):
    b = american_to_decimal(book_odds) - 1
    q = 1 - fair_prob
    fk = max((b * fair_prob - q) / b, 0)
    return round(fk * KELLY_FRACTION * BANKROLL, 2)

# ══════════════════════════════════════════════
#  BET TRACKER
# ══════════════════════════════════════════════

def load_tracker():
    if os.path.exists(TRACKER_FILE):
        with open(TRACKER_FILE, "r") as f:
            return json.load(f)
    return {"bets": [], "posted_ids": [], "bankroll_start": BANKROLL}

def save_tracker(tracker):
    with open(TRACKER_FILE, "w") as f:
        json.dump(tracker, f, indent=2)

def log_bet(tracker, bet):
    bet_id = f"{bet['game']}_{bet['selection']}_{bet['book']}_{bet['book_odds']}"
    if bet_id not in tracker["posted_ids"]:
        tracker["posted_ids"].append(bet_id)
        tracker["bets"].append({
            "id":           bet_id,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "game":         bet["game"],
            "sport":        bet["sport"],
            "market":       bet["market"],
            "selection":    bet["selection"],
            "book":         bet["book"],
            "book_odds":    bet["book_odds"],
            "fair_prob_pct":bet["fair_prob_pct"],
            "ev_pct":       bet["ev_pct"],
            "bet_amount":   bet["bet_amount"],
            "result":       None,
            "profit":       None,
        })
        save_tracker(tracker)
        return True
    return False

def tracker_stats(tracker):
    bets    = tracker["bets"]
    settled = [b for b in bets if b["result"] is not None]
    wins    = sum(1 for b in settled if b["result"] == "win")
    losses  = sum(1 for b in settled if b["result"] == "loss")
    pushes  = sum(1 for b in settled if b["result"] == "push")
    total_profit = sum(b["profit"] or 0 for b in settled)
    total_staked = sum(b["bet_amount"] for b in settled if b["result"] in ("win","loss"))
    roi     = (total_profit / total_staked * 100) if total_staked > 0 else 0
    avg_ev  = sum(b["ev_pct"] for b in bets) / len(bets) if bets else 0
    return {
        "total_bets":       len(bets),
        "settled":          len(settled),
        "pending":          len(bets) - len(settled),
        "record":           f"{wins}W - {losses}L - {pushes}P",
        "total_profit":     round(total_profit, 2),
        "roi_pct":          round(roi, 2),
        "avg_ev_pct":       round(avg_ev, 2),
        "current_bankroll": round(BANKROLL + total_profit, 2),
    }

# ══════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════

def get_odds(sport):
    resp = requests.get(
        f"{BASE_URL}/sports/{sport}/odds",
        params={
            "apiKey":     ODDS_API_KEY,
            "regions":    "us,us2,eu",
            "markets":    "h2h,spreads,totals",
            "oddsFormat": "american",
            "bookmakers": ",".join(SHARP_BOOKS + SOFT_BOOKS),
        }
    )
    if resp.status_code != 200:
        print(f"  API error {sport}: {resp.status_code}")
        return []
    remaining = resp.headers.get("x-requests-remaining", "?")
    games = resp.json()
    print(f"  {sport}: {len(games)} games | {remaining} API calls left")
    return games

# ══════════════════════════════════════════════
#  FIND +EV BETS
# ══════════════════════════════════════════════

def find_ev_bets(games):
    ev_bets = []
    for game in games:
        title     = f"{game['away_team']} @ {game['home_team']}"
        sport_key = game.get("sport_key", "")
        try:
            t = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
            game_time = t.strftime("%a %b %d - %I:%M %p UTC")
        except:
            game_time = game.get("commence_time", "")

        book_data = {
            bm["key"]: {m["key"]: m["outcomes"] for m in bm.get("markets", [])}
            for bm in game.get("bookmakers", [])
        }

        sharp_key = next((s for s in SHARP_BOOKS if s in book_data and book_data[s]), None)
        if not sharp_key:
            continue

        for market_key, sharp_outcomes in book_data[sharp_key].items():
            sharp_prices = {o["name"]: o["price"] for o in sharp_outcomes}
            names = list(sharp_prices.keys())
            if len(names) != 2:
                continue

            name_a, name_b = names
            fair_a, fair_b = remove_vig(sharp_prices[name_a], sharp_prices[name_b])

            for soft_key in SOFT_BOOKS:
                if soft_key not in book_data or market_key not in book_data[soft_key]:
                    continue
                soft_prices = {
                    o["name"]: (o["price"], o.get("point"))
                    for o in book_data[soft_key][market_key]
                }

                for name, fair in [(name_a, fair_a), (name_b, fair_b)]:
                    if name not in soft_prices:
                        continue

                    book_odds, point = soft_prices[name]

                    # Filter: realistic odds range
                    if not (MIN_ODDS <= book_odds <= MAX_ODDS):
                        continue
                    # Filter: no absurd spreads
                    if market_key == "spreads" and point is not None:
                        if abs(point) > MAX_SPREAD_POINTS:
                            continue
                    # Filter: fair prob must be reasonable
                    if not (0.20 <= fair <= 0.80):
                        continue

                    ev = calc_ev(fair, book_odds)

                    # Filter: EV in believable range
                    if ev < MIN_EV_PCT or ev > 30:
                        continue

                    label = name
                    if point is not None:
                        label += f" {point:+g}" if market_key == "spreads" else f" {point}"

                    ev_bets.append({
                        "game":          title,
                        "sport":         sport_key,
                        "game_time":     game_time,
                        "market":        market_key,
                        "selection":     label,
                        "book":          soft_key,
                        "book_odds":     book_odds,
                        "fair_prob_pct": round(fair * 100, 1),
                        "book_impl_pct": round(implied_prob(book_odds) * 100, 1),
                        "ev_pct":        round(ev, 2),
                        "bet_amount":    kelly_bet(fair, book_odds),
                    })

    ev_bets.sort(key=lambda x: x["ev_pct"], reverse=True)
    return ev_bets

# ══════════════════════════════════════════════
#  DISCORD
# ══════════════════════════════════════════════

def ev_color(ev):
    if ev >= 10: return 0x00FF88
    if ev >= 7:  return 0x57F287
    if ev >= 5:  return 0xFEE75C
    return 0xEB459E

def post_bet_to_discord(bet):
    book_name   = BOOK_LABELS.get(bet["book"], bet["book"].title())
    sport_emoji = SPORT_EMOJIS.get(bet["sport"], "🎯")
    odds_str    = f"{bet['book_odds']:+d}"
    ev_bar      = "█" * min(int(bet["ev_pct"]), 10)

    embed = {
        "title": f"{sport_emoji}  +EV BET ALERT",
        "color": ev_color(bet["ev_pct"]),
        "fields": [
            {"name": "🏟️ Game",         "value": bet["game"],               "inline": False},
            {"name": "🎯 Bet",           "value": f"**{bet['selection']}**", "inline": False},
            {"name": "📖 Book",          "value": f"**{book_name}**",        "inline": True},
            {"name": "💵 Odds",          "value": f"**{odds_str}**",         "inline": True},
            {"name": "💰 Edge (EV%)",    "value": f"**+{bet['ev_pct']}%** {ev_bar}", "inline": True},
            {"name": "💼 Suggested Bet", "value": f"**${bet['bet_amount']}** (of $1000 bankroll)", "inline": False},
        ],
        "footer": {"text": "Sharp Reference: Pinnacle | Quarter Kelly"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    resp = requests.post(DISCORD_WEBHOOK_BETS, json={
        "username": "EV Alert 🚨",
        "embeds": [embed]
    })
    return resp.status_code in (200, 204)

def post_daily_summary_to_discord(tracker):
    stats = tracker_stats(tracker)
    profit_sign = "+" if stats["total_profit"] >= 0 else ""
    color = 0x00FF88 if stats["total_profit"] >= 0 else 0xFF4444

    embed = {
        "title": "📈  DAILY BETTING SUMMARY",
        "color": color,
        "fields": [
            {"name": "📋 Record",           "value": stats["record"],                              "inline": True},
            {"name": "💰 Total P&L",        "value": f"**{profit_sign}${stats['total_profit']}**", "inline": True},
            {"name": "📊 ROI",              "value": f"{stats['roi_pct']}%",                       "inline": True},
            {"name": "🏦 Current Bankroll", "value": f"${stats['current_bankroll']}",              "inline": True},
            {"name": "⚡ Avg EV%",          "value": f"+{stats['avg_ev_pct']}%",                   "inline": True},
            {"name": "🎯 Total Bets",       "value": f"{stats['total_bets']} ({stats['pending']} pending)", "inline": True},
        ],
        "footer": {"text": "Sharp Reference: Pinnacle | Results verified manually"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    resp = requests.post(DISCORD_WEBHOOK_SUMMARY, json={
        "username": "Daily P&L 📊",
        "embeds": [embed]
    })
    return resp.status_code in (200, 204)

# ══════════════════════════════════════════════
#  WELCOME MESSAGE
#  Run post_welcome() once when setting up
# ══════════════════════════════════════════════

def post_welcome():
    message = """👋 **Welcome to EV+ Betting — The Sharpest Betting Community**
------------------------

💡 **What is +EV Betting?**
Expected Value (EV) betting means finding bets where the odds a book is offering are BETTER than the true probability of the outcome. Over hundreds of bets, this turns into consistent profit.

We use Pinnacle — the sharpest sportsbook in the world — as our reference to find edges at soft books like DraftKings, FanDuel, BetMGM and more.

------------------------

📋 **How To Use This Server**

📌 **#ev-bets** — Live bet alerts posted automatically. When a bet drops, open the book listed and place it FAST. Odds move quickly.

📊 **#daily-summary** — Posted every morning. Shows our full record, profit/loss, and ROI.

------------------------

⚙️ **How To Follow The Bets**

1. Sign up at the books we use: DraftKings, FanDuel, BetMGM, Caesars, ESPN Bet
2. When a bet drops in #ev-bets, open that book immediately
3. Find the game and bet listed
4. Bet the suggested amount (or scale to your bankroll)

------------------------

📏 **Scaling To Your Bankroll**

Suggested sizes are based on a $1,000 bankroll:

- $500 bankroll   -> bet HALF the shown amount
- $1,000 bankroll -> bet exactly what is shown
- $2,000 bankroll -> bet DOUBLE the shown amount

------------------------

⚠️ **Important**

- EV betting is a long game. Losing days and weeks are normal. The edge shows over hundreds of bets.
- Place bets immediately when they drop — edges disappear fast.
- Open accounts at as many books as possible now. Books limit winning accounts over time.
- Never bet more than you are comfortable losing. This is not financial advice.

------------------------

🙋 Questions? Ask here. GL everyone 🤑"""

    resp = requests.post(DISCORD_WEBHOOK_GENERAL, json={
        "username": "EV Alert 🚨",
        "content": message
    })
    if resp.status_code in (200, 204):
        print("Welcome message posted to #general!")
    else:
        print(f"Failed: {resp.status_code} — make sure DISCORD_WEBHOOK_GENERAL is set at the top of the code.")

# ══════════════════════════════════════════════
#  EASY BET RESULT UPDATER
#  Run update_results() in a separate Colab cell
# ══════════════════════════════════════════════

def calc_profit(bet, result):
    stake = bet["bet_amount"]
    if result == "win":
        odds = bet["book_odds"]
        return round(stake * odds / 100, 2) if odds > 0 else round(stake * 100 / abs(odds), 2)
    elif result == "loss":
        return round(-stake, 2)
    return 0.0

def update_results():
    tracker = load_tracker()
    pending = [b for b in tracker["bets"] if b["result"] is None]

    if not pending:
        print("No pending bets to update!")
        return

    print(f"\n{'='*55}")
    print(f"  PENDING BETS ({len(pending)} to settle)")
    print(f"{'='*55}\n")

    for i, bet in enumerate(pending):
        book_name = BOOK_LABELS.get(bet["book"], bet["book"])
        print(f"  [{i+1}] {bet['game']}")
        print(f"       {bet['selection']} @ {book_name} {bet['book_odds']:+d} | Stake: ${bet['bet_amount']}")
        print()

    print("  Enter the number of the bet to settle, or 'done' to finish:\n")

    while True:
        choice = input("  Bet # (or 'done'): ").strip().lower()
        if choice == "done":
            break
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(pending):
                print("  Invalid number, try again.")
                continue
        except ValueError:
            print("  Type a number or 'done'.")
            continue

        bet = pending[idx]
        book_name = BOOK_LABELS.get(bet["book"], bet["book"])
        print(f"\n  Settling: {bet['selection']} @ {book_name} {bet['book_odds']:+d}")
        r = input("  Result (w=win, l=loss, p=push): ").strip().lower()

        if r in ("w", "win"):     result = "win"
        elif r in ("l", "loss"):  result = "loss"
        elif r in ("p", "push"):  result = "push"
        else:
            print("  Invalid — skipping.")
            continue

        profit = calc_profit(bet, result)
        for b in tracker["bets"]:
            if b["id"] == bet["id"]:
                b["result"] = result
                b["profit"] = profit
                break

        save_tracker(tracker)
        profit_str = f"+${profit}" if profit >= 0 else f"-${abs(profit)}"
        print(f"  Saved! {result.upper()} | {profit_str}\n")

    stats = tracker_stats(tracker)
    print(f"\n{'='*55}")
    print(f"  Record: {stats['record']}")
    print(f"  Total P&L: ${stats['total_profit']} | ROI: {stats['roi_pct']}%")
    print(f"{'='*55}")
    print("\n  Run force_summary() to post updated stats to Discord.")

def force_summary():
    tracker = load_tracker()
    success = post_daily_summary_to_discord(tracker)
    print("Summary posted!" if success else "Failed to post summary.")

# ══════════════════════════════════════════════
#  MAIN SCAN
# ══════════════════════════════════════════════

def run_scan():
    print(f"\n{'='*55}")
    print(f"  SCANNING at {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*55}")

    tracker  = load_tracker()
    all_bets = []

    for sport in SPORTS:
        games = get_odds(sport)
        if games:
            all_bets.extend(find_ev_bets(games))

    all_bets.sort(key=lambda x: x["ev_pct"], reverse=True)
    all_bets = all_bets[:MAX_BETS_PER_SCAN]

    new_bets = [bet for bet in all_bets if log_bet(tracker, bet)]

    print(f"\n  {len(new_bets)} new +EV bet(s) found")

    for bet in new_bets:
        success = post_bet_to_discord(bet)
        print(f"  {'Posted' if success else 'Failed'}: {bet['selection']} @ {bet['book']} EV:{bet['ev_pct']}%")
        time.sleep(1)

    save_tracker(tracker)
    print(f"\n  Next scan in {SCAN_EVERY_MINS} minutes...")

def run_daily_summary():
    tracker = load_tracker()
    post_daily_summary_to_discord(tracker)
    print("Daily summary posted.")

# ══════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════

def main():
    print("\n" + "="*55)
    print("  +EV DISCORD BETTING SYSTEM STARTED")
    print(f"  Min EV: {MIN_EV_PCT}% | Odds: {MIN_ODDS} to {MAX_ODDS}")
    print(f"  Scan every {SCAN_EVERY_MINS} mins | Max {MAX_BETS_PER_SCAN} bets per scan")
    print("="*55 + "\n")

    run_scan()

    schedule.every(SCAN_EVERY_MINS).minutes.do(run_scan)
    schedule.every().day.at("09:00").do(run_daily_summary)

    print("\n  Running — press Ctrl+C to stop\n")
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
