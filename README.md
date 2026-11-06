# SpendSmart AI

SpendSmart AI is a responsive Flask and SQLite personal finance app. It includes account registration and login, private income and expense records, month and year navigation, budgets, charts, savings goals, Ollama grounded insights, and downloadable PDF reports.

## Requirements

- Python 3.10 or newer
- Ollama (optional for local AI features)

## Install and run on Windows

Open PowerShell in this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `SECRET_KEY` to a long random value. Then launch the site:

```powershell
python app.py
```

Open <http://127.0.0.1:5000> and create an account. SQLite tables and the default category list are created automatically in `instance/spendsmart.sqlite3` on first launch.

## Enable local AI

Install Ollama from <https://ollama.com>, then in a separate terminal run:

```powershell
ollama pull llama3.2
ollama serve
```

Set `OLLAMA_MODEL` in `.env` to any model you have pulled. The application calls the Ollama `/api/chat` endpoint at `OLLAMA_HOST`. AI routes receive monthly totals and category sums queried for the signed-in account; the assistant is instructed to use only those verified figures. AI features return an availability message when Ollama is stopped. The PDF report requests its own monthly summary and may take a few seconds while the model responds.

## Features

- Separate account data with password hashing and user-scoped transaction queries
- Income and expense entry with date, category, payment method, and description
- Edit and delete transaction entries; browse any month and year without deleting old records
- Monthly and category budgets, usage indicators, and warnings
- Interactive doughnut, stacked bar, and trend charts (Chart.js loaded from jsDelivr)
- Local Ollama insights and finance Q&A
- Monthly PDF reports with actual totals, category breakdown, budget performance, and an AI summary
- Savings goal tracking, profile name settings, and persistent light/dark appearance preference

## Notes

- For deployment, use a production WSGI server, HTTPS, a strong `SECRET_KEY`, and an appropriately protected database file. Do not enable Flask debug mode on a public interface.
- Chart.js and the font are loaded from CDNs, so charts and custom typography need internet access in the browser. The rest of the application runs locally.
- SQLite stores amounts as numeric values and displays Indian Rupees. Monthly views derive from transaction dates; no month-specific tables are created.
- This app is an educational finance organizer, not financial advice.
