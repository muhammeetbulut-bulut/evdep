import requests
import time
import math
import os
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8688455737:AAENmxT4dn8LoExD-XRcS6J3noLIscBMeQo")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8480843841"))
API_KEY = os.environ.get("API_KEY", "0c0c1ad20573b309924dd3d7b1bc3e62")
API_URL = "https://v3.football.api-sports.io"

HOME, AWAY = range(2)
ALPHA = 0.85


def safe_get(url):
    headers = {"x-apisports-key": API_KEY}
    for _ in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                return r.json()
            else:
                print(f"API error {r.status_code} for {url}")
        except Exception as e:
            print(f"Request exception: {e}")
        time.sleep(1)
    return None


def search_team(name):
    r = safe_get(API_URL + "/teams?search=" + name)
    if not r or not r.get("response"):
        return None, None
    team = r["response"][0]["team"]
    return team["id"], team["name"]


def fetch_last(team_id, n=15):
    r = safe_get(API_URL + f"/fixtures?team={team_id}&last={n}&status=FT")
    return r.get("response", []) if r else []


def fetch_venue(team_id, venue="home", n=12):
    r = safe_get(API_URL + f"/fixtures?team={team_id}&last=30&status=FT")
    if not r:
        return []
    result = []
    for m in r.get("response", []):
        is_home = m["teams"]["home"]["id"] == team_id
        if (venue == "home" and not is_home) or (venue == "away" and is_home):
            continue
        result.append(m)
        if len(result) >= n:
            break
    return result


def fetch_h2h(id1, id2, n=12):
    r = safe_get(API_URL + f"/fixtures/headtohead?h2h={id1}-{id2}&last={n}")
    if not r:
        return []
    cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc)
    result = []
    for m in r.get("response", []):
        if m["fixture"]["status"]["short"] != "FT":
            continue
        try:
            md = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00"))
            if md >= cutoff:
                result.append(m)
        except:
            pass
    return result


def fetch_xg(team_id, fixture_ids):
    xg_vals = []
    for fid in fixture_ids[:8]:  # 10 yerine 8, kota dostu
        r = safe_get(API_URL + f"/fixtures/statistics?fixture={fid}&team={team_id}")
        if not r:
            continue
        for ts in r.get("response", []):
            if ts.get("team", {}).get("id") != team_id:
                continue
            st = {s["type"]: s.get("value") for s in ts.get("statistics", [])}
            
            inside = float(st.get("Shots insidebox") or 0)
            on_target = float(st.get("Shots on Goal") or 0)
            outside = float(st.get("Shots outsidebox") or 0)
            
            # Ball Possession "%58" gibi geliyor → temizle
            poss_str = st.get("Ball Possession", "0%")
            poss = float(poss_str.replace("%", "")) / 100 if "%" in poss_str else 0.5
            
            xg_proxy = (inside * 0.14 + on_target * 0.24 + outside * 0.05 + poss * 0.8)
            xg_vals.append(xg_proxy)
        time.sleep(0.7)
    return round(sum(xg_vals) / len(xg_vals), 2) if xg_vals else 1.5


def weighted_avg(values, alpha=ALPHA):
    if not values:
        return 0.0
    w = ws = tw = 0.0
    for v in values:
        w = w * alpha + 1
        ws += v * w
        tw += w
    return ws / tw if tw > 0 else 0.0


def poisson_over(lam, threshold=1.5):
    k_max = math.floor(threshold)
    p_under = sum(math.exp(-lam) * (lam ** k) / math.factorial(k) for k in range(k_max + 1))
    return max(0.0, min(1.0, 1 - p_under))


def logistic(x, k=2.5):
    try:
        return 1.0 / (1.0 + math.exp(-k * x))
    except:
        return 0.5


def parse_matches(matches, team_id):
    gf, ga = [], []
    h1_gf, h1_ga = [], []
    h2_gf, h2_ga = [], []
    fids = []
    for m in matches:
        is_home = m["teams"]["home"]["id"] == team_id
        gh = m["goals"]["home"] or 0
        ga_val = m["goals"]["away"] or 0
        h1h = m.get("score", {}).get("halftime", {}).get("home") or 0
        h1a = m.get("score", {}).get("halftime", {}).get("away") or 0
        scored, conceded = (gh, ga_val) if is_home else (ga_val, gh)
        h1s, h1c = (h1h, h1a) if is_home else (h1a, h1h)
        h2s = max(0, scored - h1s)
        h2c = max(0, conceded - h1c)
        gf.append(scored)
        ga.append(conceded)
        h1_gf.append(h1s)
        h1_ga.append(h1c)
        h2_gf.append(h2s)
        h2_ga.append(h2c)
        fids.append(m["fixture"]["id"])
    return gf, ga, h1_gf, h1_ga, h2_gf, h2_ga, fids


def analyze_team(gen_matches, ven_matches, team_id):
    if not gen_matches:
        return None

    gf, ga, h1_gf, h1_ga, h2_gf, h2_ga, fids = parse_matches(gen_matches, team_id)
    if ven_matches:
        vgf, vga, vh1_gf, vh1_ga, vh2_gf, vh2_ga, _ = parse_matches(ven_matches, team_id)
    else:
        vgf = vga = vh1_gf = vh1_ga = vh2_gf = vh2_ga = []

    xg = fetch_xg(team_id, fids)

    def rate(lst):
        return sum(1 for x in lst if x > 0) / len(lst) if lst else 0.0

    w_gf = weighted_avg(gf) * 0.6 + (weighted_avg(vgf) if vgf else weighted_avg(gf)) * 0.4
    h1_gf_r = rate(h1_gf) * 0.5 + (rate(vh1_gf) if vh1_gf else rate(h1_gf)) * 0.5
    h2_gf_r = rate(h2_gf) * 0.5 + (rate(vh2_gf) if vh2_gf else rate(h2_gf)) * 0.5

    lam_attack = w_gf * 0.45 + xg * 0.30 + h1_gf_r * 0.15 + h2_gf_r * 0.10
    poisson_conf = poisson_over(lam_attack, 1.5)
    trend_score = w_gf * 0.40 + h1_gf_r * 0.25 + h2_gf_r * 0.20 + xg * 0.15
    logistic_conf = logistic(trend_score - 1.45)
    combined = poisson_conf * 0.60 + logistic_conf * 0.40

    over_conf = int(round(combined * 100))
    under_conf = 100 - over_conf

    return {
        "over_conf": over_conf,
        "under_conf": under_conf,
        "lam": round(lam_attack, 2),
        "xG": xg,
        "GF_avg": round(sum(gf) / len(gf), 2) if gf else 0,
        "H1_GF": round(h1_gf_r * 100),
        "H2_GF": round(h2_gf_r * 100),
    }


def analyze_h2h(h2h_matches, home_id, away_id):
    if not h2h_matches:
        return None
    totals = [(m["goals"]["home"] or 0) + (m["goals"]["away"] or 0) for m in h2h_matches]
    n = len(totals)
    over15_list = [1 if t > 1 else 0 for t in totals]
    weighted_over = round(weighted_avg(over15_list) * 100)
    return {"over15_rate": weighted_over, "n": n}


def blend_with_h2h(team_res, h2h_res, is_home):
    if not h2h_res or not team_res:
        return team_res
    blended = team_res["over_conf"] * 0.80 + h2h_res["over15_rate"] * 0.20
    team_res["over_conf"] = int(round(blended))
    team_res["under_conf"] = 100 - team_res["over_conf"]
    return team_res


def best_option(home_res, away_res):
    options = []
    if home_res:
        opt = "HOME 1.5 OVER" if home_res["over_conf"] >= 55 else "HOME 1.5 UNDER"
        conf = home_res["over_conf"] if "OVER" in opt else home_res["under_conf"]
        options.append((opt, conf))
    if away_res:
        opt = "AWAY 1.5 OVER" if away_res["over_conf"] >= 55 else "AWAY 1.5 UNDER"
        conf = away_res["over_conf"] if "OVER" in opt else away_res["under_conf"]
        options.append((opt, conf))
    if not options:
        return "No Option", 0
    options.sort(key=lambda x: x[1], reverse=True)
    return options[0]


def reliability_label(conf):
    if conf >= 75: return "High"
    elif conf >= 62: return "Medium"
    else: return "Low"


def run_analysis(home_name, away_name):
    try:
        home_id, home_real = search_team(home_name)
        away_id, away_real = search_team(away_name)
        if not home_id or not away_id:
            return None, None, None, None

        home_gen = fetch_last(home_id, 15)
        home_ven = fetch_venue(home_id, "home", 12)
        away_gen = fetch_last(away_id, 15)
        away_ven = fetch_venue(away_id, "away", 12)
        h2h_matches = fetch_h2h(home_id, away_id, 12)

        home_res = analyze_team(home_gen, home_ven, home_id)
        away_res = analyze_team(away_gen, away_ven, away_id)
        h2h_res = analyze_h2h(h2h_matches, home_id, away_id)

        home_res = blend_with_h2h(home_res, h2h_res, True)
        away_res = blend_with_h2h(away_res, h2h_res, False)

        return home_real, away_real, home_res, away_res
    except Exception as e:
        print(f"run_analysis error: {e}")
        return None, None, None, None


def format_msg(home_name, away_name, home_res, away_res):
    option, conf = best_option(home_res, away_res)
    reliability = reliability_label(conf)
    marker = "[+]" if "OVER" in option else "[-]"

    msg = "MATCH ANALYSIS RESULT 🔥\n\n"
    msg += "🏠 " + home_name + "\n"
    msg += "🚩 " + away_name + "\n"
    msg += "ℹ️ Most Reliable Option [% " + str(conf) + " ]\n"
    msg += "⚽ " + option + " " + marker + "\n"
    msg += "📊 Reliability [" + reliability + "]"
    return msg


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Analyze Another Match", callback_data="again")],
        [InlineKeyboardButton("Close Bot", callback_data="close")]
    ])


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Welcome! Use /analysis to start.")


async def analysis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🏠 Please enter Home Team Name")
    return HOME


async def home_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["home"] = update.message.text.strip()
    await update.message.reply_text("🚩 Please enter Away Team Name")
    return AWAY


async def away_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["away"] = update.message.text.strip()
    wait_msg = await update.message.reply_text("🛜 Analyzing, please wait...")
    
    home = context.user_data.get("home")
    away = context.user_data.get("away")
    
    if not home or not away:
        await wait_msg.edit_text("Error: Team names missing. Try /analysis again.")
        return ConversationHandler.END

    try:
        home_real, away_real, home_res, away_res = run_analysis(home, away)

        if not home_real or not away_real or not home_res or not away_res:
            await wait_msg.edit_text(
                "Team not found or API issue.\nUse full English names (e.g. Real Madrid, AS Roma, FC Porto, VfB Stuttgart)",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="again")]])
            )
            return ConversationHandler.END

        msg = format_msg(home_real, away_real, home_res, away_res)
        await wait_msg.edit_text(msg, reply_markup=main_keyboard())
    except Exception as e:
        print(f"away_team error: {e}")
        await wait_msg.edit_text("Analysis failed. Please try again or check console logs.")
    
    return ConversationHandler.END


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized.")
        return ConversationHandler.END
    await query.answer()
    if query.data == "again":
        await query.edit_message_text("🏠 Please enter Home Team Name")
        return HOME
    elif query.data == "close":
        await query.edit_message_text("Bot closed. Use /analysis to restart.")
        return ConversationHandler.END


def run_bot():
    while True:
        try:
            app = Application.builder().token(BOT_TOKEN).build()
            conv = ConversationHandler(
                entry_points=[CommandHandler("analysis", analysis_cmd)],
                states={
                    HOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, home_team),
                           CallbackQueryHandler(button_handler, pattern="^again$")],
                    AWAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, away_team),
                           CallbackQueryHandler(button_handler, pattern="^again$")],
                },
                fallbacks=[CommandHandler("analysis", analysis_cmd)],
                allow_reentry=True,
            )
            app.add_handler(CommandHandler("start", start_cmd))
            app.add_handler(conv)
            app.add_handler(CallbackQueryHandler(button_handler))
            print("BOT STARTED - Fixed Version")
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            print(f"Bot restart: {e}")
            time.sleep(5)


run_bot()
