# Bagula Mukhi — Quotation Maker

Web app for managing supplier price catalogs and generating professional quotations.

## Features
- Upload supplier PDF catalogs — AI extracts all items and prices automatically
- Multiple catalogs per supplier, update individual catalogs when prices change
- Build quotations: search items, set quantity, apply markup/discount per item or globally
- GST support with configurable rate
- Export quotations as PDF and Excel
- User login with admin-managed accounts

---

## Running Locally

**Requirements:** Python 3.9+

```bash
# 1. Clone the repo
git clone https://github.com/Ritvik-Anand/QuoteMaker.git
cd QuoteMaker

# 2. Copy and fill in environment variables
cp .env.example .env
# Edit .env and set your DEEPSEEK_API_KEY

# 3. Start the app
chmod +x start.sh
./start.sh
```

Open **http://localhost:5050** in your browser.

On first run, an admin account is created automatically. The username and password are printed in the terminal — log in and change the password immediately.

---

## Deploying to Railway (recommended for team access)

Railway gives everyone in your company a single URL to access the app.

### Step 1 — Create a Railway project

1. Go to [railway.app](https://railway.app) and sign up
2. Click **New Project → Deploy from GitHub repo**
3. Select **Ritvik-Anand/QuoteMaker**

### Step 2 — Add a persistent volume (for the database)

1. In your Railway project, click **+ New → Volume**
2. Mount path: `/data`
3. This keeps your data safe across deployments

### Step 3 — Set environment variables

In Railway → your service → **Variables**, add:

| Variable | Value |
|---|---|
| `DEEPSEEK_API_KEY` | Your DeepSeek API key |
| `SECRET_KEY` | A long random string (run `python3 -c "import secrets; print(secrets.token_hex(32))"`) |
| `DATABASE_PATH` | `/data/quotation_maker.db` |
| `ADMIN_USERNAME` | `admin` (or your preferred username) |
| `ADMIN_PASSWORD` | A strong password for the first admin account |

### Step 4 — Deploy

Railway auto-deploys on every push to `main`. Your app will be live at a URL like `https://quotemaker-production.up.railway.app`.

Share that URL with your employees. Each employee gets their own login — create accounts from the **Manage Users** page (admin only).

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `DEEPSEEK_API_KEY` | Yes | DeepSeek API key for PDF parsing |
| `SECRET_KEY` | Yes (prod) | Secret for session cookies |
| `DATABASE_PATH` | No | Path to SQLite file (default: `./quotation_maker.db`) |
| `ADMIN_USERNAME` | No | First admin username (default: `admin`) |
| `ADMIN_PASSWORD` | No | First admin password (auto-generated if not set) |
| `PORT` | No | Port to listen on (set automatically by Railway) |

---

## Tech Stack

- **Backend:** Python / Flask
- **Database:** SQLite (WAL mode)
- **PDF parsing:** pdfplumber + DeepSeek API
- **PDF export:** ReportLab
- **Excel export:** openpyxl
- **Server:** Gunicorn
