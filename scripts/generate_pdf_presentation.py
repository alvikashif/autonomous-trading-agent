"""Generate high-aesthetic 16:9 PDF presentation using ReportLab."""
import os
import sys
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

WIDTH = 14 * inch  # 16:9 aspect ratio widescreen
HEIGHT = 7.875 * inch

# Color Palette: Slate Fintech & Emerald Dark Theme
C_BG = colors.HexColor("#090D16")
C_CARD = colors.HexColor("#0F172A")
C_CARD_BORDER = colors.HexColor("#1E293B")
C_PRIMARY = colors.HexColor("#38BDF8")      # Cyan / Sky
C_SUCCESS = colors.HexColor("#34D399")      # Emerald
C_TEXT_MAIN = colors.HexColor("#FFFFFF")    # White
C_TEXT_MUTED = colors.HexColor("#94A3B8")   # Slate
C_SUBTLE = colors.HexColor("#475569")
C_BADGE_BG = colors.HexColor("#020617")


def draw_card(c, x, y, w, h, title, subtitle, bullets, badge=""):
    # Card background & border
    c.setFillColor(C_CARD)
    c.setStrokeColor(C_CARD_BORDER)
    c.setLineWidth(1.2)
    c.roundRect(x, y, w, h, radius=10, fill=1, stroke=1)

    # Title
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(C_TEXT_MAIN)
    c.drawString(x + 18, y + h - 28, title)

    # Subtitle
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(C_PRIMARY)
    c.drawString(x + 18, y + h - 42, subtitle.upper())

    # Badge in top right
    if badge:
        badge_w = len(badge) * 6 + 12
        c.setFillColor(C_BADGE_BG)
        c.setStrokeColor(colors.HexColor("#38BDF8"))
        c.setLineWidth(0.8)
        c.roundRect(x + w - badge_w - 14, y + h - 34, badge_w, 18, radius=4, fill=1, stroke=1)
        c.setFont("Helvetica-Bold", 7.5)
        c.setFillColor(C_PRIMARY)
        c.drawCentredString(x + w - badge_w / 2 - 14, y + h - 26, badge)

    # Bullets
    text_y = y + h - 62
    c.setFont("Helvetica", 9.5)
    for lead, text in bullets:
        # Green square bullet
        c.setFillColor(C_SUCCESS)
        c.rect(x + 18, text_y + 1, 4, 4, fill=1, stroke=0)

        # Bold Lead
        c.setFont("Helvetica-Bold", 9.5)
        c.setFillColor(C_TEXT_MAIN)
        lead_str = lead + ": " if lead else ""
        c.drawString(x + 28, text_y, lead_str)
        lead_len = c.stringWidth(lead_str, "Helvetica-Bold", 9.5)

        # Regular Text
        c.setFont("Helvetica", 9.5)
        c.setFillColor(C_TEXT_MUTED)
        
        # Word wrap text
        words = text.split()
        cur_line = ""
        first_line = True
        line_x = x + 28 + lead_len
        max_w = w - 42 - (lead_len if first_line else 0)

        for word in words:
            test_line = cur_line + (" " if cur_line else "") + word
            if c.stringWidth(test_line, "Helvetica", 9.5) < max_w:
                cur_line = test_line
            else:
                c.drawString(line_x, text_y, cur_line)
                text_y -= 13
                line_x = x + 28
                max_w = w - 42
                cur_line = word
                first_line = False

        if cur_line:
            c.drawString(line_x, text_y, cur_line)
            text_y -= 13

        text_y -= 5


def draw_header(c, kicker, title, desc):
    # Kicker
    c.setFillColor(C_PRIMARY)
    c.circle(0.8 * inch + 4, HEIGHT - 0.75 * inch, 3.5, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(0.8 * inch + 14, HEIGHT - 0.78 * inch, kicker.upper())

    # Title
    c.setFont("Helvetica-Bold", 24)
    c.setFillColor(C_TEXT_MAIN)
    c.drawString(0.8 * inch, HEIGHT - 1.25 * inch, title)

    # Desc
    c.setFont("Helvetica", 11)
    c.setFillColor(C_TEXT_MUTED)
    c.drawString(0.8 * inch, HEIGHT - 1.55 * inch, desc)


def draw_footer(c, slide_num, total_slides, tags):
    c.setStrokeColor(C_CARD_BORDER)
    c.setLineWidth(1)
    c.line(0.8 * inch, 0.65 * inch, WIDTH - 0.8 * inch, 0.65 * inch)

    # Tags
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(C_SUBTLE)
    c.drawString(0.8 * inch, 0.42 * inch, " · ".join(tags))

    # Page number
    c.setFont("Helvetica", 9)
    c.setFillColor(C_SUBTLE)
    c.drawRightString(WIDTH - 0.8 * inch, 0.42 * inch, f"Slide {slide_num} of {total_slides} · Alvi Quantitative Terminal")


def build_pdf(out_path: str):
    c = canvas.Canvas(out_path, pagesize=(WIDTH, HEIGHT))

    # =========================================================================
    # SLIDE 1: Executive Overview
    # =========================================================================
    c.setFillColor(C_BG)
    c.rect(0, 0, WIDTH, HEIGHT, fill=1, stroke=0)
    draw_header(c, "Executive Architecture Briefing", 
                "Alvi Quantitative Terminal & Autonomous AI Engine",
                "Institutional-grade swing execution engine integrating AAOIFI Shariah compliance, dual-LLM quantitative reasoning, and Groww Delivery API execution.")

    w4 = (WIDTH - 1.6 * inch - 0.6 * inch) / 4
    h_cards = 4.8 * inch
    y_pos = 1.0 * inch

    draw_card(c, 0.8 * inch, y_pos, w4, h_cards,
              "100% AAOIFI Shariah", "Ethical Islamic Asset Screen", [
                  ("Strict 0% Riba", "Zero conventional banks, liquor, gambling, tobacco or weapons."),
                  ("Debt / Mcap <= 33%", "Strictly eliminates over-leveraged companies operating on interest."),
                  ("Cash / Mcap <= 33%", "Ensures capital backs productive real enterprise value."),
                  ("Delivery CNC Only", "Actual shares owned in Demat; zero margin or short selling.")
              ], badge="AAOIFI")

    draw_card(c, 0.8 * inch + w4 + 0.2 * inch, y_pos, w4, h_cards,
              "Mathematical Rigor", "20-Day Donchian Breakouts", [
                  ("Channel Breakouts", "Clean clearance over 20-day high with volatility expansion."),
                  ("Trend Alignment", "Price > 50 SMA > 200 SMA (Golden Trend Regime stack)."),
                  ("RSI Momentum", "Optimal 55–70 band; rejects parabolic moves (>75 RSI)."),
                  ("Asymmetric Payoff", "Strict 2:1 risk/reward target (+8% Target vs -4% Stop).")
              ], badge="DONCHIAN")

    draw_card(c, 0.8 * inch + (w4 + 0.2 * inch) * 2, y_pos, w4, h_cards,
              "Dual AI Brain", "Claude + Gemini Engine", [
                  ("Claude Sonnet 5.5", "Primary risk & conviction analyst (>=7.5 / 10 threshold)."),
                  ("Gemini 3.5 Flash", "Standby fallback engine with automatic failover circuit."),
                  ("Beyond Numbers", "Evaluates management changes, catalysts & crowd psychology."),
                  ("Prepaid Budget", "Real-time token cost tracking against prepaid $5.00 limit.")
              ], badge="DUAL LLM")

    draw_card(c, 0.8 * inch + (w4 + 0.2 * inch) * 3, y_pos, w4, h_cards,
              "Production Cluster", "CapRover on Oracle Cloud", [
                  ("Daily Cron (16:00 IST)", "Autonomous post-market analysis & Groww order routing."),
                  ("Mid-Day Watchdog", "120s intraday stop-loss (-4%) & profit trailing loop."),
                  ("Anti-Throttling", "RAM cache + Google Finance fallback on cloud IPs."),
                  ("Live Terminal", "OLED Dark Web UI with HMAC-SHA256 session cookies.")
              ], badge="DOCKER :14")

    draw_footer(c, 1, 5, ["LIVE GROWW CNC", "AAOIFI COMPLIANT", "CAPROVER DOCKER SWARM"])
    c.showPage()

    # =========================================================================
    # SLIDE 2: Technical Indicators & Pre-Screening
    # =========================================================================
    c.setFillColor(C_BG)
    c.rect(0, 0, WIDTH, HEIGHT, fill=1, stroke=0)
    draw_header(c, "Stage 1: Algorithmic Selection",
                "Pre-Screening Engine & Technical Indicators Checked",
                "Thousands of NSE cash stocks are mathematically evaluated before any AI tokens are consumed, filtering for high-probability momentum setups.")

    w3 = (WIDTH - 1.6 * inch - 0.4 * inch) / 3

    draw_card(c, 0.8 * inch, y_pos, w3, h_cards,
              "1. 20-Day Donchian Breakout", "Volatility & Resistance Clearance", [
                  ("Primary Formula", "Close > High_20d with expanding multi-timeframe volume."),
                  ("Clearance Margin", "Requires >0.5% clearance over the 20-day high to eliminate false wicks and traps."),
                  ("Institutional Base", "Identifies multi-week accumulation transitioning into an explosive markup trend."),
                  ("Capital Price Cap", "Filters equities <= Rs 5,000 to guarantee exact CNC position sizing within allocation limits.")
              ], badge="BREAKOUT")

    draw_card(c, 0.8 * inch + w3 + 0.2 * inch, y_pos, w3, h_cards,
              "2. Moving Average Alignment", "Multi-Timeframe Trend Stack", [
                  ("The Golden Stack", "Enforces Price > 50-day SMA > 200-day SMA in strict ascending hierarchy."),
                  ("Long-Term Filter", "Disqualifies stocks below 200 SMA (such as Patanjali at 411.5 vs 422.5 SMA200) regardless of news hype."),
                  ("Base Slope", "Demands an upward trajectory on the 50 SMA to avoid buying declining structural bear moves."),
                  ("Extension Guard", "Rejects stocks extended >25% above 50 SMA to avoid buying at exhaustion points.")
              ], badge="MA STACK")

    draw_card(c, 0.8 * inch + (w3 + 0.2 * inch) * 2, y_pos, w3, h_cards,
              "3. RSI & Momentum Velocity", "Non-Exhaustion & Velocity Auditing", [
                  ("14-Period RSI", "Target zone is strictly 55.0 to 70.0 (sweetspot of established momentum with upside room)."),
                  ("Overbought Veto", "Hard rejection on parabolic profiles with RSI > 75-80 (e.g. SAILIFE at 93.4, INDHOTEL at 84.0)."),
                  ("5d & 20d Velocity", "Cross-checks 5-day return (+5% to +12%) with 20-day trend to confirm accelerating volume."),
                  ("Stall Guard", "Rejects stocks showing negative 5-day momentum on 20-day highs (e.g. LTTS stalled at -0.5% 5d).")
              ], badge="OSCILLATOR")

    draw_footer(c, 2, 5, ["NSE CASH DELIVERY", "ZERO INTRADAY MARGIN", "TECHNICAL CONFIRMATION"])
    c.showPage()

    # =========================================================================
    # SLIDE 3: The AI Reasoning Brain
    # =========================================================================
    c.setFillColor(C_BG)
    c.rect(0, 0, WIDTH, HEIGHT, fill=1, stroke=0)
    draw_header(c, "Stage 2: Synthetic Intelligence",
                "The AI Quantitative Brain: Qualitative Multi-Factor Reasoning",
                "Raw technicals only provide candidates. The LLM Brain synthesizes balance sheet health, trader psychology, macro tape, and corporate catalysts.")

    w2 = (WIDTH - 1.6 * inch - 0.3 * inch) / 2
    h2 = 2.3 * inch

    draw_card(c, 0.8 * inch, y_pos + h2 + 0.2 * inch, w2, h2,
              "Fundamental Durability & Capital Health", "P&L, Debt Burden & Return on Equity (ROE)", [
                  ("Return on Equity", "Prioritizes efficient capital allocators (ROE >= 15%). Rejects weak/negative ROE (e.g. Ather at -6.7%)."),
                  ("Leverage Health", "Inspects Debt-to-Equity & interest coverage to ensure survival through market downturns."),
                  ("Operating Margin", "Evaluates quarterly EBITDA expansion vs commodity input price compression.")
              ], badge="BALANCE SHEET")

    draw_card(c, 0.8 * inch + w2 + 0.3 * inch, y_pos + h2 + 0.2 * inch, w2, h2,
              "Market Regime & Tape Context", "Bullish vs Neutral vs Defensive Regime", [
                  ("Macro Tape", "Diagnoses whether the market tape is BULLISH, NEUTRAL, or DEFENSIVE using Nifty 50 breadth."),
                  ("Risk Expectancy", "In a NEUTRAL tape, tight 4% stops on extended runs have negative expected value."),
                  ("Discipline", "Claude elected PASS on all 10 candidates in neutral tape, preserving 100% capital from pullback traps.")
              ], badge="REGIME")

    draw_card(c, 0.8 * inch, y_pos, w2, h2,
              "Trader Mindset & Crowd Dynamics", "Accumulation vs Retail FOMO & Gap Risk", [
                  ("Smart Money", "Distinguishes early institutional accumulation from late retail FOMO buying at the peak of a 30% surge."),
                  ("Overnight Risk", "Evaluates vulnerability to weekend headline gap-downs and post-earnings hangover."),
                  ("Tactical Retest", "Instructs waiting for breakout retest (e.g. Shyam Metalics ~1,270) rather than market-order chasing.")
              ], badge="PSYCHOLOGY")

    draw_card(c, 0.8 * inch + w2 + 0.3 * inch, y_pos, w2, h2,
              "Corporate Catalysts & Sizing Conviction", "Material Inflection Points & Sizing", [
                  ("Leadership Turnaround", "Ingests executive changes (e.g. incoming CEO re-rating prospects)."),
                  ("Sector Tailwinds", "Incorporates national infrastructure spending, metals demand, or export policy shifts."),
                  ("Conviction Scoring", "Grades 1.0 to 10.0 scale; requires >=7.5 with 2:1 profit asymmetry (+8% target / -4% stop) to execute.")
              ], badge="CATALYSTS")

    draw_footer(c, 3, 5, ["CLAUDE SONNET 5.5", "GEMINI 3.5 FLASH", "CONVICTION THRESHOLD >= 7.5"])
    c.showPage()

    # =========================================================================
    # SLIDE 4: Execution & Intraday Guardian
    # =========================================================================
    c.setFillColor(C_BG)
    c.rect(0, 0, WIDTH, HEIGHT, fill=1, stroke=0)
    draw_header(c, "Stage 3: Autonomous Execution",
                "Autonomous Execution & Intraday Protection",
                "End-to-end bridge between AI strategic verdicts and live market orders on Groww, guarded by real-time mid-day stop loss and trailing profit loops.")

    draw_card(c, 0.8 * inch, y_pos, w3, h_cards,
              "Daily Execution Engine", "agent-jobs (16:00 IST Mon–Fri)", [
                  ("Post-Market Loop", "Wakes at 16:00 IST after exchange closes to analyze verified daily settlement bars."),
                  ("Capital Sizing", "Dynamically calculates shares based on live Groww cash balance, capped at Rs 5,000 per position."),
                  ("Groww API Routing", "Directly submits orders to Groww API as PRODUCT_CNC (Cash Delivery) using automated TOTP 2FA."),
                  ("State Sync", "Persists execution signals, target levels, and stop prices to data/ai_signals.json.")
              ], badge="CRON 16:00")

    draw_card(c, 0.8 * inch + w3 + 0.2 * inch, y_pos, w3, h_cards,
              "Mid-Day Price Guardian", "agent-monitor (09:15–15:30 IST)", [
                  ("Intraday Watchdog", "Watches live Groww holdings every 120 seconds throughout NSE open market hours."),
                  ("Stop-Loss (-4%)", "If price drops <= stop price mid-day, verifies Kill Switch and executes an immediate MARKET CNC SELL on Groww."),
                  ("Target Hit (+8%)", "Triggers an instant mid-day query to the AI Brain for strategic momentum evaluation."),
                  ("Zero Night Calls", "Completely sleeps during nights and weekends, ensuring 0 unnecessary API requests.")
              ], badge="WATCHDOG")

    draw_card(c, 0.8 * inch + (w3 + 0.2 * inch) * 2, y_pos, w3, h_cards,
              "AI Profit-Maximizer", "Adaptive Profit Trailing Advisor", [
                  ("The Strategic Choice", "Does the winning stock have legs to run +15%, or is momentum exhausting at +8%?"),
                  ("If TAKE_PROFIT", "Orders an immediate CNC SELL to lock in the +8% gain into Demat."),
                  ("If HOLD_AND_TRAIL", "Raises target to +12%/+14% and raises stop loss to a trailing +5% locked-in profit in state."),
                  ("Wave Capture", "Allows riding explosive multi-week momentum without cutting winning trades short.")
              ], badge="TRAILING")

    draw_footer(c, 4, 5, ["GROWW API CNC DELIVERY", "MID-DAY STOP LOSS -4%", "AI PROFIT MAXIMIZER"])
    c.showPage()

    # =========================================================================
    # SLIDE 5: Risk Controls & Cloud Resilience
    # =========================================================================
    c.setFillColor(C_BG)
    c.rect(0, 0, WIDTH, HEIGHT, fill=1, stroke=0)
    draw_header(c, "Enterprise Risk Controls",
                "Multi-Layer Risk Architecture & Cloud Resilience",
                "Built for zero-downtime autonomy on Oracle Cloud: protecting cloud IP addresses, budget safeguards, automatic LLM failover, and universal kill switch.")

    draw_card(c, 0.8 * inch, y_pos + h2 + 0.2 * inch, w2, h2,
              "Data Center Anti-Throttling Guard", "Oracle Cloud IP Rate-Limit Defenses", [
                  ("In-Memory 30s Cache", "Repeated quote queries for the same symbol are served instantly from RAM with 0 network calls."),
                  ("Google Finance Fallback", "If Yahoo returns 429 Too Many Requests or blocks Oracle IPs, Google Finance seamlessly takes over."),
                  ("120s Polling Cadence", "Only ~187 quote requests over the entire 6.25h trading day, well below all rate limits.")
              ], badge="ANTI-THROTTLE")

    draw_card(c, 0.8 * inch + w2 + 0.3 * inch, y_pos + h2 + 0.2 * inch, w2, h2,
              "Prepaid AI Budget & Dual-LLM Failover", "Zero Interruption & Spend Governance", [
                  ("Prepaid $5.00 Cap", "Real-time token cost tracking ($0.0795 spent / $4.9205 remaining across 7 calls)."),
                  ("Auto Gemini Takeover", "If Claude budget exhausts or Anthropic errors, Google Gemini automatically executes trades."),
                  ("Dual Visibility", "Dashboard displays real-time spend, call counts, and failover status for both Claude & Gemini.")
              ], badge="FAILOVER")

    draw_card(c, 0.8 * inch, y_pos, w2, h2,
              "Physical Emergency Kill Switch", "Universal Veto Authority", [
                  ("Instant Circuit Breaker", "Engaging Kill Switch instantly blocks all buy and sell orders across all containers."),
                  ("Single-Click UI", "Prominent emergency halt button on the live OLED dashboard at trading.yourdomain.com."),
                  ("Persistent State", "Recorded in data/kill_switch.json with ISO-8601 audit timestamp and trigger reason.")
              ], badge="KILL SWITCH")

    draw_card(c, 0.8 * inch + w2 + 0.3 * inch, y_pos, w2, h2,
              "Production Cluster & Access Security", "Clean Deployment Architecture", [
                  ("Docker Swarm Cluster", "Running live on CapRover Oracle Cloud host (Image Tag :14)."),
                  ("Zero Duplication", "Strictly audited single-source environment variables across all services."),
                  ("HMAC-SHA256 Auth", "Tamper-evident 30-day session cookies with timing-safe comparison on custom username.")
              ], badge="CAPROVER :14")

    draw_footer(c, 5, 5, ["TRADING.YOURDOMAIN.COM", "HMAC AUTHENTICATED", "AUTOMATIC FAILOVER ACTIVE"])
    c.showPage()

    c.save()
    print(f"High-resolution PDF presentation saved to {out_path}")

if __name__ == "__main__":
    out_pdf = sys.argv[1] if len(sys.argv) > 1 else "Alvi_Quantitative_Trading_System.pdf"
    build_pdf(out_pdf)
