"""Fair billing app: enter customer + quantities, get a PDF bill saved locally
and a text bill ready to send to the customer on WhatsApp.

Run:  python app.py   then open http://localhost:5050
"""
import json
import os
import re
import subprocess
import threading
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from fpdf import FPDF
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

BASE = Path(__file__).parent
BILLS_DIR = Path(os.environ.get("BILLS_DIR", Path.home() / "Documents" / "Fair Bills"))
BILLS_DIR.mkdir(parents=True, exist_ok=True)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

SALES_XLSX = BILLS_DIR / "sales.xlsx"
STOCK_XLSX = BASE / "stock.xlsx"  # you fill in: Product | Stock (kg)
SALES_LOCK = threading.RLock()  # two counters may save at the same moment
PAYMENT_MODES = ("Cash", "UPI")
WA_CONFIG = BASE / "whatsapp_config.json"  # "mode": "web" (WhatsApp Web helper) or "api" (Meta Cloud API)
WA_HELPER = "http://127.0.0.1:3001"

app = Flask(__name__)


def load_config():
    with open(BASE / "products.json", encoding="utf-8") as f:
        return json.load(f)


def money(cfg, amount):
    return f"{cfg['currency']} {amount:,.2f}"


def fmt_qty(q):
    return f"{q:g}"


def whatsapp_number(mobile, cc):
    """Turn what was typed into an international number without '+'."""
    raw = mobile.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    digits = digits.lstrip("0")
    if len(digits) <= 10:
        return cc + digits
    return digits


def unique_path(name, mobile):
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "customer"
    safe_mobile = re.sub(r"\D", "", mobile)
    path = BILLS_DIR / f"{safe_name}_{safe_mobile}.pdf"
    n = 2
    while path.exists():  # same customer buying again: keep the earlier bill
        path = BILLS_DIR / f"{safe_name}_{safe_mobile}_{n}.pdf"
        n += 1
    return path


def r2(x):
    """Round to 2 decimals, half-up (matches the page's Math.round)."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_bill(cfg, items, discount_on):
    prices = {p["name"]: p["price"] for p in cfg["products"]}
    lines = []
    for it in items:
        name = it.get("name")
        try:
            qty = float(it.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if name in prices and qty > 0:
            lines.append({"name": name, "qty": qty, "price": prices[name],
                          "amount": r2(qty * prices[name])})
    subtotal = r2(sum(l["amount"] for l in lines))
    discount = r2(subtotal * cfg["discount_percent"] / 100) if discount_on else 0
    # final amount is rounded up to the next whole rupee
    total = float(Decimal(str(r2(subtotal - discount))).quantize(Decimal("1"), rounding=ROUND_CEILING))
    return lines, subtotal, discount, total


def round_off(subtotal, discount, total):
    return r2(total - r2(subtotal - discount))


def whole(cfg, amount):
    return f"{cfg['currency']} {amount:,.0f}"


def build_pdf(cfg, path, name, mobile, lines, subtotal, discount, total, when):
    pdf = FPDF(format="A5")
    pdf.add_font("DejaVu", "", FONT)
    pdf.add_font("DejaVu", "B", FONT_BOLD)
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    w = pdf.epw

    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(w, 9, "Bill / Receipt", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("DejaVu", "", 10)
    pdf.cell(w / 2, 6, f"Customer: {name}")
    pdf.cell(w / 2, 6, when.strftime("%d %b %Y, %H:%M"), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(w, 6, f"Mobile: {mobile}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    cols = [w * 0.42, w * 0.16, w * 0.20, w * 0.22]
    pdf.set_font("DejaVu", "B", 10)
    pdf.set_fill_color(235, 235, 235)
    for txt, cw, al in zip(["Item", f"Qty ({cfg['unit']})", f"Rate/{cfg['unit']}", "Amount"],
                           cols, ["L", "R", "R", "R"]):
        pdf.cell(cw, 7, txt, border="B", align=al, fill=True)
    pdf.ln()

    pdf.set_font("DejaVu", "", 10)
    for l in lines:
        pdf.cell(cols[0], 7, l["name"])
        pdf.cell(cols[1], 7, fmt_qty(l["qty"]), align="R")
        pdf.cell(cols[2], 7, money(cfg, l["price"]), align="R")
        pdf.cell(cols[3], 7, money(cfg, l["amount"]), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + w, pdf.get_y())
    pdf.ln(2)

    def total_row(label, value, bold=False):
        pdf.set_font("DejaVu", "B" if bold else "", 12 if bold else 10)
        pdf.cell(w - cols[3], 7, label, align="R")
        pdf.cell(cols[3], 7, value, align="R", new_x="LMARGIN", new_y="NEXT")

    total_row("Subtotal", money(cfg, subtotal))
    if discount:
        total_row(f"{cfg['discount_label']} ({cfg['discount_percent']}%)", "- " + money(cfg, discount))
    total_row("Round off", "+ " + money(cfg, round_off(subtotal, discount, total)))
    total_row("Total", whole(cfg, total), bold=True)

    pdf.ln(8)
    pdf.set_font("DejaVu", "", 9)
    pdf.cell(w, 5, "Thank you for shopping with us!", align="C")
    pdf.output(str(path))


def bill_text(cfg, name, mobile, lines, subtotal, discount, total, when):
    """The bill as a WhatsApp message (*text* is bold in WhatsApp)."""
    unit = cfg["unit"]
    out = ["*Bill / Receipt*",
           f"Name: {name}",
           f"Mobile: {mobile}",
           f"Date: {when.strftime('%d %b %Y, %H:%M')}",
           ""]
    for l in lines:
        out.append(f"{l['name']}: {fmt_qty(l['qty'])} {unit} x {money(cfg, l['price'])} = {money(cfg, l['amount'])}")
    out += ["", f"Subtotal: {money(cfg, subtotal)}"]
    if discount:
        out.append(f"{cfg['discount_label']} ({cfg['discount_percent']}%): -{money(cfg, discount)}")
    out += [f"Round off: +{money(cfg, round_off(subtotal, discount, total))}",
            f"*Total: {whole(cfg, total)}*", "", "Thank you for shopping with us!"]
    return "\n".join(out)


def qty_col(cfg, product):
    return f"{product} ({cfg['unit']})"


def sales_rows():
    """All rows of sales.xlsx as dicts keyed by column name."""
    if not SALES_XLSX.exists():
        return []
    wb = load_workbook(SALES_XLSX, read_only=True)
    rows = list(wb["Sales"].iter_rows(values_only=True))
    wb.close()
    return [dict(zip(rows[0], r)) for r in rows[1:]] if rows else []


def ensure_stock_file(cfg):
    """Create stock.xlsx with every product and an empty Stock column, for you to fill in."""
    if STOCK_XLSX.exists():
        return
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append(["Product", f"Stock ({cfg['unit']})"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for p in cfg["products"]:
        ws.append([p["name"], None])
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 12
    wb.save(STOCK_XLSX)


def stock_levels(cfg):
    """{product: kg brought} from stock.xlsx; products with an empty cell have no limit."""
    if not STOCK_XLSX.exists():
        return {}
    wb = load_workbook(STOCK_XLSX, read_only=True, data_only=True)
    names = {p["name"].lower(): p["name"] for p in cfg["products"]}
    levels = {}
    for row in wb.active.iter_rows(min_row=2, values_only=True):
        if row and row[0] and len(row) > 1 and isinstance(row[1], (int, float)):
            name = names.get(str(row[0]).strip().lower())
            if name:
                levels[name] = float(row[1])
    wb.close()
    return levels


def remaining_stock(cfg, rows=None):
    """{product: kg left} for products that have a stock level (stock - everything sold)."""
    rows = sales_rows() if rows is None else rows
    left = {}
    for name, stock in stock_levels(cfg).items():
        sold = sum(r.get(qty_col(cfg, name)) or 0 for r in rows)
        left[name] = round(max(stock - sold, 0), 3)
    return left


def update_stock_file(cfg, rows=None):
    """Write Sold and Left columns into stock.xlsx next to the Stock you entered."""
    if not STOCK_XLSX.exists():
        return
    rows = sales_rows() if rows is None else rows
    unit = cfg["unit"]
    wb = load_workbook(STOCK_XLSX)
    ws = wb.active
    for col, title in ((3, f"Sold ({unit})"), (4, f"Left ({unit})")):
        ws.cell(row=1, column=col, value=title).font = Font(bold=True)
        ws.column_dimensions["CD"[col - 3]].width = 12
    listed = {}
    for r in range(2, ws.max_row + 1):
        if ws.cell(row=r, column=1).value:
            listed[str(ws.cell(row=r, column=1).value).strip().lower()] = r
    for p in cfg["products"]:  # products added to the price list later get a row too
        if p["name"].lower() not in listed:
            ws.append([p["name"]])
            listed[p["name"].lower()] = ws.max_row
    for p in cfg["products"]:
        r = listed[p["name"].lower()]
        sold = round(sum(row.get(qty_col(cfg, p["name"])) or 0 for row in rows), 3)
        stock = ws.cell(row=r, column=2).value
        ws.cell(row=r, column=3, value=sold)
        ws.cell(row=r, column=4, value=round(stock - sold, 3) if isinstance(stock, (int, float)) else None)
    wb.save(STOCK_XLSX)


def day_summary(rows, day):
    out = {m: 0.0 for m in PAYMENT_MODES}
    bills = 0
    for r in rows:
        when = r.get("Date & time")
        if isinstance(when, datetime) and when.date() == day:
            bills += 1
            if r.get("Payment") in out:
                out[r["Payment"]] += r.get("Final amount") or 0
    return {"cash": r2(out["Cash"]), "upi": r2(out["UPI"]), "total": r2(out["Cash"] + out["UPI"]), "bills": bills}


def write_summary_sheet(wb, cfg, rows):
    """Rebuild the Summary sheet: cash/UPI per day and stock left per product."""
    if "Summary" in wb.sheetnames:
        del wb["Summary"]
    ws = wb.create_sheet("Summary")
    bold = Font(bold=True)
    ws.append(["Date", "Bills", "Cash", "UPI", "Total"])
    days = sorted({r["Date & time"].date() for r in rows if isinstance(r.get("Date & time"), datetime)})
    for d in days:
        t = day_summary(rows, d)
        ws.append([d.strftime("%d-%m-%Y"), t["bills"], t["cash"], t["upi"], t["total"]])
    ws.append([])
    ws.append(["Product", f"Stock ({cfg['unit']})", f"Sold ({cfg['unit']})", f"Left ({cfg['unit']})"])
    stock_header_row = ws.max_row
    levels = stock_levels(cfg)
    for p in cfg["products"]:
        sold = round(sum(r.get(qty_col(cfg, p["name"])) or 0 for r in rows), 3)
        stock = levels.get(p["name"])
        ws.append([p["name"], stock, sold, None if stock is None else round(stock - sold, 3)])
    for c in (*ws[1], *ws[stock_header_row]):
        c.font = bold
    for col, w in zip("ABCDE", (20, 10, 12, 12, 12)):
        ws.column_dimensions[col].width = w


def log_sale(cfg, name, mobile, payment, lines, subtotal, discount, total, when, bill_file):
    """Append one row per bill to sales.xlsx (one column per product). Returns the row number."""
    header = ["Date & time", "Name", "Mobile", *[qty_col(cfg, p["name"]) for p in cfg["products"]],
              "Total before discount", "Discount", "Round off", "Final amount", "Payment", "WhatsApp", "Bill file"]
    values = {"Date & time": when.replace(microsecond=0), "Name": name, "Mobile": mobile,
              **{qty_col(cfg, l["name"]): l["qty"] for l in lines},
              "Total before discount": subtotal, "Discount": discount,
              "Round off": round_off(subtotal, discount, total), "Final amount": total,
              "Payment": payment, "WhatsApp": "Sending…", "Bill file": bill_file}

    with SALES_LOCK:
        if SALES_XLSX.exists():
            wb = load_workbook(SALES_XLSX)
            ws = wb["Sales"]
            cols = [c.value for c in ws[1]]
            for h in header:  # columns added later (new product, Payment) go at the end
                if h not in cols:
                    cols.append(h)
                    ws.cell(row=1, column=len(cols), value=h).font = Font(bold=True)
        else:
            wb = Workbook()
            ws = wb.active
            ws.title = "Sales"
            ws.append(header)
            for c in ws[1]:
                c.font = Font(bold=True)
            ws.freeze_panes = "D2"
            ws.column_dimensions["A"].width = 18
            ws.column_dimensions["B"].width = 20
            ws.column_dimensions["C"].width = 14
            cols = header
        ws.append([values.get(h) for h in cols])
        row = ws.max_row
        for i, h in enumerate(cols, start=1):
            if h == "Date & time":
                ws.cell(row=row, column=i).number_format = "dd-mm-yyyy hh:mm"
            elif h in ("Total before discount", "Discount", "Round off", "Final amount"):
                ws.cell(row=row, column=i).number_format = "#,##0.00"
        rows = [dict(zip(cols, r)) for r in ws.iter_rows(min_row=2, values_only=True)]
        write_summary_sheet(wb, cfg, rows)
        wb.save(SALES_XLSX)
    return row


def set_whatsapp_status(row, status):
    with SALES_LOCK:
        wb = load_workbook(SALES_XLSX)
        ws = wb["Sales"]
        col = [c.value for c in ws[1]].index("WhatsApp") + 1
        ws.cell(row=row, column=col, value=status)
        wb.save(SALES_XLSX)


def wa_mode():
    if not WA_CONFIG.exists():
        return "web"
    with open(WA_CONFIG, encoding="utf-8") as f:
        return json.load(f).get("mode", "web")


def load_wa_config():
    """WhatsApp Cloud API settings, or None if not set up / not in api mode."""
    if not WA_CONFIG.exists():
        return None
    with open(WA_CONFIG, encoding="utf-8") as f:
        wa = json.load(f)
    if wa.get("mode", "web") != "api":
        return None
    filled = all(wa.get(k) and not str(wa[k]).startswith("PASTE") for k in ("token", "phone_number_id", "template"))
    return wa if filled else None


def one_line(text):
    # template variables may not contain newlines, tabs or 4+ spaces in a row
    return re.sub(r"\s+", " ", text).strip()


def send_whatsapp(wa, cfg, to, name, lines, subtotal, discount, total, when):
    """Send the bill as an approved template message. Returns (ok, message)."""
    items = " | ".join(f"{l['name']} {fmt_qty(l['qty'])} {cfg['unit']} = {money(cfg, l['amount'])}" for l in lines)
    disc = f"-{money(cfg, discount)} ({cfg['discount_percent']}% {cfg['discount_label'].lower()})" if discount else "None"
    params = [name, when.strftime("%d %b %Y, %H:%M"), items, money(cfg, subtotal), disc,
              f"{whole(cfg, total)} (incl. round off +{money(cfg, round_off(subtotal, discount, total))})"]
    body = {"messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": wa["template"], "language": {"code": wa.get("language", "en")},
                         "components": [{"type": "body", "parameters": [
                             {"type": "text", "text": one_line(p)} for p in params]}]}}
    url = f"https://graph.facebook.com/{wa.get('api_version', 'v25.0')}/{wa['phone_number_id']}/messages"
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {wa['token']}"}, json=body, timeout=20)
    except requests.RequestException as e:
        return False, f"No internet / WhatsApp unreachable ({e.__class__.__name__})"
    if r.ok:
        return True, "sent"
    try:
        err = r.json()["error"]
        return False, err.get("error_data", {}).get("details") or err.get("message", r.text)
    except (ValueError, KeyError):
        return False, r.text


def helper_status():
    try:
        return requests.get(f"{WA_HELPER}/status", timeout=3).json()
    except requests.RequestException:
        return {"state": "offline"}


def send_via_web(to, text):
    """Send through the WhatsApp Web helper (wa-sender/). Returns (ok, message)."""
    try:
        r = requests.post(f"{WA_HELPER}/send", json={"to": to, "text": text}, timeout=30).json()
    except requests.RequestException:
        return False, "WhatsApp helper is not running"
    return (True, "sent") if r.get("ok") else (False, r.get("error", "unknown error"))


def start_helper():
    """Start the WhatsApp Web helper next to this app (once)."""
    if wa_mode() != "web" or helper_status()["state"] != "offline":
        return None
    log = open(BASE / "wa-sender" / "helper.log", "a")
    return subprocess.Popen(["node", str(BASE / "wa-sender" / "index.js")], stdout=log, stderr=log)


@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/api/config")
def config():
    cfg = load_config()
    cfg["bills_dir"] = str(BILLS_DIR)
    cfg["whatsapp_mode"] = wa_mode()
    cfg["whatsapp_auto"] = wa_mode() == "web" or load_wa_config() is not None
    return jsonify(cfg)


@app.post("/api/bill")
def make_bill():
    cfg = load_config()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    mobile = (data.get("mobile") or "").strip()
    payment = data.get("payment")
    if not name or not re.search(r"\d{6,}", re.sub(r"\D", "", mobile)):
        return jsonify(error="Please enter a name and a valid mobile number."), 400
    if payment not in PAYMENT_MODES:
        return jsonify(error="Choose the payment mode: Cash or UPI."), 400

    lines, subtotal, discount, total = compute_bill(cfg, data.get("items", []), bool(data.get("discount", True)))
    if not lines:
        return jsonify(error="Enter a quantity for at least one product."), 400

    when = datetime.now()
    to = whatsapp_number(mobile, cfg["default_country_code"])
    text = bill_text(cfg, name, mobile, lines, subtotal, discount, total, when)

    # check stock and record the sale in one step, so two counters can't sell the same last kg
    with SALES_LOCK:
        left = remaining_stock(cfg)
        short = [f"{l['name']} (only {fmt_qty(left[l['name']])} {cfg['unit']} left)"
                 for l in lines if l["name"] in left and l["qty"] > left[l["name"]] + 1e-9]
        if short:
            return jsonify(error="Not enough stock: " + ", ".join(short), stock=left), 409
        path = unique_path(name, mobile)
        build_pdf(cfg, path, name, mobile, lines, subtotal, discount, total, when)
        result = {"file": path.name, "path": str(path), "total": total, "wa_number": to, "wa_text": text}
        try:
            row = log_sale(cfg, name, mobile, payment, lines, subtotal, discount, total, when, path.name)
            result["excel"] = str(SALES_XLSX)
        except Exception as e:  # never lose the bill because of the spreadsheet
            row = None
            result["excel_error"] = str(e)
        try:
            update_stock_file(cfg)
        except Exception as e:
            result["excel_error"] = f"stock.xlsx not updated: {e}"

    if wa_mode() == "web":
        ok, msg = send_via_web(to, text)
        result.update(wa_sent=ok, wa_message=msg)
    elif wa := load_wa_config():
        ok, msg = send_whatsapp(wa, cfg, to, name, lines, subtotal, discount, total, when)
        result.update(wa_sent=ok, wa_message=msg)

    if row:
        status = {True: "Sent", False: f"Failed: {result.get('wa_message')}"}.get(result.get("wa_sent"), "Manual")
        try:
            set_whatsapp_status(row, status)
        except Exception as e:
            result["excel_error"] = str(e)
    return jsonify(result)


@app.get("/api/summary")
def summary():
    """Today's cash/UPI totals and stock left, for the page."""
    cfg = load_config()
    try:
        with SALES_LOCK:  # don't read the files while another counter is saving a bill
            rows = sales_rows()
            stock = remaining_stock(cfg, rows)
        return jsonify(today=day_summary(rows, datetime.now().date()), stock=stock)
    except Exception as e:
        return jsonify(error=f"Could not read the Excel files: {e}"), 500


@app.get("/api/wa-status")
def wa_status():
    return jsonify(helper_status() if wa_mode() == "web" else {"state": "api"})


@app.post("/api/wa-logout")
def wa_logout():
    """Unlink the current WhatsApp account so another one can be linked."""
    try:
        requests.post(f"{WA_HELPER}/logout", timeout=5)
    except requests.RequestException:
        return jsonify(error="WhatsApp helper is not running"), 503
    return jsonify(ok=True)


@app.post("/api/open-folder")
def open_folder():
    # opens the bills folder in the file manager of the laptop running this app
    subprocess.Popen(["xdg-open", str(BILLS_DIR)])
    return jsonify(ok=True)


@app.get("/bills/<path:filename>")
def get_bill(filename):
    p = (BILLS_DIR / filename).resolve()
    if p.parent != BILLS_DIR.resolve() or not p.exists():
        abort(404)
    return send_file(p, mimetype="application/pdf", download_name=p.name,
                     as_attachment=request.args.get("download") == "1")


if __name__ == "__main__":
    import atexit
    import signal
    import sys
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so atexit also runs when killed
    ensure_stock_file(load_config())
    update_stock_file(load_config())
    helper = start_helper()
    if helper:
        atexit.register(helper.terminate)
    # host 0.0.0.0 lets a phone/tablet on the same Wi-Fi open the page too
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))
