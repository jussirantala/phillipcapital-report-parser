import pdfplumber
import re
import os
import sys
import json
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.gridspec as gridspec
import numpy as np


MONTH_ABBREVS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
MONTH_NAMES = {
    "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
    "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
    "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
}


def load_config():
    """Load multipliers from config.json next to this script."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path) as f:
        return json.load(f)


def find_pdf():
    """Find a PDF file to parse — auto-detect in current dir or ask the user."""
    search_dir = os.path.dirname(os.path.abspath(__file__)) or os.getcwd()

    while True:
        pdfs = sorted([f for f in os.listdir(search_dir) if f.lower().endswith(".pdf")])

        if len(pdfs) == 1:
            path = os.path.join(search_dir, pdfs[0])
            print(f"Found PDF: {pdfs[0]}")
            return path

        if len(pdfs) > 1:
            print(f"\nFound {len(pdfs)} PDF files in {search_dir}:\n")
            for i, name in enumerate(pdfs, 1):
                print(f"  {i}. {name}")
            print()
            while True:
                choice = input(f"Select a file (1-{len(pdfs)}): ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(pdfs):
                    path = os.path.join(search_dir, pdfs[int(choice) - 1])
                    return path
                print("Invalid choice, try again.")

        # No PDFs found
        print(f"No PDF files found in {search_dir}")
        user_path = input("Enter path to a PDF file or directory: ").strip().strip('"')
        if os.path.isfile(user_path) and user_path.lower().endswith(".pdf"):
            return user_path
        if os.path.isdir(user_path):
            search_dir = user_path
            continue
        print(f"Invalid path: {user_path}")


def detect_years(pdf_path):
    """Scan the PDF for all years that appear in RUN DATE fields."""
    years = set()
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            m = re.search(r"RUN DATE\s*:\s*\d{2}/\d{2}/(\d{4})", text)
            if m:
                years.add(m.group(1))
    return sorted(years)


def pick_year(pdf_path):
    """Detect years in the PDF and let the user choose if there are multiple."""
    years = detect_years(pdf_path)

    if not years:
        year = input("Could not detect any year in the PDF. Enter year (e.g. 2025): ").strip()
        return year

    if len(years) == 1:
        print(f"Detected year: {years[0]}")
        return years[0]

    print(f"\nMultiple years found in the PDF:\n")
    for i, y in enumerate(years, 1):
        print(f"  {i}. {y}")
    print()
    while True:
        choice = input(f"Select year (1-{len(years)}): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(years):
            return years[int(choice) - 1]
        print("Invalid choice, try again.")


def derive_eur_usd_rate(pdf_path, year):
    """Derive EUR/USD rate from WIRE RECEIVED + Adjustments USDE pairs on same dates."""
    # Collect per-date: EUR deposits and USDE adjustments
    date_eur = defaultdict(float)
    date_usde = defaultdict(float)

    with pdfplumber.open(pdf_path) as pdf:
        current_month_prefix = None
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            m = re.search(rf"RUN DATE\s*:\s*(\d{{2}})/\d{{2}}/{year}", text)
            if m:
                current_month_prefix = m.group(1)
            if not current_month_prefix:
                continue

            for line in text.split("\n"):
                # WIRE RECEIVED: EUR column (3rd number)
                wr = re.match(
                    rf"(\d{{2}}/\d{{2}}/{year})\s+WIRE RECEIVED\s+[\d,.]+\s+([\d,.]+)\s+[\d,.]+",
                    line,
                )
                if wr:
                    date_eur[wr.group(1)] += float(wr.group(2).replace(",", ""))
                    continue

                # Adjustments USDE: Combined column (1st number, positive only = deposit)
                adj = re.match(
                    rf"(\d{{2}}/\d{{2}}/{year})\s+Adjustme[nt]{{1,3}}s?\s+USDE\s+([\d,.]+)\s",
                    line,
                )
                if adj:
                    val = float(adj.group(2).replace(",", ""))
                    date_usde[adj.group(1)] += val

    # Match dates where both a deposit and a positive adjustment exist
    rates = []
    for date in date_eur:
        if date in date_usde and date_eur[date] > 0 and date_usde[date] > 0:
            rate = date_usde[date] / date_eur[date]
            if 0.8 < rate < 1.5:  # sanity check
                rates.append(rate)

    if rates:
        return sum(rates) / len(rates)
    return None


def make_month_data():
    """Create a fresh month data dict."""
    return {
        "pnl": 0.0, "commission": 0.0,
        "clearing_fee": 0.0, "nfa_fee": 0.0,
        "deposits_eur": 0.0, "withdrawals_eur": 0.0,
        "wire_fees_usd": 0.0,
        "contracts": defaultdict(lambda: {"buys": 0.0, "sells": 0.0, "buy_qty": 0, "sell_qty": 0}),
    }


def parse_trades(pdf_path, year, multipliers):
    """Parse trades from a PhillipCapital futures PDF export.

    Returns dict per month with keys:
        pnl, commission, clearing_fee, nfa_fee,
        deposits_eur, withdrawals_eur, wire_fees_usd,
        contracts: {symbol: {buys, sells, buy_qty, sell_qty}}
    """
    data = defaultdict(make_month_data)
    current_month = None
    yy = year[2:]  # e.g. "25" from "2025"
    unknown_symbols = set()

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            # Detect month from RUN DATE : MM/DD/YYYY To MM/DD/YYYY
            m = re.search(rf"RUN DATE\s*:\s*(\d{{2}})/\d{{2}}/{year}", text)
            if m:
                current_month = m.group(1) + year  # e.g. "022025"

            if not current_month:
                continue

            for line in text.split("\n"):
                # ── Cash flow: daily Realised P&L ────────────────────────
                pnl_match = re.match(
                    rf"\d{{2}}/\d{{2}}/{year}\s+Realised P&L\s+([-\d,]+\.\d{{2}})",
                    line,
                )
                if pnl_match:
                    data[current_month]["pnl"] += float(pnl_match.group(1).replace(",", ""))
                    continue

                # ── Cash flow: daily Commission ──────────────────────────
                comm_match = re.match(
                    rf"\d{{2}}/\d{{2}}/{year}\s+Commission\s+([-\d,]+\.\d{{2}})",
                    line,
                )
                if comm_match:
                    data[current_month]["commission"] += float(comm_match.group(1).replace(",", ""))
                    continue

                # ── Cash flow: daily Clearing Fee ────────────────────────
                clear_match = re.match(
                    rf"\d{{2}}/\d{{2}}/{year}\s+Clearing Fee\s+([-\d,]+\.\d{{2}})",
                    line,
                )
                if clear_match:
                    data[current_month]["clearing_fee"] += float(clear_match.group(1).replace(",", ""))
                    continue

                # ── Cash flow: daily NFA ─────────────────────────────────
                nfa_match = re.match(
                    rf"\d{{2}}/\d{{2}}/{year}\s+NFA\s+([-\d,]+\.\d{{2}})",
                    line,
                )
                if nfa_match:
                    data[current_month]["nfa_fee"] += float(nfa_match.group(1).replace(",", ""))
                    continue

                # ── Deposits: WIRE RECEIVED (EUR column) ─────────────────
                dep_match = re.match(
                    rf"\d{{2}}/\d{{2}}/{year}\s+WIRE RECEIVED\s+[\d,.]+\s+([\d,.]+)\s+[\d,.]+",
                    line,
                )
                if dep_match:
                    data[current_month]["deposits_eur"] += float(dep_match.group(1).replace(",", ""))
                    continue

                # ── Withdrawals: WIRE SENT (EUR column, negative) ────────
                wd_match = re.match(
                    rf"\d{{2}}/\d{{2}}/{year}\s+WIRE SENT.*?\s+([-\d,.]+)\s+[\d,.]+\s*$",
                    line,
                )
                if wd_match:
                    data[current_month]["withdrawals_eur"] += float(wd_match.group(1).replace(",", ""))
                    continue

                # ── Wire fees (USD column) ───────────────────────────────
                wf_match = re.match(
                    rf"\d{{2}}/\d{{2}}/{year}\s+WIRE FEE\s+[\d,.]+\s+[\d,.]+\s+([-\d,.]+)",
                    line,
                )
                if wf_match:
                    data[current_month]["wire_fees_usd"] += float(wf_match.group(1).replace(",", ""))
                    continue

                # ── Trade lines: buy (positive qty) ──────────────────────
                # Format: "02/28/25 3 CME MICRO MINI NQ(MNQ) Mar 25 20557.50 USD"
                # Also handles spaced date: "0 2 / 2 8 / 2 5 3 CME ..."
                buy_match = re.search(
                    rf"(\d+)\s+CME\s+.*?\((\w+)\)\s+(?:{MONTH_ABBREVS})\s+{yy}\s+([\d.]+)\s+\w{{3}}$",
                    line,
                )
                if buy_match and "-" not in line.split("CME")[0]:
                    qty = int(buy_match.group(1))
                    symbol = buy_match.group(2)
                    price = float(buy_match.group(3))
                    data[current_month]["contracts"][symbol]["buys"] += price * qty
                    data[current_month]["contracts"][symbol]["buy_qty"] += qty
                    if symbol not in multipliers:
                        unknown_symbols.add(symbol)
                    continue

                # ── Trade lines: sell (negative qty) ─────────────────────
                # Format: "02/28/25 -3 CME ... (MNQ) Mar 25 20567.75 61.50 USD"
                sell_match = re.search(
                    rf"-(\d+)\s+CME\s+.*?\((\w+)\)\s+(?:{MONTH_ABBREVS})\s+{yy}\s+([\d.]+)\s+[-\d,.]+\s+\w{{3}}$",
                    line,
                )
                if sell_match:
                    qty = int(sell_match.group(1))
                    symbol = sell_match.group(2)
                    price = float(sell_match.group(3))
                    data[current_month]["contracts"][symbol]["sells"] += price * qty
                    data[current_month]["contracts"][symbol]["sell_qty"] += qty
                    if symbol not in multipliers:
                        unknown_symbols.add(symbol)
                    continue

    if unknown_symbols:
        print(f"\nWARNING: Unknown contract symbols (not in config.json): {', '.join(sorted(unknown_symbols))}")
        print("Add them to config.json with the correct multiplier.\n")

    return data


def calc_month_totals(data, month, multipliers):
    """Calculate aggregated buys, sells, and calculated P&L for a month across all contracts."""
    d = data[month]
    total_buys = 0.0
    total_sells = 0.0
    calc_pnl = 0.0
    for sym, c in d["contracts"].items():
        mult = multipliers.get(sym, 1)
        total_buys += c["buys"]
        total_sells += c["sells"]
        calc_pnl += (c["sells"] - c["buys"]) * mult
    return total_buys, total_sells, calc_pnl


def print_table(data, year, eur_rate, multipliers):
    """Print a text summary table to the console."""
    months = sorted(data.keys())
    if not months:
        print("No trade data found.")
        return

    # ── Per-contract breakdown ───────────────────────────────────────────────
    all_symbols = set()
    for month in months:
        all_symbols.update(data[month]["contracts"].keys())
    if all_symbols:
        print(f"\n{'':=^100}")
        print(f"{'PER-CONTRACT BREAKDOWN':^100}")
        print(f"{'':=^100}")
        print(f"\n{'MONTH':>8} | {'SYM':>5} | {'MULT':>5} | {'BUY QTY':>8} | {'SELL QTY':>8} | {'BUYS':>16} | {'SELLS':>16} | {'CALC P&L':>14}")
        print("-" * 100)
        for month in months:
            label = f"{MONTH_NAMES.get(month[:2], month[:2])} {year}"
            for sym in sorted(data[month]["contracts"].keys()):
                c = data[month]["contracts"][sym]
                mult = multipliers.get(sym, 1)
                pnl = (c["sells"] - c["buys"]) * mult
                print(f"{label:>8} | {sym:>5} | {mult:>5} | {c['buy_qty']:>8} | {c['sell_qty']:>8} | {c['buys']:>16,.2f} | {c['sells']:>16,.2f} | {pnl:>+14,.2f}")

    # ── USD Table ────────────────────────────────────────────────────────────
    print(f"\n{'':=^140}")
    print(f"{'USD SUMMARY':^140}")
    print(f"{'':=^140}")
    print(f"\n{'MONTH':>8} | {'BUYS':>14} | {'SELLS':>14} | {'CALC P&L':>12} | {'PDF P&L':>12} | {'DIFF':>10} | {'COMMISSION':>12} | {'CLEARING':>10} | {'NFA':>8} | {'NET P&L':>14}")
    print("-" * 140)

    totals = defaultdict(float)
    for month in months:
        d = data[month]
        buys, sells, calc_pnl = calc_month_totals(data, month, multipliers)
        net = d["pnl"] + d["commission"] + d["clearing_fee"] + d["nfa_fee"]
        diff = calc_pnl - d["pnl"]
        label = f"{MONTH_NAMES.get(month[:2], month[:2])} {year}"
        print(f"{label:>8} | {buys:>14,.2f} | {sells:>14,.2f} | {calc_pnl:>+12,.2f} | {d['pnl']:>+12,.2f} | {diff:>+10,.2f} | {d['commission']:>12,.2f} | {d['clearing_fee']:>10,.2f} | {d['nfa_fee']:>8,.2f} | {net:>+14,.2f}")
        totals["buys"] += buys
        totals["sells"] += sells
        totals["calc_pnl"] += calc_pnl
        for k in ("pnl", "commission", "clearing_fee", "nfa_fee", "deposits_eur", "withdrawals_eur", "wire_fees_usd"):
            totals[k] += d[k]

    print("-" * 140)
    net_total = totals["pnl"] + totals["commission"] + totals["clearing_fee"] + totals["nfa_fee"]
    total_diff = totals["calc_pnl"] - totals["pnl"]
    print(f"{'TOTAL':>8} | {totals['buys']:>14,.2f} | {totals['sells']:>14,.2f} | {totals['calc_pnl']:>+12,.2f} | {totals['pnl']:>+12,.2f} | {total_diff:>+10,.2f} | {totals['commission']:>12,.2f} | {totals['clearing_fee']:>10,.2f} | {totals['nfa_fee']:>8,.2f} | {net_total:>+14,.2f}")

    # ── Deposits & Withdrawals ───────────────────────────────────────────────
    print(f"\n{'MONTH':>8} | {'DEPOSITS (EUR)':>16} | {'WITHDRAWALS (EUR)':>18} | {'WIRE FEES (USD)':>16} | {'NET FLOW (EUR)':>16}")
    print("-" * 90)

    for month in months:
        d = data[month]
        if d["deposits_eur"] == 0 and d["withdrawals_eur"] == 0 and d["wire_fees_usd"] == 0:
            continue
        label = f"{MONTH_NAMES.get(month[:2], month[:2])} {year}"
        net_flow = d["deposits_eur"] + d["withdrawals_eur"]
        print(f"{label:>8} | {d['deposits_eur']:>16,.2f} | {d['withdrawals_eur']:>+18,.2f} | {d['wire_fees_usd']:>16,.2f} | {net_flow:>+16,.2f}")

    print("-" * 90)
    net_flow_total = totals["deposits_eur"] + totals["withdrawals_eur"]
    print(f"{'TOTAL':>8} | {totals['deposits_eur']:>16,.2f} | {totals['withdrawals_eur']:>+18,.2f} | {totals['wire_fees_usd']:>16,.2f} | {net_flow_total:>+16,.2f}")

    # ── EUR Table ────────────────────────────────────────────────────────────
    print(f"\n{'':=^130}")
    print(f"{'EUR SUMMARY  (EUR/USD rate: ' + f'{eur_rate:.4f})':^130}")
    print(f"{'':=^130}")
    to_eur = 1.0 / eur_rate
    print(f"\n{'MONTH':>8} | {'REALISED P&L':>14} | {'COMMISSION':>12} | {'CLEARING':>10} | {'NFA':>8} | {'WIRE FEES':>10} | {'NET P&L':>14} | {'DEPOSITS':>12} | {'WITHDRAWALS':>14}")
    print("-" * 130)

    for month in months:
        d = data[month]
        net_usd = d["pnl"] + d["commission"] + d["clearing_fee"] + d["nfa_fee"] + d["wire_fees_usd"]
        label = f"{MONTH_NAMES.get(month[:2], month[:2])} {year}"
        print(f"{label:>8} | {d['pnl']*to_eur:>+14,.2f} | {d['commission']*to_eur:>12,.2f} | {d['clearing_fee']*to_eur:>10,.2f} | {d['nfa_fee']*to_eur:>8,.2f} | {d['wire_fees_usd']*to_eur:>10,.2f} | {net_usd*to_eur:>+14,.2f} | {d['deposits_eur']:>12,.2f} | {d['withdrawals_eur']:>+14,.2f}")

    print("-" * 130)
    total_net_usd = net_total + totals["wire_fees_usd"]
    print(f"{'TOTAL':>8} | {totals['pnl']*to_eur:>+14,.2f} | {totals['commission']*to_eur:>12,.2f} | {totals['clearing_fee']*to_eur:>10,.2f} | {totals['nfa_fee']*to_eur:>8,.2f} | {totals['wire_fees_usd']*to_eur:>10,.2f} | {total_net_usd*to_eur:>+14,.2f} | {totals['deposits_eur']:>12,.2f} | {totals['withdrawals_eur']:>+14,.2f}")


def generate_report(data, year, pdf_path, eur_rate, multipliers):
    """Generate a PNG dashboard report."""
    months = sorted(data.keys())
    if not months:
        print("No trade data found — skipping report generation.")
        return

    labels = [MONTH_NAMES.get(m[:2], m[:2]) for m in months]
    to_eur = 1.0 / eur_rate

    # Per-month values
    pnl_vals = [data[m]["pnl"] for m in months]
    comm_vals = [data[m]["commission"] for m in months]
    clear_vals = [data[m]["clearing_fee"] for m in months]
    nfa_vals = [data[m]["nfa_fee"] for m in months]
    wire_fees = [data[m]["wire_fees_usd"] for m in months]
    net_vals = [pnl_vals[i] + comm_vals[i] + clear_vals[i] + nfa_vals[i] for i in range(len(months))]

    month_totals = [calc_month_totals(data, m, multipliers) for m in months]
    buy_vals = [t[0] for t in month_totals]
    sell_vals = [t[1] for t in month_totals]
    calc_pnl_vals = [t[2] for t in month_totals]

    dep_vals = [data[m]["deposits_eur"] for m in months]
    wd_vals = [data[m]["withdrawals_eur"] for m in months]
    cumulative = list(np.cumsum(net_vals))

    total_pnl = sum(pnl_vals)
    total_comm = sum(comm_vals)
    total_clear = sum(clear_vals)
    total_nfa = sum(nfa_vals)
    total_fees = total_comm + total_clear + total_nfa
    total_net = total_pnl + total_fees
    total_buys = sum(buy_vals)
    total_sells = sum(sell_vals)
    total_deps = sum(dep_vals)
    total_wds = sum(wd_vals)
    total_wire_fees = sum(wire_fees)

    # ── Colours ──────────────────────────────────────────────────────────────
    pos_color = "#4CAF50"
    neg_color = "#F44336"
    PANEL_BG = "#16213e"
    TICK_COL = "#c0c0c0"
    GRID_COL = "#2a2a4a"

    def money(v, sym="$"):
        return f"{sym}{v:+,.0f}" if v != 0 else f"{sym}0"

    def fmt_k(x, _):
        return f"${x/1000:+.1f}k" if abs(x) >= 1000 else f"${x:+.0f}"

    # ── Layout ───────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(20, 18), facecolor="#1a1a2e")
    fig.suptitle(
        f"PhillipCapital Futures Report  —  {year}",
        fontsize=18, fontweight="bold", color="white", y=0.99,
    )

    gs = gridspec.GridSpec(
        4, 2, figure=fig,
        hspace=0.45, wspace=0.3,
        left=0.06, right=0.97,
        top=0.96, bottom=0.03,
        height_ratios=[1, 1, 1.2, 1.0],
    )

    ax_bar = fig.add_subplot(gs[0, 0])   # Monthly P&L bars
    ax_cum = fig.add_subplot(gs[0, 1])   # Cumulative P&L line
    ax_wf  = fig.add_subplot(gs[1, :])   # Net P&L after fees waterfall
    ax_sum = fig.add_subplot(gs[2, :])   # USD Summary table
    ax_eur = fig.add_subplot(gs[3, :])   # EUR Summary table

    for ax in (ax_bar, ax_cum, ax_wf, ax_sum, ax_eur):
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=TICK_COL, labelsize=9)
        for spine in ax.spines.values():
            spine.set_edgecolor("#3a3a5a")

    # ── 1. Monthly Gross P&L (bar chart) ─────────────────────────────────────
    ax_bar.set_title("Monthly Realised P&L (before fees)", color="white", fontsize=11, pad=8)
    x_pos = np.arange(len(months))
    bar_colors = [pos_color if v >= 0 else neg_color for v in pnl_vals]
    bars = ax_bar.bar(x_pos, pnl_vals, color=bar_colors, width=0.6, zorder=3)

    for bar, val in zip(bars, pnl_vals):
        ax_bar.text(
            bar.get_x() + bar.get_width() / 2, val,
            money(val),
            ha="center", va="bottom" if val >= 0 else "top",
            color="white", fontsize=9, fontweight="bold",
        )

    ax_bar.axhline(0, color="#555577", linewidth=0.8)
    ax_bar.set_xticks(x_pos)
    ax_bar.set_xticklabels(labels, color="white", fontsize=10)
    ax_bar.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_k))
    ax_bar.set_ylabel("USD", color=TICK_COL, fontsize=9)
    ax_bar.grid(axis="y", color=GRID_COL, linewidth=0.7, zorder=0)

    ax_bar.text(
        0.97, 0.05,
        f"Total: {money(total_pnl)}",
        transform=ax_bar.transAxes, ha="right", va="bottom",
        color="white", fontsize=10, fontweight="bold",
        bbox=dict(facecolor="#0f3460", edgecolor="#4a90d9", boxstyle="round,pad=0.3"),
    )

    # ── 2. Cumulative P&L (line chart) ───────────────────────────────────────
    ax_cum.set_title("Cumulative P&L (after all fees)", color="white", fontsize=11, pad=8)
    ax_cum.plot(x_pos, cumulative, color="#00bcd4", linewidth=2, zorder=3)
    ax_cum.fill_between(x_pos, cumulative, alpha=0.15, color="#00bcd4", zorder=2)
    ax_cum.axhline(0, color="#555577", linewidth=0.8)
    ax_cum.set_xticks(x_pos)
    ax_cum.set_xticklabels(labels, rotation=45, ha="right", color=TICK_COL, fontsize=8)
    ax_cum.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_k))
    ax_cum.set_ylabel("USD", color=TICK_COL, fontsize=9)
    ax_cum.grid(color=GRID_COL, linewidth=0.7, zorder=0)

    if cumulative:
        ax_cum.annotate(
            money(cumulative[-1]),
            xy=(x_pos[-1], cumulative[-1]),
            xytext=(-45, 10), textcoords="offset points",
            color="white", fontsize=9, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color="#aaaacc", lw=1),
        )

    # ── 3. Net P&L after fees (waterfall) ────────────────────────────────────
    ax_wf.set_title("Monthly Net P&L (after all fees)", color="white", fontsize=11, pad=8)
    wf_colors = [pos_color if v >= 0 else neg_color for v in net_vals]
    bars_wf = ax_wf.bar(x_pos, net_vals, color=wf_colors, width=0.6, zorder=3)

    for bar, val in zip(bars_wf, net_vals):
        ax_wf.text(
            bar.get_x() + bar.get_width() / 2, val,
            money(val),
            ha="center", va="bottom" if val >= 0 else "top",
            color="white", fontsize=8,
        )

    total_fees_per_month = [comm_vals[i] + clear_vals[i] + nfa_vals[i] for i in range(len(months))]
    ax_wf.scatter(x_pos, total_fees_per_month, color="#FF9800", s=30, zorder=5, label="Total Fees")
    ax_wf.axhline(0, color="#555577", linewidth=0.8)
    ax_wf.set_xticks(x_pos)
    ax_wf.set_xticklabels(labels, rotation=30, ha="right", color=TICK_COL, fontsize=9)
    ax_wf.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_k))
    ax_wf.set_ylabel("USD", color=TICK_COL, fontsize=9)
    ax_wf.grid(axis="y", color=GRID_COL, linewidth=0.7, zorder=0)
    ax_wf.legend(loc="upper left", facecolor=PANEL_BG, edgecolor="#3a3a5a",
                 labelcolor="white", fontsize=8)

    # ── 4. USD Summary table ─────────────────────────────────────────────────
    ax_sum.axis("off")
    ax_sum.set_title("USD Summary  (trades + deposits/withdrawals)", color="white", fontsize=11, pad=8)

    col_labels = ["Month", "Buys", "Sells", "P&L", "Commiss.", "Clearing", "NFA", "Net P&L", "Dep EUR", "Wdraw EUR", "Wire Fee"]
    col_x = [0.01, 0.08, 0.18, 0.28, 0.38, 0.48, 0.56, 0.64, 0.75, 0.85, 0.94]
    row_h = 0.065
    y_start = 0.95

    for ci, label in enumerate(col_labels):
        ax_sum.text(col_x[ci], y_start, label,
                    transform=ax_sum.transAxes,
                    color="#aaddff", fontsize=8, fontweight="bold",
                    va="top", family="monospace")

    ax_sum.plot([0.01, 0.99], [y_start - 0.03, y_start - 0.03],
                color="#3a3a5a", linewidth=0.8, transform=ax_sum.transAxes)

    for ri, month in enumerate(months):
        y = y_start - 0.06 - ri * row_h
        d = data[month]
        buys_m, sells_m, calc_m = month_totals[ri]
        net = d["pnl"] + d["commission"] + d["clearing_fee"] + d["nfa_fee"]
        m_label = f"{MONTH_NAMES.get(month[:2], month[:2])} {year}"

        pnl_color = pos_color if d["pnl"] >= 0 else neg_color
        net_color = pos_color if net >= 0 else neg_color

        vals = [
            (m_label, "white"),
            (f"${buys_m:,.0f}", TICK_COL),
            (f"${sells_m:,.0f}", TICK_COL),
            (f"${d['pnl']:+,.2f}", pnl_color),
            (f"${d['commission']:,.2f}", "#FF9800"),
            (f"${d['clearing_fee']:,.2f}", "#FF9800"),
            (f"${d['nfa_fee']:,.2f}", "#FF9800"),
            (f"${net:+,.2f}", net_color),
            (f"{d['deposits_eur']:,.0f}" if d['deposits_eur'] else "", "#2196F3"),
            (f"{d['withdrawals_eur']:+,.0f}" if d['withdrawals_eur'] else "", neg_color),
            (f"${d['wire_fees_usd']:,.2f}" if d['wire_fees_usd'] else "", "#FF9800"),
        ]
        for ci, (txt, color) in enumerate(vals):
            ax_sum.text(col_x[ci], y, txt,
                        transform=ax_sum.transAxes,
                        color=color, fontsize=8, va="top", family="monospace")

    # Totals row
    y_total = y_start - 0.06 - len(months) * row_h
    ax_sum.plot([0.01, 0.99], [y_total + row_h * 0.45, y_total + row_h * 0.45],
                color="#3a3a5a", linewidth=0.8, transform=ax_sum.transAxes)

    totals_row = [
        ("TOTAL", "white"),
        (f"${total_buys:,.0f}", TICK_COL),
        (f"${total_sells:,.0f}", TICK_COL),
        (f"${total_pnl:+,.2f}", pos_color if total_pnl >= 0 else neg_color),
        (f"${total_comm:,.2f}", "#FF9800"),
        (f"${total_clear:,.2f}", "#FF9800"),
        (f"${total_nfa:,.2f}", "#FF9800"),
        (f"${total_net:+,.2f}", pos_color if total_net >= 0 else neg_color),
        (f"{total_deps:,.0f}", "#2196F3"),
        (f"{total_wds:+,.0f}", neg_color),
        (f"${total_wire_fees:,.2f}", "#FF9800"),
    ]
    for ci, (txt, color) in enumerate(totals_row):
        ax_sum.text(col_x[ci], y_total, txt,
                    transform=ax_sum.transAxes,
                    color=color, fontsize=9, va="top", fontweight="bold",
                    family="monospace")

    # ── 5. EUR Summary table ─────────────────────────────────────────────────
    ax_eur.axis("off")
    ax_eur.set_title(f"EUR Summary  (EUR/USD rate: {eur_rate:.4f})", color="white", fontsize=11, pad=8)

    eur_cols = ["Month", "P&L", "Commiss.", "Clearing", "NFA", "Wire Fee", "Net P&L", "Deposits", "Withdrawals", "Net Flow"]
    eur_x = [0.01, 0.09, 0.20, 0.31, 0.40, 0.49, 0.59, 0.70, 0.80, 0.91]
    y_start_e = 0.95

    for ci, label in enumerate(eur_cols):
        ax_eur.text(eur_x[ci], y_start_e, label,
                    transform=ax_eur.transAxes,
                    color="#aaddff", fontsize=8, fontweight="bold",
                    va="top", family="monospace")

    ax_eur.plot([0.01, 0.99], [y_start_e - 0.03, y_start_e - 0.03],
                color="#3a3a5a", linewidth=0.8, transform=ax_eur.transAxes)

    for ri, month in enumerate(months):
        y = y_start_e - 0.06 - ri * row_h
        d = data[month]
        net_usd = d["pnl"] + d["commission"] + d["clearing_fee"] + d["nfa_fee"] + d["wire_fees_usd"]
        net_flow = d["deposits_eur"] + d["withdrawals_eur"]
        m_label = f"{MONTH_NAMES.get(month[:2], month[:2])} {year}"

        pnl_e = d["pnl"] * to_eur
        net_e = net_usd * to_eur
        pnl_color = pos_color if pnl_e >= 0 else neg_color
        net_color = pos_color if net_e >= 0 else neg_color
        flow_color = pos_color if net_flow >= 0 else neg_color

        vals = [
            (m_label, "white"),
            (f"{pnl_e:+,.2f}", pnl_color),
            (f"{d['commission']*to_eur:,.2f}", "#FF9800"),
            (f"{d['clearing_fee']*to_eur:,.2f}", "#FF9800"),
            (f"{d['nfa_fee']*to_eur:,.2f}", "#FF9800"),
            (f"{d['wire_fees_usd']*to_eur:,.2f}" if d['wire_fees_usd'] else "", "#FF9800"),
            (f"{net_e:+,.2f}", net_color),
            (f"{d['deposits_eur']:,.0f}" if d['deposits_eur'] else "", "#2196F3"),
            (f"{d['withdrawals_eur']:+,.0f}" if d['withdrawals_eur'] else "", neg_color),
            (f"{net_flow:+,.0f}" if net_flow else "", flow_color),
        ]
        for ci, (txt, color) in enumerate(vals):
            ax_eur.text(eur_x[ci], y, txt,
                        transform=ax_eur.transAxes,
                        color=color, fontsize=8, va="top", family="monospace")

    # EUR totals
    y_total_e = y_start_e - 0.06 - len(months) * row_h
    ax_eur.plot([0.01, 0.99], [y_total_e + row_h * 0.45, y_total_e + row_h * 0.45],
                color="#3a3a5a", linewidth=0.8, transform=ax_eur.transAxes)

    total_net_usd_all = total_net + total_wire_fees
    net_flow_total = total_deps + total_wds
    flow_color = pos_color if net_flow_total >= 0 else neg_color

    eur_totals = [
        ("TOTAL", "white"),
        (f"{total_pnl*to_eur:+,.2f}", pos_color if total_pnl >= 0 else neg_color),
        (f"{total_comm*to_eur:,.2f}", "#FF9800"),
        (f"{total_clear*to_eur:,.2f}", "#FF9800"),
        (f"{total_nfa*to_eur:,.2f}", "#FF9800"),
        (f"{total_wire_fees*to_eur:,.2f}", "#FF9800"),
        (f"{total_net_usd_all*to_eur:+,.2f}", pos_color if total_net_usd_all >= 0 else neg_color),
        (f"{total_deps:,.0f}", "#2196F3"),
        (f"{total_wds:+,.0f}", neg_color),
        (f"{net_flow_total:+,.0f}", flow_color),
    ]
    for ci, (txt, color) in enumerate(eur_totals):
        ax_eur.text(eur_x[ci], y_total_e, txt,
                    transform=ax_eur.transAxes,
                    color=color, fontsize=9, va="top", fontweight="bold",
                    family="monospace")

    # ── Save ─────────────────────────────────────────────────────────────────
    script_dir = os.path.dirname(os.path.abspath(__file__)) or os.getcwd()
    out_path = os.path.join(script_dir, f"phillipcapital_report_{year}.png")
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"\nReport saved: {out_path}")


# ── Main ─────────────────────────────────────────────────────────────────────
config = load_config()
multipliers = config["multipliers"]
print(f"Loaded {len(multipliers)} contract multipliers from config.json")

pdf_file = find_pdf()
print(f"\nParsing: {os.path.basename(pdf_file)}\n")
year = pick_year(pdf_file)
print()

# Derive EUR/USD rate from deposit data in the PDF
avg_rate = derive_eur_usd_rate(pdf_file, year)
if avg_rate:
    print(f"Derived avg EUR/USD rate from deposits: {avg_rate:.4f}")
    ans = input(f"Use this rate? (Y/n, or enter custom rate): ").strip()
    if ans.lower() == "n":
        eur_rate = float(input("Enter EUR/USD rate: ").strip())
    elif ans and ans.replace(".", "").replace(",", "").isdigit():
        eur_rate = float(ans.replace(",", "."))
    else:
        eur_rate = avg_rate
else:
    eur_rate = float(input("Enter EUR/USD rate (e.g. 1.08): ").strip())

print()
data = parse_trades(pdf_file, year, multipliers)
print_table(data, year, eur_rate, multipliers)
generate_report(data, year, pdf_file, eur_rate, multipliers)
