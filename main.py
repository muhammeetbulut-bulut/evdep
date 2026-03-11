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
        except Exception:
            pass
        time.sleep(1)
    return None


def search_team(name):
    r = safe_get(API_URL + "/teams?search=" + name)
    if not r:
        return None, None
    resp = r.get("response", [])
    if not resp:
        return None, None
    team = resp[0]["team"]
    return team["id"], team["name"]


def fetch_last(team_id, n=10):
    r = safe_get(API_URL + "/fixtures?team=" + str(team_id) + "&last=" + str(n) + "&status=FT")
    return r.get("response", []) if r else []


def fetch_venue(team_id, venue="home", n=10):
    r = safe_get(API_URL + "/fixtures?team=" + str(team_id) + "&last=30&status=FT")
    if not r:
        return []
    result = []
    for m in r.get("response", []):
        is_home = m["teams"]["home"]["id"] == team_id
        if venue == "home" and not is_home:
            continue
        if venue == "away" and is_home:
            continue
        result.append(m)
        if len(result) >= n:
            break
    return result


def fetch_h2h(id1, id2, n=8):
    r = safe_get(API_URL + "/fixtures/headtohead?h2h=" + str(id1) + "-" + str(id2) + "&last=" + str(n))
    if not r:
        return []
    cutoff = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = []
    for m in r.get("response", []):
        if m["fixture"]["status"]["short"] != "FT":
            continue
        try:
            md = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00"))
            if md >= cutoff:
                result.append(m)
        except Exception:
            pass
    return result


def fetch_xg(team_id, fixture_ids):
    xg_vals = []
    for fid in fixture_ids[:5]:
        r = safe_get(API_URL + "/fixtures/statistics?fixture=" + str(fid) + "&team=" + str(team_id))
        if not r:
            continue
        for ts in r.get("response", []):
            if ts["team"]["id"] != team_id:
                continue
            st = {s["type"]: s["value"] for s in ts.get("statistics", [])}
            shots = float(st.get("Total Shots") or 0)
            on_target = float(st.get("Shots on Goal") or 0)
            big = float(st.get("Big Chances") or 0)
            xg_vals.append(shots * 0.09 + on_target * 0.18 + big * 0.35)
        time.sleep(0.1)
    return round(sum(xg_vals) / len(xg_vals), 2) if xg_vals else 0.0


def weighted_avg(values, alpha=ALPHA):
    if not values:
        return 0.0
    w, ws, tw = 1.0, 0.0, 0.0
    for v in reversed(values):
        ws += v * w
        tw += w
        w *= alpha
    return ws / tw if tw > 0 else 0.0


def poisson_over(lam, threshold=1.5):
    k_max = math.floor(threshold)
    p_under = sum(
        math.exp(-lam) * (lam ** k) / math.factorial(k)
        for k in range(k_max + 1)
    )
    return max(0.0, min(1.0, 1 - p_under))


def logistic(x, k=2.5):
    try:
        return 1.0 / (1.0 + math.exp(-k * x))
    except Exception:
        return 0.5


def parse_matches(matches, team_id):
    gf, ga = [], []
    h1_gf, h1_ga = [], []
    h2_gf, h2_ga = [], []
    fids = []
    for m in matches:
        is_home = m["teams"]["home"]["id"] == team_id
        gh = m["goals"]["home"] or 0
        ga_ = m["goals"]["away"] or 0
        h1h = m.get("score", {}).get("halftime", {}).get("home") or 0
        h1a = m.get("score", {}).get("halftime", {}).get("away") or 0
        scored, conceded = (gh, ga_) if is_home else (ga_, gh)
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
        vgf, vga, vh1_gf, vh1_ga, vh2_gf, vh2_ga = [], [], [], [], [], []

    xg = fetch_xg(team_id, fids)

    def rate(lst):
        return sum(1 for x in lst if x > 0) / len(lst) if lst else 0.0

    w_gf = weighted_avg(gf) * 0.5 + (weighted_avg(vgf) if vgf else weighted_avg(gf)) * 0.5
    h1_gf_r = rate(h1_gf) * 0.5 + (rate(vh1_gf) if vh1_gf else rate(h1_gf)) * 0.5
    h1_ga_r = rate(h1_ga) * 0.5 + (rate(vh1_ga) if vh1_ga else rate(h1_ga)) * 0.5
    h2_gf_r = rate(h2_gf) * 0.5 + (rate(vh2_gf) if vh2_gf else rate(h2_gf)) * 0.5
    h2_ga_r = rate(h2_ga) * 0.5 + (rate(vh2_ga) if vh2_ga else rate(h2_ga)) * 0.5

    lam_attack = w_gf * 0.5 + xg * 0.3 + h1_gf_r * 0.1 + h2_gf_r * 0.1
    poisson_conf = poisson_over(lam_attack, 1.5)
    trend_score = w_gf * 0.45 + h1_gf_r * 0.2 + h2_gf_r * 0.2 + xg * 0.15
    logistic_conf = logistic(trend_score - 1.5)
    combined = poisson_conf * 0.5 + logistic_conf * 0.5
    over_conf = int(round(combined * 100))
    under_conf = 100 - over_conf

    return {
        "over_conf": over_conf,
        "under_conf": under_conf,
        "lam": round(lam_attack, 2),
        "xG": xg,
        "GF_avg": round(sum(gf) / len(gf), 2),
        "GA_avg": round(sum(ga) / len(ga), 2),
        "H1_GF": round(h1_gf_r * 100),
        "H2_GF": round(h2_gf_r * 100),
        "H1_GA": round(h1_ga_r * 100),
        "H2_GA": round(h2_ga_r * 100),
    }


def analyze_h2h(h2h_matches, home_id, away_id):
    if not h2h_matches:
        return None
    totals = []
    home_over, away_over = 0, 0
    for m in h2h_matches:
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        totals.append(gh + ga)
        mhid = m["teams"]["home"]["id"]
        if mhid == home_id:
            if gh > 1: home_over += 1
            if ga > 1: away_over += 1
        else:
            if ga > 1: home_over += 1
            if gh > 1: away_over += 1
    n = len(totals)
    return {
        "avg_total": round(sum(totals) / n, 2),
        "over15_rate": round(sum(1 for t in totals if t > 1) / n * 100),
        "home_over_rate": round(home_over / n * 100),
        "away_over_rate": round(away_over / n * 100),
        "n": n,
    }


def blend_with_h2h(team_res, h2h_res, is_home):
    if not h2h_res or not team_res:
        return team_res
    h2h_rate = h2h_res["home_over_rate"] if is_home else h2h_res["away_over_rate"]
    blended = team_res["over_conf"] * 0.70 + h2h_rate * 0.30
    team_res["over_conf"] = int(round(blended))
    team_res["under_conf"] = 100 - team_res["over_conf"]
    return team_res


def best_option(home_res, away_res):
    options = []
    if home_res:
        if home_res["over_conf"] >= 55:
            options.append(("HOME 1.5 OVER", home_res["over_conf"]))
        else:
            options.append(("HOME 1.5 UNDER", home_res["under_conf"]))
    if away_res:
        if away_res["over_conf"] >= 55:
            options.append(("AWAY 1.5 OVER", away_res["over_conf"]))
        else:
            options.append(("AWAY 1.5 UNDER", away_res["under_conf"]))
    if not options:
        return "No Option", 0
    options.sort(key=lambda x: x[1], reverse=True)
    return options[0]


def reliability_label(conf):
    if conf >= 75:
        return "High"
    elif conf >= 62:
        return "Medium"
    else:
        return "Low"


def run_analysis(home_name, away_name):
    home_id, home_real = search_team(home_name)
    away_id, away_real = search_team(away_name)
    if not home_id or not away_id:
        return None, None, None, None

    home_gen = fetch_last(home_id, 10)
    home_ven = fetch_venue(home_id, "home", 10)
    away_gen = fetch_last(away_id, 10)
    away_ven = fetch_venue(away_id, "away", 10)
    h2h_matches = fetch_h2h(home_id, away_id, 8)

    home_res = analyze_team(home_gen, home_ven, home_id)
    away_res = analyze_team(away_gen, away_ven, away_id)
    h2h_res = analyze_h2h(h2h_matches, home_id, away_id)

    home_res = blend_with_h2h(home_res, h2h_res, is_home=True)
    away_res = blend_with_h2h(away_res, h2h_res, is_home=False)

    return home_real, away_real, home_res, away_res


def format_msg(home_name, away_name, home_res, away_res):
    option, conf = best_option(home_res, away_res)
    reliability = reliability_label(conf)
    marker = "[+]" if "OVER" in option else "[-]"

    msg = "MATCH ANALYSIS RESULT \U0001f525\n\n"
    msg += "\U0001f3f3\ufe0f: " + home_name + "\n"
    msg += "\U0001f6a9: " + away_name + "\n"
    msg += "\u2139\ufe0f: Most Reliable Option [ %" + str(conf) + " ]\n"
    msg += "\u26bd\ufe0f: " + option + " " + marker + "\n"
    msg += "\U0001f6dc: Reliability [ " + reliability + " ]"
    return msg


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Run Another Analysis", callback_data="again")],
        [InlineKeyboardButton("Close Bot", callback_data="close")]
    ])


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "Welcome!\nHome/Away 1.5 Over/Under Analysis\nUse /analysis to start."
    )


async def analysis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("\U0001f3f3\ufe0f: Please enter Home Team Name")
    return HOME


async def home_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["home"] = update.message.text.strip()
    await update.message.reply_text("\U0001f6a9: Please enter Away Team Name")
    return AWAY


async def away_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["away"] = update.message.text.strip()
    wait = await update.message.reply_text("\U0001f6dc: Analysis started, please wait...")
    home = context.user_data["home"]
    away = context.user_data["away"]

    home_real, away_real, home_res, away_res = run_analysis(home, away)

    if not home_real or not away_real:
        await wait.edit_text(
            "Team not found. Please use English full name.\nExample: Barcelona, Arsenal, Fenerbahce",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="again")]])
        )
        return ConversationHandler.END

    msg = format_msg(home_real, away_real, home_res, away_res)
    await wait.edit_text(msg, reply_markup=main_keyboard())
    return ConversationHandler.END


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized.")
        return ConversationHandler.END
    await query.answer()
    if query.data == "again":
        await query.edit_message_text("\U0001f3f3\ufe0f: Please enter Home Team Name")
        return HOME
    elif query.data == "close":
        await query.edit_message_text("Bot closed.\nUse /analysis to restart.")
        return ConversationHandler.END


def run_bot():
    while True:
        try:
            app = Application.builder().token(BOT_TOKEN).build()
            conv = ConversationHandler(
                entry_points=[
                    CommandHandler("analysis", analysis_cmd),
                    CommandHandler("analiz", analysis_cmd),
                ],
                states={
                    HOME: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, home_team),
                        CallbackQueryHandler(button_handler, pattern="^again$"),
                    ],
                    AWAY: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, away_team),
                    ],
                },
                fallbacks=[CommandHandler("analysis", analysis_cmd)],
                per_message=False,
                allow_reentry=True,
            )
            app.add_handler(CommandHandler("start", start_cmd))
            app.add_handler(conv)
            app.add_handler(CallbackQueryHandler(button_handler))
            print("GOALREPORT BOT RUNNING")
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            print("RESTART: " + str(e))
            time.sleep(5)


run_bot()
