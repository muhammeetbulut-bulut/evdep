import requests
import numpy as np
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, ConversationHandler, CallbackQueryHandler

BOT_TOKEN = "8688455737:AAENmxT4dn8LoExD-XRcS6J3noLIscBMeQo"
ADMIN_ID = 8480843841
API_KEY = "0c0clad20573b309924dd3d7b1bc3e62"
API_URL = "https://v3.football.api-sports.io"

HOME, AWAY = range(2)

# ---- Team Search ----
def search_team(name):
    url = f"{API_URL}/teams?search={name}"
    headers = {"x-apisports-key": API_KEY}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code != 200: return None
    resp = r.json().get("response", [])
    if not resp: return None
    # return first match name + id
    team = resp[0]['team']
    return team['id'], team['name']

# ---- Fetch Matches ----
def fetch_last_matches(team_id, N=10):
    url = f"{API_URL}/fixtures?team={team_id}&last={N}"
    headers = {"x-apisports-key": API_KEY}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code != 200:
        return []
    return r.json().get("response", [])

# ---- Compute Metrics ----
def compute_metrics(matches, team_id, alpha=0.7):
    if not matches: return {}
    gf, ga, h1_gf, h1_ga, h2_gf, h2_ga = [], [], [], [], [], []
    for m in matches:
        home_id = m['teams']['home']['id']
        away_id = m['teams']['away']['id']
        home_goals = m['goals']['home']
        away_goals = m['goals']['away']
        h1_home = m['score']['halftime']['home']
        h1_away = m['score']['halftime']['away']
        h2_home = home_goals - h1_home
        h2_away = away_goals - h1_away
        if team_id == home_id:
            gf.append(home_goals); ga.append(away_goals)
            h1_gf.append(h1_home); h1_ga.append(h1_away)
            h2_gf.append(h2_home); h2_ga.append(h2_away)
        else:
            gf.append(away_goals); ga.append(home_goals)
            h1_gf.append(h1_away); h1_ga.append(h1_home)
            h2_gf.append(h2_away); h2_ga.append(h2_home)
    weights = np.array([alpha**i for i in range(len(gf))])
    weights = weights/weights.sum()
    return {
        "GF_avg": np.mean(gf),
        "GA_avg": np.mean(ga),
        "H1_GF_rate": np.mean([1 if g>0 else 0 for g in h1_gf]),
        "H1_GA_rate": np.mean([1 if g>0 else 0 for g in h1_ga]),
        "H2_GF_rate": np.mean([1 if g>0 else 0 for g in h2_gf]),
        "H2_GA_rate": np.mean([1 if g>0 else 0 for g in h2_ga]),
        "Weighted_GF": float(np.dot(weights, gf)),
        "Weighted_GA": float(np.dot(weights, ga))
    }

# ---- Run Analysis ----
def run_analysis(home_name, away_name):
    home_id, home_real = search_team(home_name) or (None, None)
    away_id, away_real = search_team(away_name) or (None, None)
    if not home_id or not away_id:
        return "Error: team not found", "0%"

    home_metrics = compute_metrics(fetch_last_matches(home_id, N=10), home_id)
    away_metrics = compute_metrics(fetch_last_matches(away_id, N=10), away_id)

    score_home = home_metrics.get("Weighted_GF",0) + home_metrics.get("H1_GF_rate",0) + home_metrics.get("H2_GF_rate",0)
    score_away = away_metrics.get("Weighted_GF",0) + away_metrics.get("H1_GF_rate",0) + away_metrics.get("H2_GF_rate",0)

    if score_home >= score_away and score_home >= 1.5:
        option = "HOME 1.5 OVER ✅"; confidence = "80%"
    elif score_away > score_home and score_away >= 1.5:
        option = "AWAY 1.5 OVER ✅"; confidence = "78%"
    else:
        option = "UNDER 1.5 ⚠️"; confidence = "65%"

    return option, confidence

# ---- Bot Flow ----
def start(update, context):
    if update.effective_user.id != ADMIN_ID: return
    update.message.reply_text("Welcome to the Analysis Bot 👋")
    update.message.reply_text("ℹ️: Home/Away 1.5 Over/Under Analysis → /analiz")

def analiz(update, context):
    if update.effective_user.id != ADMIN_ID: return
    update.message.reply_text("🏳️: Please enter Home Team Name")
    return HOME

def home_team(update, context):
    context.user_data['home'] = update.message.text
    update.message.reply_text("🚩: Please enter Away Team Name")
    return AWAY

def away_team(update, context):
    context.user_data['away'] = update.message.text
    update.message.reply_text("🛜: Analysis started, please wait...")
    home = context.user_data['home']
    away = context.user_data['away']
    option, confidence = run_analysis(home, away)
    msg = (
        f"MATCH ANALYSIS RESULT 🔥\n"
        f"🏳️: {home}\n"
        f"🚩: {away}\n"
        f"ℹ️: Most Reliable Option [{confidence}]\n"
        f"⚽️: {option}"
    )
    keyboard = [
        [InlineKeyboardButton("🔄 Run Another Analysis", callback_data='again')],
        [InlineKeyboardButton("🚨 Close Bot", callback_data='close')]
    ]
    update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

def button_handler(update, context):
    query = update.callback_query
    query.answer()
    if query.data == 'again':
        query.edit_message_text("🏳️: Please enter Home Team Name")
        return HOME
    elif query.data == 'close':
        query.edit_message_text("🚨 Bot Closed. For analysis use /analiz")
        return ConversationHandler.END

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("analiz", analiz)],
        states={
            HOME: [MessageHandler(Filters.text & ~Filters.command, home_team)],
            AWAY: [MessageHandler(Filters.text & ~Filters.command, away_team)],
        },
        fallbacks=[]
    )
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(conv_handler)
    dp.add_handler(CallbackQueryHandler(button_handler))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
