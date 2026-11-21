# Fair Billing

A small billing web app for selling at a stall or fair. For each customer you enter the
name, mobile number and quantities; one click then:

- saves a **PDF bill** (`<Name>_<mobile>.pdf`),
- **sends the bill on WhatsApp** to the customer,
- adds a row to **`sales.xlsx`** (quantities, totals, payment mode),
- updates the **stock** left per product.

Features: price list with English + Hindi names, quantities in kg, 10 % inauguration
discount (on by default, can be switched off), final amount rounded up to the next rupee,
Cash/UPI payment mode with daily totals, stock limits with **Sold out**, and several
phones/laptops can bill at the same time.

## Requirements

- **Linux** (tested on Ubuntu) — see *Other operating systems* below
- **Python 3.10+** with the packages in `requirements.txt`
- **Node.js 18+** and **Google Chrome** — only for sending WhatsApp automatically via WhatsApp Web
- the **DejaVu Sans** font (`fonts-dejavu-core`, installed on Ubuntu by default) for the ₹ sign in PDFs

## Install

```bash
git clone <this repo's URL>
cd fair-billing

pip install -r requirements.txt

# WhatsApp Web sender (skip if you won't send automatically via WhatsApp Web)
cd wa-sender
PUPPETEER_SKIP_DOWNLOAD=1 npm install    # uses your installed Chrome, no extra download
cd ..

cp whatsapp_config.example.json whatsapp_config.json
```

## Run

```bash
python app.py
```

Open **http://localhost:5050**. Phones and other laptops on the same Wi-Fi/hotspot can open
`http://<laptop-ip>:5050` (find the IP with `hostname -I`). Everything is saved on the laptop
running the app.

Stop it with **Ctrl + C** in that terminal (or `pkill -f fair-billing/app.py`). The WhatsApp
helper stops with it.

## Configuration

| File | What it's for |
|---|---|
| `products.json` | Product names (+ Hindi), price per kg, currency, discount %, default country code for mobile numbers. Refresh the page after editing. |
| `stock.xlsx` | Created on first start. Fill in **Stock (kg)** = how much you brought (raise it if you restock); empty = no limit. The app fills in **Sold** and **Left**. |
| `whatsapp_config.json` | How bills are sent on WhatsApp (see below). **Private — never commit it.** |
| `BILLS_DIR` env var | Where PDFs and `sales.xlsx` go. Default: `~/Documents/Fair Bills/`. |

## Output files (in `~/Documents/Fair Bills/`)

- **PDF bills** – `<Name>_<mobile>.pdf`; a repeat customer gets `_2`, `_3`, …
- **`sales.xlsx`** – one row per bill: date, name, mobile, kg of each product, total before
  discount, discount, round off, final amount, **Payment** (Cash/UPI), WhatsApp status, PDF name.
  The **Summary** sheet has Cash/UPI totals per day and stock sold/left per product.
  Don't keep it open in Excel while billing (saving from Excel would overwrite new rows).
  Delete test rows before going live — they count as sold stock.

## WhatsApp

Set `"mode"` in `whatsapp_config.json`:

| Mode | How it works |
|---|---|
| `"web"` (default) | Sends automatically through **WhatsApp Web** from a normal WhatsApp account linked like any other device. Free, full multi-line bill. |
| `"api"` | Sends through Meta's official **WhatsApp Cloud API**. Reliable, small fee per message, needs setup (below). |
| `"manual"` | No automatic sending: after saving, a button opens WhatsApp with the bill typed in; you press Send. |

In every mode the bill, Excel and stock are saved even if WhatsApp sending fails, and the
page then shows the manual send button as a backup.

### WhatsApp Web mode

1. Start the app. The page shows a QR code.
2. On the sending phone: WhatsApp → ⋮ → **Linked devices** → **Link a device**, scan the QR.
3. The bar turns green ("WhatsApp connected"). The login is remembered in
   `wa-sender/.wwebjs_auth/`, so you scan only once. The phone keeps working normally.

**Change WhatsApp account** (link in the status bar) unlinks the current number and shows a
new QR code. Helper logs: `wa-sender/helper.log`. Set `"show_browser": true` to run the
helper's Chrome as a visible window instead of hidden.

**Limitations — read before relying on it:**
- Automating WhatsApp Web is against WhatsApp's terms. Use a number you can afford to lose
  and don't send in bulk.
- WhatsApp is rolling out a mandatory **passkey check for linking new devices** (mainly
  Business accounts). Accounts that have it can't be linked by automated clients like this
  one — the phone shows "Can't link new devices" or the link never finishes. There is no known
  workaround ([details](https://github.com/rmyndharis/OpenWA/issues/560)); use `"api"` or
  `"manual"` mode for such accounts. Don't retry linking over and over.

### Meta Cloud API setup (`"mode": "api"`)

1. **Number** – get a SIM/number that is *not* on WhatsApp (or delete WhatsApp from it).
   It must be able to receive an SMS or call for the verification code.
2. **Meta app** – go to https://developers.facebook.com → My Apps → Create app →
   type *Business* → add the **WhatsApp** product. This also creates a Meta Business portfolio.
3. **Add your number** – WhatsApp → API Setup → *Add phone number*, verify it with the code.
   Copy its **Phone number ID** (not the phone number itself).
4. **Payment method** – WhatsApp Manager → add a card. Each bill message is a small
   "utility" charge from Meta (check current India pricing on Meta's pricing page).
5. **Permanent token** – business.facebook.com → Settings → Users → **System users** → Add
   (Admin) → *Assign assets*: your app + WhatsApp account (full control) → *Generate token*
   with permissions `whatsapp_business_messaging` and `whatsapp_business_management`.
   (The token on the API Setup page expires after 24 h — don't use it for real billing.)
6. **Message template** – WhatsApp Manager → Message templates → Create:
   - Category: **Utility** · Name: `bill_receipt` · Language: **English**
   - Body (copy exactly):

     ```
     Bill / Receipt

     Name: {{1}}
     Date: {{2}}

     Items: {{3}}

     Subtotal: {{4}}
     Discount: {{5}}
     *Total: {{6}}*

     Thank you for shopping with us!
     ```
   - Sample values: `Ravi Kumar` · `22 Nov 2026, 11:30` ·
     `Haldi 0.5 kg = ₹ 210.00 | Basmati Rice 2 kg = ₹ 320.00` · `₹ 530.00` ·
     `-₹ 53.00 (10% inauguration discount)` · `₹ 477.00`
   - Submit and wait for **Approved** (usually minutes, sometimes up to a day).
7. **Config** – in `whatsapp_config.json` set `"mode": "api"`, paste the token
   and Phone number ID, restart `python app.py`.
8. **Test** – generate a bill for your own personal number before going live.

Keep `whatsapp_config.json` private — the token can send messages as your business.

Note: with the API, item lines arrive on one line separated by `|` (WhatsApp template
variables can't contain line breaks), and the number registered for the API can no longer
be used in the WhatsApp app.

## Other operating systems

The app is written for Linux. On macOS/Windows:
- set `CHROME_PATH` to your Chrome executable before starting (WhatsApp Web mode),
- change the `FONT` / `FONT_BOLD` paths at the top of `app.py` to a TTF font with the ₹ sign,
- "Open folder" uses `xdg-open` (Linux only).

## Troubleshooting

- **Page doesn't open on a phone** – same Wi-Fi as the laptop? Use `http://` and `:5050`.
  The laptop's firewall may need to allow port 5050.
- **WhatsApp not connecting / no QR code** – the status bar shows the reason. Common ones:
  Chrome not installed (install Google Chrome, or set `CHROME_PATH`), Node.js missing, or
  `npm install` not run in `wa-sender`. Fix it and restart the app. Details: `wa-sender/helper.log`.
- **"Not enough stock"** – raise the number in `stock.xlsx` (Stock column) and save.
- **Excel not updated warning** – close `sales.xlsx` / `stock.xlsx` in Excel and try again.
