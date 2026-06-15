# Bagula Mukhi — Quotation Maker

Web app for managing supplier price catalogs and generating professional quotations.

## Features
- Upload supplier PDF catalogs — AI extracts all items and prices automatically
- Multiple catalogs per supplier; update one catalog without touching others
- Build quotations: search items, set quantity, apply markup/discount per item or globally
- GST support with configurable rate
- Export quotations as PDF and Excel
- User login with admin-managed accounts

---

## Running Locally

**Requirements:** Python 3.9+

```bash
git clone https://github.com/Ritvik-Anand/QuoteMaker.git
cd QuoteMaker

cp .env.example .env
# Edit .env — at minimum set DEEPSEEK_API_KEY

chmod +x start.sh
./start.sh
```

Open **http://localhost:5050**. The first admin username and password are printed in the terminal on first run.

---

## Deploying to Vercel (team access)

Vercel gives everyone in your company a single URL. It needs a PostgreSQL database — we use **Neon** (free tier, provided directly in Vercel's dashboard).

### Step 1 — Deploy from GitHub

1. Go to [vercel.com](https://vercel.com) → **Add New Project**
2. Import `Ritvik-Anand/QuoteMaker`
3. Leave all build settings as-is — Vercel detects `vercel.json` automatically
4. Click **Deploy**

### Step 2 — Add a Postgres database

1. In your Vercel project → **Storage** tab → **Create Database → Postgres (Neon)**
2. Follow the prompts — Vercel automatically adds `DATABASE_URL` to your environment

### Step 3 — Set environment variables

In Vercel → your project → **Settings → Environment Variables**, add:

| Variable | Value |
|---|---|
| `DEEPSEEK_API_KEY` | Your DeepSeek API key from [platform.deepseek.com](https://platform.deepseek.com) |
| `SECRET_KEY` | Run `python3 -c "import secrets; print(secrets.token_hex(32))"` and paste the output |
| `ADMIN_USERNAME` | `admin` (or your preferred username) |
| `ADMIN_PASSWORD` | A strong password for the first login |

> `DATABASE_URL` is added automatically by Vercel Postgres — you don't need to set it manually.

### Step 4 — Redeploy

After adding environment variables, go to **Deployments → Redeploy** (top-right menu on the latest deployment).

Your app is now live at `https://your-project.vercel.app`. Share that URL with your employees and create their accounts from **Manage Users** (admin only).

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `DEEPSEEK_API_KEY` | Yes | DeepSeek API key for PDF parsing |
| `SECRET_KEY` | Yes (prod) | Secret for session security |
| `DATABASE_URL` | Yes (Vercel) | PostgreSQL connection string — set automatically by Vercel Postgres |
| `DATABASE_PATH` | No | SQLite file path for local dev (default: `./quotation_maker.db`) |
| `ADMIN_USERNAME` | No | First admin username (default: `admin`) |
| `ADMIN_PASSWORD` | No | First admin password (auto-generated if not set) |

---

## Tech Stack

- **Backend:** Python / Flask
- **Database:** PostgreSQL on Vercel (Neon) · SQLite locally
- **PDF parsing:** pdfplumber + DeepSeek API
- **PDF export:** ReportLab · **Excel:** openpyxl
- **Server:** Gunicorn (local) · Vercel serverless (production)
