"""Generate high-aesthetic PowerPoint presentation and HTML slide deck for Alvi Quantitative Trading System."""
import sys
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE

def create_deck(output_pptx_path: str):
    prs = Presentation()
    prs.slide_width = Inches(13.333)  # 16:9 widescreen
    prs.slide_height = Inches(7.5)

    blank_slide_layout = prs.slide_layouts[6]

    # Color Palette: Slate Fintech & Emerald Dark Theme
    COLOR_BG = RGBColor(11, 15, 25)          # Deep Navy Slate
    COLOR_CARD = RGBColor(19, 27, 44)        # Card Background
    COLOR_CARD_BORDER = RGBColor(34, 48, 77) # Card Border
    COLOR_PRIMARY = RGBColor(56, 189, 248)   # Cyan / Sky Accent
    COLOR_SUCCESS = RGBColor(52, 211, 153)   # Emerald Green Accent
    COLOR_TEXT_MAIN = RGBColor(241, 245, 249)# Off-white Title
    COLOR_TEXT_MUTED = RGBColor(148, 163, 184)# Slate text
    COLOR_ACCENT = RGBColor(129, 140, 248)   # Indigo

    def set_bg(slide):
        bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), prs.slide_width, prs.slide_height)
        bg.fill.solid()
        bg.fill.fore_color.rgb = COLOR_BG
        bg.line.fill.background()
        return bg

    def add_card(slide, left, top, width, height, title, subtitle="", border_color=COLOR_CARD_BORDER):
        card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
        card.fill.solid()
        card.fill.fore_color.rgb = COLOR_CARD
        card.line.color.rgb = border_color
        card.line.width = Pt(1.5)
        
        tf = card.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.25)
        tf.margin_right = Inches(0.25)
        tf.margin_top = Inches(0.2)
        tf.margin_bottom = Inches(0.2)
        
        p = tf.paragraphs[0]
        p.text = title
        p.font.name = "Arial"
        p.font.size = Pt(15)
        p.font.bold = True
        p.font.color.rgb = COLOR_PRIMARY
        
        if subtitle:
            p2 = tf.add_paragraph()
            p2.text = subtitle
            p2.font.name = "Arial"
            p2.font.size = Pt(11)
            p2.font.color.rgb = COLOR_TEXT_MUTED
            p2.space_before = Pt(4)
            p2.space_after = Pt(8)
        return card

    # =========================================================================
    # SLIDE 1: Title & Executive Overview
    # =========================================================================
    s1 = prs.slides.add_slide(blank_slide_layout)
    set_bg(s1)

    # Header Box
    h_box = s1.shapes.add_textbox(Inches(1.0), Inches(0.8), Inches(11.33), Inches(2.2))
    tf1 = h_box.text_frame
    tf1.word_wrap = True
    
    p = tf1.paragraphs[0]
    p.text = "ALVI QUANTITATIVE TERMINAL"
    p.font.name = "Arial"
    p.font.size = Pt(13)
    p.font.bold = True
    p.font.color.rgb = COLOR_PRIMARY
    
    p = tf1.add_paragraph()
    p.text = "Autonomous AI Swing Trading & Shariah Asset Engine"
    p.font.name = "Arial"
    p.font.size = Pt(32)
    p.font.bold = True
    p.font.color.rgb = COLOR_TEXT_MAIN
    p.space_before = Pt(8)
    
    p = tf1.add_paragraph()
    p.text = "Institutional-grade swing execution engine integrating AAOIFI Shariah compliance, dual-LLM reasoning (Claude Sonnet 5.5 + Gemini 3.5), and live Groww API delivery execution."
    p.font.name = "Arial"
    p.font.size = Pt(14)
    p.font.color.rgb = COLOR_TEXT_MUTED
    p.space_before = Pt(10)

    # 4 Pillars
    c1 = add_card(s1, 1.0, 3.4, 2.65, 3.2, "100% AAOIFI Shariah", "Ethical Islamic Asset Screen")
    tf = c1.text_frame
    for bullet in [
        "Strict 0% Riba: Zero conventional banks, liquor, or gambling.",
        "Debt/Mcap <= 33%: Eliminates leveraged risk.",
        "Cash/Mcap <= 33%: Ensures real enterprise.",
        "Delivery CNC Only: Real shares in Demat, 0 margin/derivatives."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + bullet
        p.font.size = Pt(11)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(4)

    c2 = add_card(s1, 3.85, 3.4, 2.65, 3.2, "Mathematical Rigor", "20-Day Donchian Breakouts")
    tf = c2.text_frame
    for bullet in [
        "Clean price clearance over 20-day high.",
        "Golden Stack: Price > 50 SMA > 200 SMA.",
        "RSI Momentum: Optimal 55–70 band.",
        "Exhaustion Avoidance: Rejects parabolic moves (>75 RSI)."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + bullet
        p.font.size = Pt(11)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(4)

    c3 = add_card(s1, 6.7, 3.4, 2.65, 3.2, "Dual AI Brain", "Claude + Gemini Engine")
    tf = c3.text_frame
    for bullet in [
        "Claude Sonnet 5.5: Primary risk analyst.",
        "Gemini 3.5 Flash: Auto-failover engine.",
        "Synthesizes news, catalysts & trader psychology.",
        "High Bar: Requires >=7.5 / 10 conviction."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + bullet
        p.font.size = Pt(11)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(4)

    c4 = add_card(s1, 9.55, 3.4, 2.65, 3.2, "Full-Stack Cloud", "Autonomous Oracle Cluster")
    tf = c4.text_frame
    for bullet in [
        "CapRover Docker Swarm architecture.",
        "Intraday Watchdog: Auto stop-loss at -4%.",
        "Take-Profit Maximizer: Trailing targets.",
        "Anti-Throttling: Resilient quote fallback."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + bullet
        p.font.size = Pt(11)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(4)

    # =========================================================================
    # SLIDE 2: Technical Indicators & Pre-Screening
    # =========================================================================
    s2 = prs.slides.add_slide(blank_slide_layout)
    set_bg(s2)

    h_box2 = s2.shapes.add_textbox(Inches(1.0), Inches(0.8), Inches(11.33), Inches(1.5))
    tf2 = h_box2.text_frame
    p = tf2.paragraphs[0]
    p.text = "STAGE 1: QUANTITATIVE SELECTION PIPELINE"
    p.font.name = "Arial"
    p.font.size = Pt(13)
    p.font.bold = True
    p.font.color.rgb = COLOR_PRIMARY
    
    p = tf2.add_paragraph()
    p.text = "Mathematical Screening & Technical Indicators Checked"
    p.font.name = "Arial"
    p.font.size = Pt(28)
    p.font.bold = True
    p.font.color.rgb = COLOR_TEXT_MAIN
    p.space_before = Pt(4)

    t1 = add_card(s2, 1.0, 2.5, 3.6, 4.3, "1. 20-Day Channel Breakout", "Donchian Volatility Expansion")
    tf = t1.text_frame
    for b in [
        "Indicator: 20-day Highest High Price (Donchian Upper Band).",
        "Condition: Close > High_20d with expanding volume.",
        "Why it matters: Breaks multi-week resistance bases and establishes institutional continuation.",
        "Filter: Rejects marginal ticks (<0.5% clearance) to avoid fake-out traps."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(11.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(8)

    t2 = add_card(s2, 4.85, 2.5, 3.6, 4.3, "2. Structural Trend Alignment", "Multi-Timeframe Moving Averages")
    tf = t2.text_frame
    for b in [
        "Indicator: 50-day SMA & 200-day SMA trend stack.",
        "Condition: Current Price > 50 SMA > 200 SMA.",
        "Trend Confirmation: Stock must be in a confirmed structural secular bull trend.",
        "Safety Rule: Disqualifies stocks below 200 SMA (such as Patanjali) regardless of short-term hype."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(11.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(8)

    t3 = add_card(s2, 8.7, 2.5, 3.6, 4.3, "3. RSI & Momentum Velocity", "Oscillator Non-Exhaustion Check")
    tf = t3.text_frame
    for b in [
        "Indicator: 14-period Wilder Relative Strength Index (RSI).",
        "Optimal Sweetspot: 55.0 to 70.0 (Strong bullish momentum).",
        "Overbought Veto: Hard rejection for RSI > 75–80 (e.g. SAILIFE at 93.4, INDHOTEL at 84.0).",
        "Velocity Check: 5-day & 20-day returns must reflect accelerating, not decelerating, buying volume."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(11.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(8)

    # =========================================================================
    # SLIDE 3: The AI Reasoning Brain
    # =========================================================================
    s3 = prs.slides.add_slide(blank_slide_layout)
    set_bg(s3)

    h_box3 = s3.shapes.add_textbox(Inches(1.0), Inches(0.8), Inches(11.33), Inches(1.5))
    tf3 = h_box3.text_frame
    p = tf3.paragraphs[0]
    p.text = "STAGE 2: THE AI QUANTITATIVE BRAIN"
    p.font.name = "Arial"
    p.font.size = Pt(13)
    p.font.bold = True
    p.font.color.rgb = COLOR_PRIMARY
    
    p = tf3.add_paragraph()
    p.text = "Qualitative Reasoning & Multi-Factor Intelligence"
    p.font.name = "Arial"
    p.font.size = Pt(28)
    p.font.bold = True
    p.font.color.rgb = COLOR_TEXT_MAIN
    p.space_before = Pt(4)

    a1 = add_card(s3, 1.0, 2.5, 5.4, 2.1, "Fundamental Durability & Capital Efficiency", "P&L, Debt & Return on Equity (ROE)")
    tf = a1.text_frame
    for b in [
        "Evaluates Return on Equity (ROE >= 15% preferred; rejects weak capital allocators).",
        "Inspects Debt-to-Equity and cash generation to guarantee fundamental survival during market drawdowns.",
        "Real Example: Rejects newly listed high-momentum stocks (like Lenskart) lacking profitable operating track records."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(10.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(3)

    a2 = add_card(s3, 6.7, 2.5, 5.6, 2.1, "Market Regime & Tape Diagnosis", "Bullish vs Neutral vs Defensive Regime")
    tf = a2.text_frame
    for b in [
        "Calculates market breadth against Nifty 50 benchmark.",
        "Adjusts conviction threshold based on tape context: In a NEUTRAL tape, tight stops have poor expected value on extended runs.",
        "Enforces capital preservation: Claude elected PASS when 10 candidates were overbought, preserving 100% of cash."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(10.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(3)

    a3 = add_card(s3, 1.0, 4.8, 5.4, 2.0, "Trader Mindset & Crowd Psychology", "Exhaustion, FOMO & Mean-Reversion")
    tf = a3.text_frame
    for b in [
        "Differentiates early institutional accumulation from late retail FOMO chasing.",
        "Assesses gap-down risk: Evaluates weekend headline exposure and post-earnings hangover.",
        "Checks retest levels: Instructs waiting for breakout retest (e.g. Shyam Metalics ~1,270) rather than buying top."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(10.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(3)

    a4 = add_card(s3, 6.7, 4.8, 5.6, 2.0, "Corporate Catalysts & Leadership Changes", "Material Inflection Points")
    tf = a4.text_frame
    for b in [
        "Ingests executive changes (e.g. incoming CEO/leadership turnaround).",
        "Weights sectoral tailwinds (e.g. government infrastructure Capex, metals margin expansion).",
        "Enforces strict 2:1 asymmetric payoff (+8% Target vs -4% Stop Loss) before approving trade."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(10.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(3)

    # =========================================================================
    # SLIDE 4: Autonomous Execution & Intraday Protection
    # =========================================================================
    s4 = prs.slides.add_slide(blank_slide_layout)
    set_bg(s4)

    h_box4 = s4.shapes.add_textbox(Inches(1.0), Inches(0.8), Inches(11.33), Inches(1.5))
    tf4 = h_box4.text_frame
    p = tf4.paragraphs[0]
    p.text = "STAGE 3: EXECUTION & INTRADAY GUARDIAN"
    p.font.name = "Arial"
    p.font.size = Pt(13)
    p.font.bold = True
    p.font.color.rgb = COLOR_PRIMARY
    
    p = tf4.add_paragraph()
    p.text = "Live Broker Connectivity & Mid-Day Stop/Trail Logic"
    p.font.name = "Arial"
    p.font.size = Pt(28)
    p.font.bold = True
    p.font.color.rgb = COLOR_TEXT_MAIN
    p.space_before = Pt(4)

    e1 = add_card(s4, 1.0, 2.5, 3.6, 4.3, "Daily Scheduled Cron (16:00 IST)", "Post-Market Ingestion in agent-jobs")
    tf = e1.text_frame
    for b in [
        "Wakes at 16:00 IST post-market close.",
        "Ingests official day closing prices and updates local technical cache.",
        "Queries Claude / Gemini with extracted candidate profiles.",
        "Calculates sizing capped at Rs 5,000.",
        "If AI_AUTO_TRADE=true and conviction >= 7.5, submits live CNC order to Groww."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(11)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(6)

    e2 = add_card(s4, 4.85, 2.5, 3.6, 4.3, "Mid-Day Watchdog (09:15–15:30)", "Active Intraday Guard in agent-monitor")
    tf = e2.text_frame
    for b in [
        "Monitors live Groww holdings every 120s during open market hours.",
        "Automatic Stop-Loss (-4%): If stock drops to stop price mid-day, submits immediate MARKET CNC SELL to Groww.",
        "Target Reached (+8%): Instantly queries AI Brain mid-day to evaluate trade momentum.",
        "Automatic Kill-Switch check before placing any trade."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(11)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(6)

    e3 = add_card(s4, 8.7, 2.5, 3.6, 4.3, "AI Take-Profit Maximizer", "Trailing Winners Beyond Initial Target")
    tf = e3.text_frame
    for b in [
        "Asks AI: Is momentum exhausted or accelerating?",
        "If TAKE_PROFIT: Sells immediately to lock in +8% gain.",
        "If HOLD_AND_TRAIL: Adjusts target to +12%/+14% and raises stop to trailing +5% in RAM/JSON.",
        "Prevents prematurely cutting off massive winning breakout waves."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(11)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(6)

    # =========================================================================
    # SLIDE 5: Safety Architecture & Cloud Resilience
    # =========================================================================
    s5 = prs.slides.add_slide(blank_slide_layout)
    set_bg(s5)

    h_box5 = s5.shapes.add_textbox(Inches(1.0), Inches(0.8), Inches(11.33), Inches(1.5))
    tf5 = h_box5.text_frame
    p = tf5.paragraphs[0]
    p.text = "ENTERPRISE RISK & INFRASTRUCTURE CONTROLS"
    p.font.name = "Arial"
    p.font.size = Pt(13)
    p.font.bold = True
    p.font.color.rgb = COLOR_PRIMARY
    
    p = tf5.add_paragraph()
    p.text = "Multi-Layer Circuit Breakers & Anti-Throttling Engine"
    p.font.name = "Arial"
    p.font.size = Pt(28)
    p.font.bold = True
    p.font.color.rgb = COLOR_TEXT_MAIN
    p.space_before = Pt(4)

    r1 = add_card(s5, 1.0, 2.5, 5.4, 2.1, "Data Center Anti-Throttling Guard", "Oracle Cloud IP Resilience")
    tf = r1.text_frame
    for b in [
        "In-Memory 30s Cache: Duplicate quote queries served from RAM with 0 network calls.",
        "Dual-Source Fallback: If Yahoo returns 429 Too Many Requests, Google Finance takes over transparently.",
        "120s Polling Cadence: Only ~187 quote requests over the entire trading day, avoiding data-center IP bans.",
        "Groww API Safety: 0 quote requests sent to Groww; only calls Demat for orders and holdings."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(10.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(3)

    r2 = add_card(s5, 6.7, 2.5, 5.6, 2.1, "Prepaid AI Budget & Failover Circuit", "Zero Interruption Guarantee")
    tf = r2.text_frame
    for b in [
        "Prepaid $5.00 Limit: Automated real-time token tracking ($0.0795 spent, $4.9205 / 98.4% remaining).",
        "Automatic Failover to Gemini: If Claude balance exhausts or Anthropic errors, Google Gemini automatically executes trades.",
        "Dual Provider Visibility: Dashboard renders live spend, calls, and status for both Claude and Gemini."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(10.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(3)

    r3 = add_card(s5, 1.0, 4.8, 5.4, 2.0, "Physical Emergency Kill Switch", "Universal Veto Authority")
    tf = r3.text_frame
    for b in [
        "Persistent State: Stored in data/kill_switch.json with instant audit log.",
        "One-Click UI Trigger: Engaged directly from dashboard red button or CLI.",
        "Hard Circuit Breaker: Checked before every single buy or sell order across all containers.",
        "Guaranteed Safety: Unconditionally halts all order placement immediately."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(10.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(3)

    r4 = add_card(s5, 6.7, 4.8, 5.6, 2.0, "Production Environment & Access Security", "Clean Deployment Matrix")
    tf = r4.text_frame
    for b in [
        "Docker Swarm Cluster: Running live on CapRover Oracle Host (Image :14).",
        "Zero Environment Duplication: Clean separation of concerns between dashboard, jobs & monitor.",
        "HMAC-SHA256 Auth: 30-day persistent session cookies with timing-safe comparison on trading.lifemint.net."
    ]:
        p = tf.add_paragraph()
        p.text = "• " + b
        p.font.size = Pt(10.5)
        p.font.color.rgb = COLOR_TEXT_MAIN
        p.space_before = Pt(3)

    prs.save(output_pptx_path)
    print(f"PowerPoint Presentation successfully saved to {output_pptx_path}")

if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "Alvi_Quantitative_Trading_System.pptx"
    create_deck(out_path)
