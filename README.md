# PhillipCapital Futures Trade Report Parser

Parses PhillipCapital futures trade PDF exports (monthly combined files containing multiple months are supported) and summarizes buy/sell totals, net P&L, and commissions per month.

## Getting Started (First Time Setup)

### 1. Install Python

If you don't have Python installed:

- Go to https://www.python.org/downloads/
- Download and run the installer
- **Important:** Check the box that says **"Add Python to PATH"** during installation
- Click "Install Now"

To verify it worked, open a terminal (Command Prompt or PowerShell on Windows, Terminal on Mac) and type:

```bash
python --version
```

You should see something like `Python 3.12.x`.

### 2. Download this project

Click the green **Code** button on GitHub, then **Download ZIP**. Extract the ZIP to a folder on your computer.

Or if you have Git installed:

```bash
git clone https://github.com/your-username/phillipcapital-report-parser.git
cd phillipcapital-report-parser
```

### 3. Install dependencies

Open a terminal in the project folder and run:

```bash
pip install -r requirements.txt
```

### 4. Run the parser

1. Place your combined monthly PDF export(s) in the project folder.
2. Run the script:
   ```bash
   python parser.py
   ```

The script will automatically find PDF files in the current directory:

- **1 PDF found** — uses it automatically
- **Multiple PDFs found** — shows a numbered list for you to pick from
- **No PDFs found** — prompts you to enter a file path or directory

## Output

The script prints a per-month summary table:

```
MONTH | BUYS (price*qty) | SELLS (price*qty) | NET P&L | COMMISSION
```

- **Buys/Sells** — aggregated `price * quantity` for long and short trades
- **Net P&L** — `(sells - buys) * contract multiplier` (default: $2 for MNQ; adjust for MES $5, etc.)
- **Commission** — total commission charges extracted from the report

## Supported Contracts

Currently parses CME Micro futures (e.g. MNQ). The contract multiplier in the P&L calculation defaults to $2 (MNQ) — adjust the multiplier in the script for other products.

## Notes

- The parser detects month boundaries from `RUN DATE` fields in the PDF.
- Designed for the specific PDF layout exported by PhillipCapital (PVMH combined reports).
