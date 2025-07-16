import os
import time
import traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pandas as pd
import yfinance as yf
import ta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# === Load environment variables ===
load_dotenv()
STARTING_CASH = float(os.getenv("STARTING_CASH", "10000"))
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_PASS = os.getenv("EMAIL_PASS")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "0.05"))
MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "14"))

# === File paths ===
BUY_LOG = "buys.csv"
EXIT_LOG = "exits.csv"

# === Ticker source ===
def get_sp_list():
    urls = {
        "S&P 400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
        "S&P 600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
    }
    tickers = []
    for url in urls.values():
        try:
            tables = pd.read_html(url)
            for table in tables:
                if "Symbol" in table.columns:
                    tickers.extend(table["Symbol"].tolist())
                    break
        except Exception:
            continue
    return sorted(set(t.replace(".", "-") for t in tickers))

# === Indicator scan ===
def scan_indicators(ticker):
    try:
        df = yf.download(ticker, period="250d", interval="1d", auto_adjust=True, progress=False)
        if df is None or df.empty:
            return None, None

        df.dropna(inplace=True)

        df['rsi'] = ta.momentum.RSIIndicator(df['Close']).rsi()
        macd = ta.trend.MACD(df['Close'])
        df['macd'] = macd.macd()
        df['macd_signal'] = macd.macd_signal()
        bb = ta.volatility.BollingerBands(df['Close'])
        df['bb_lower'] = bb.bollinger_lband()
        df['bb_upper'] = bb.bollinger_hband()
        df['adx'] = ta.trend.ADXIndicator(df['High'], df['Low'], df['Close']).adx()
        df['sma_200'] = ta.trend.SMAIndicator(df['Close'], window=200).sma_indicator()

        df.dropna(inplace=True)
        latest = df.iloc[-1]
        reasons = []

        if latest['rsi'] < 50:
            reasons.append("RSI<50")
        if latest['macd'] > latest['macd_signal']:
            reasons.append("MACD>Signal")
        if latest['Close'] <= latest['bb_upper']:
            reasons.append("Close<=BB_Upper")
        if latest['adx'] > 15:
            reasons.append("ADX>15")
        if latest['Close'] > latest['sma_200']:
            reasons.append("Close>SMA200")

        return df, reasons if len(reasons) == 5 else None

    except Exception:
        traceback.print_exc()
        return None, None

# === Load or initialize portfolio ===
def load_portfolio():
    if os.path.exists(BUY_LOG):
        return pd.read_csv(BUY_LOG)
    return pd.DataFrame(columns=["ticker", "price", "entry", "reason", "shares"])

# === Save buy ===
def log_buy(ticker, price, reason, shares):
    now = datetime.now().strftime("%Y-%m-%d")
    row = pd.DataFrame([[ticker, price, now, reason, shares]],
                       columns=["ticker", "price", "entry", "reason", "shares"])
    header = not os.path.exists(BUY_LOG)
    row.to_csv(BUY_LOG, mode="a", header=header, index=False)
    print(f"[BUY] {ticker} at ${price:.2f} for {shares} shares — {reason}")

# === Save exit ===
def log_exit(ticker, price, reason):
    now = datetime.now().strftime("%Y-%m-%d")
    row = pd.DataFrame([[ticker, price, now, reason]],
                       columns=["ticker", "price", "exit", "reason"])
    header = not os.path.exists(EXIT_LOG)
    row.to_csv(EXIT_LOG, mode="a", header=header, index=False)
    print(f"[SELL] {ticker} at ${price:.2f} — {reason}")

# === Email results ===
def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASS)
        server.send_message(msg)

# === Main logic ===
def run_bot():
    print("=" * 40)
    print(f"📈 Swing Trader Paper Bot Running - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 40)

    portfolio = load_portfolio()
    tickers = get_sp_list()
    balance = STARTING_CASH - (portfolio["price"] * portfolio["shares"]).sum()
    buy_msgs = []
    sell_msgs = []

    print(f"Scanning {len(tickers)} tickers...\n")
    for ticker in tickers:
        if ticker in portfolio["ticker"].values:
            continue

        df, reasons = scan_indicators(ticker)
        if reasons:
            price = df["Close"].iloc[-1]
            allocation = STARTING_CASH * MAX_POSITION_SIZE
            shares = int(allocation // price)
            if shares > 0 and price * shares <= balance:
                log_buy(ticker, price, ", ".join(reasons), shares)
                buy_msgs.append(f"{ticker} at ${price:.2f} — {', '.join(reasons)}")
                balance -= price * shares

        time.sleep(0.5)

    # === Exit logic ===
    updated_portfolio = load_portfolio()
    to_keep = []
    for _, row in updated_portfolio.iterrows():
        ticker, buy_price, entry_date = row["ticker"], row["price"], row["entry"]
        shares = int(row["shares"])
        df = yf.download(ticker, period="30d", interval="1d", auto_adjust=True, progress=False)
        if df is None or df.empty:
            continue

        latest = df.iloc[-1]
        exit_reason = None

        if latest["Close"] >= buy_price * 1.10:
            exit_reason = "Take Profit 10%"
        elif latest["Close"] <= buy_price * 0.93:
            exit_reason = "Stop Loss 7%"
        elif datetime.now() - datetime.strptime(entry_date, "%Y-%m-%d") > timedelta(days=MAX_HOLD_DAYS):
            exit_reason = f"Max Hold {MAX_HOLD_DAYS} days"
        elif "macd" in df.columns and df["macd"].iloc[-1] < df["macd_signal"].iloc[-1]:
            exit_reason = "MACD cross down"

        if exit_reason:
            log_exit(ticker, latest["Close"], exit_reason)
            sell_msgs.append(f"{ticker} at ${latest['Close']:.2f} — {exit_reason}")
        else:
            to_keep.append(row)

    pd.DataFrame(to_keep).to_csv(BUY_LOG, index=False)

    # === Email summary ===
    summary = ""
    if buy_msgs:
        summary += "<h3>✅ Buys</h3><ul>" + "".join(f"<li>{msg}</li>" for msg in buy_msgs) + "</ul>"
    if sell_msgs:
        summary += "<h3>🚪 Sells</h3><ul>" + "".join(f"<li>{msg}</li>" for msg in sell_msgs) + "</ul>"
    if not summary:
        summary = "<p>No trades executed today.</p>"

    send_email("📈 Daily Swing Trade Report", summary)

# === Run ===
if __name__ == "__main__":
    run_bot()
