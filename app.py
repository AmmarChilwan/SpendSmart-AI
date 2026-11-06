"""SpendSmart AI — Flask personal finance application."""
from __future__ import annotations

import io
import json
import os
import secrets
import sqlite3
from calendar import month_name
from datetime import date, datetime
from functools import wraps
from urllib.error import URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import pandas as pd
except ImportError:  # Pandas is used for optional report/data workflows.
    pd = None

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
except ImportError:
    colors = letter = getSampleStyleSheet = Paragraph = SimpleDocTemplate = Spacer = Table = TableStyle = None

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "instance", "spendsmart.sqlite3"))
CATEGORIES = ["Rent", "Food", "Groceries", "Shopping", "Transportation", "Education", "Electricity", "Internet", "Entertainment", "Medical", "Bills", "Other"]
PAYMENTS = ["UPI", "Cash", "Card", "Bank transfer", "Wallet", "Other"]

app = Flask(__name__)
app.config.update(SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL, amount REAL NOT NULL CHECK(amount > 0), category_id INTEGER NOT NULL REFERENCES categories(id), type TEXT NOT NULL DEFAULT 'expense' CHECK(type IN ('expense','income')), txn_date TEXT NOT NULL, payment_method TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_txn_user_date ON transactions(user_id, txn_date);
        CREATE TABLE IF NOT EXISTS monthly_budgets (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, year INTEGER NOT NULL, month INTEGER NOT NULL, amount REAL NOT NULL CHECK(amount >= 0), UNIQUE(user_id, year, month));
        CREATE TABLE IF NOT EXISTS category_budgets (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, year INTEGER NOT NULL, month INTEGER NOT NULL, category_id INTEGER NOT NULL REFERENCES categories(id), amount REAL NOT NULL CHECK(amount >= 0), UNIQUE(user_id, year, month, category_id));
        CREATE TABLE IF NOT EXISTS savings_goals (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, target REAL NOT NULL CHECK(target > 0), saved REAL NOT NULL DEFAULT 0 CHECK(saved >= 0), due_date TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        db.executemany("INSERT OR IGNORE INTO categories(name) VALUES (?)", [(name,) for name in CATEGORIES])


init_db()


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "info")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return {"current_user": session.get("name"), "categories": CATEGORIES, "payments": PAYMENTS, "today": date.today().isoformat(), "month_name": {i: month_name[i] for i in range(1,13)}}


@app.template_filter("money")
def money(value):
    try:
        return "₹" + format(float(value or 0), ",.2f").rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "₹0"


def month_args():
    try:
        y, m = int(request.args.get("year", date.today().year)), int(request.args.get("month", date.today().month))
        if not (1 <= m <= 12 and 1900 <= y <= 2200):
            raise ValueError
        return y, m
    except (ValueError, TypeError):
        return date.today().year, date.today().month


def month_data(user_id, year, month):
    start = f"{year:04}-{month:02}-01"
    end = f"{year+1:04}-01-01" if month == 12 else f"{year:04}-{month+1:02}-01"
    with connect() as db:
        txns = db.execute("SELECT t.*, c.name category FROM transactions t JOIN categories c ON c.id=t.category_id WHERE t.user_id=? AND t.txn_date>=? AND t.txn_date<? ORDER BY t.txn_date DESC,t.id DESC", (user_id, start, end)).fetchall()
        budget = db.execute("SELECT amount FROM monthly_budgets WHERE user_id=? AND year=? AND month=?", (user_id, year, month)).fetchone()
        cat_budgets = db.execute("SELECT c.name,b.amount FROM category_budgets b JOIN categories c ON c.id=b.category_id WHERE b.user_id=? AND b.year=? AND b.month=?", (user_id, year, month)).fetchall()
    income = sum(t["amount"] for t in txns if t["type"] == "income")
    expenses = sum(t["amount"] for t in txns if t["type"] == "expense")
    by_cat = {}
    if pd is not None and txns:
        frame = pd.DataFrame([dict(t) for t in txns])
        by_cat = {name: float(value) for name, value in frame.loc[frame["type"] == "expense"].groupby("category")["amount"].sum().to_dict().items()}
    else:
        for t in txns:
            if t["type"] == "expense":
                by_cat[t["category"]] = by_cat.get(t["category"], 0) + t["amount"]
    return {"txns": txns, "income": income, "expenses": expenses, "balance": income-expenses, "budget": budget["amount"] if budget else 0, "by_cat": by_cat, "cat_budgets": {r["name"]: r["amount"] for r in cat_budgets}}


def safe_month_url(endpoint, y, m, delta=0):
    idx = y * 12 + m - 1 + delta
    ny, nm = divmod(idx, 12)
    return url_for(endpoint, year=ny, month=nm+1)


@app.route("/")
def home():
    return redirect(url_for("dashboard" if session.get("user_id") else "login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name, email, password = request.form.get("name", "").strip(), request.form.get("email", "").strip().lower(), request.form.get("password", "")
        if len(name) < 2 or "@" not in email or len(password) < 8:
            flash("Enter your name, a valid email, and a password of at least 8 characters.", "error")
        else:
            try:
                with connect() as db:
                    cur = db.execute("INSERT INTO users(name,email,password_hash) VALUES (?,?,?)", (name, email, generate_password_hash(password)))
                session.update(user_id=cur.lastrowid, name=name, email=email)
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "error")
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        with connect() as db:
            user = db.execute("SELECT * FROM users WHERE email=?", (request.form.get("email", "").strip().lower(),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear(); session.update(user_id=user["id"], name=user["name"], email=user["email"])
            return redirect(url_for("dashboard"))
        flash("Email or password is incorrect.", "error")
    return render_template("auth.html", mode="login")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    y, m = month_args(); data = month_data(session["user_id"], y, m)
    with connect() as db:
        recent = db.execute("SELECT t.*,c.name category FROM transactions t JOIN categories c ON c.id=t.category_id WHERE user_id=? ORDER BY txn_date DESC,id DESC LIMIT 6", (session["user_id"],)).fetchall()
        trend = db.execute("SELECT substr(txn_date,1,7) ym, SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) expenses FROM transactions WHERE user_id=? AND txn_date>=date(?, '-5 months','start of month') AND txn_date<? GROUP BY ym ORDER BY ym", (session["user_id"], f"{y:04}-{m:02}-01", f"{y+1:04}-01-01" if m==12 else f"{y:04}-{m+1:02}-01")).fetchall()
        goal = db.execute("SELECT COALESCE(SUM(saved),0) saved FROM savings_goals WHERE user_id=?", (session["user_id"],)).fetchone()["saved"]
    return render_template("dashboard.html", data=data, recent=recent, year=y, month=m, month_label=f"{month_name[m]} {y}", savings=goal, trend=list(trend))


@app.route("/expenses")
@login_required
def expenses():
    y,m=month_args(); data=month_data(session["user_id"],y,m)
    # Keep the new entry date inside the selected month, even when browsing history.
    import calendar
    chosen_day=min(date.today().day, calendar.monthrange(y,m)[1]) if (y,m)==(date.today().year,date.today().month) else 1
    default_date=date(y,m,chosen_day).isoformat()
    return render_template("expenses.html", data=data, year=y, month=m, default_date=default_date, month_label=f"{month_name[m]} {y}", prev=safe_month_url("expenses",y,m,-1), next=safe_month_url("expenses",y,m,1))


@app.route("/transactions", methods=["POST"])
@login_required
def save_transaction():
    try:
        title=request.form.get("title", "").strip(); amount=float(request.form.get("amount", "")); category=request.form.get("category", ""); txn_date=request.form.get("date", ""); payment=request.form.get("payment_method", "Other"); kind=request.form.get("type", "expense")
        date.fromisoformat(txn_date)
        if not title or amount <= 0 or category not in CATEGORIES or payment not in PAYMENTS or kind not in ("expense", "income"):
            raise ValueError
    except (ValueError, TypeError):
        flash("Check the title, amount, category, date, and payment method.", "error")
        return redirect(request.referrer or url_for("expenses"))
    txn_id=request.form.get("transaction_id")
    with connect() as db:
        cat_id=db.execute("SELECT id FROM categories WHERE name=?", (category,)).fetchone()["id"]
        if txn_id:
            cur=db.execute("UPDATE transactions SET title=?,amount=?,category_id=?,type=?,txn_date=?,payment_method=?,description=? WHERE id=? AND user_id=?", (title,amount,cat_id,kind,txn_date,payment,request.form.get("description", "").strip(),txn_id,session["user_id"]))
            if cur.rowcount==0: abort(404)
        else:
            db.execute("INSERT INTO transactions(user_id,title,amount,category_id,type,txn_date,payment_method,description) VALUES (?,?,?,?,?,?,?,?)", (session["user_id"],title,amount,cat_id,kind,txn_date,payment,request.form.get("description", "").strip()))
    flash("Transaction saved.", "success")
    return redirect(url_for("expenses", year=int(txn_date[:4]), month=int(txn_date[5:7])))


@app.route("/transactions/<int:txn_id>/delete", methods=["POST"])
@login_required
def delete_transaction(txn_id):
    with connect() as db:
        cur=db.execute("DELETE FROM transactions WHERE id=? AND user_id=?", (txn_id,session["user_id"]))
    if cur.rowcount==0: abort(404)
    flash("Transaction deleted.", "success")
    return redirect(request.referrer or url_for("expenses"))


@app.route("/budget", methods=["GET", "POST"])
@login_required
def budget():
    y,m=month_args()
    if request.method=="POST":
        try:
            amount=float(request.form.get("amount",0)); month=int(request.form.get("month")); year=int(request.form.get("year"))
            if amount < 0 or not 1<=month<=12: raise ValueError
        except ValueError:
            flash("Enter a valid monthly budget.","error"); return redirect(url_for("budget"))
        with connect() as db:
            db.execute("INSERT INTO monthly_budgets(user_id,year,month,amount) VALUES (?,?,?,?) ON CONFLICT(user_id,year,month) DO UPDATE SET amount=excluded.amount", (session["user_id"],year,month,amount))
            for cat in CATEGORIES:
                raw=request.form.get("cat_"+cat)
                if raw is not None and raw.strip():
                    db.execute("INSERT INTO category_budgets(user_id,year,month,category_id,amount) VALUES (?,?,?,?,?) ON CONFLICT(user_id,year,month,category_id) DO UPDATE SET amount=excluded.amount", (session["user_id"],year,month,db.execute("SELECT id FROM categories WHERE name=?",(cat,)).fetchone()["id"],max(0,float(raw))))
        flash("Budget plan updated.","success"); return redirect(url_for("budget",year=year,month=month))
    data=month_data(session["user_id"],y,m)
    return render_template("budget.html",data=data,year=y,month=m,month_label=f"{month_name[m]} {y}")


@app.route("/analytics")
@login_required
def analytics():
    y,m=month_args(); data=month_data(session["user_id"],y,m)
    with connect() as db:
        rows=db.execute("SELECT substr(txn_date,1,7) ym,c.name category,SUM(t.amount) amount FROM transactions t JOIN categories c ON c.id=t.category_id WHERE t.user_id=? AND t.type='expense' AND txn_date>=date(?, '-5 months','start of month') AND txn_date<? GROUP BY ym,c.name ORDER BY ym", (session["user_id"],f"{y:04}-{m:02}-01",f"{y+1:04}-01-01" if m==12 else f"{y:04}-{m+1:02}-01")).fetchall()
    return render_template("analytics.html",data=data,year=y,month=m,month_label=f"{month_name[m]} {y}",chart_rows=[dict(r) for r in rows])


def actual_context(uid, year, month):
    data=month_data(uid,year,month)
    prior_year,prior_month=(year-1,12) if month==1 else (year,month-1)
    prior=month_data(uid,prior_year,prior_month)
    return {"month":f"{month_name[month]} {year}","income":round(data["income"],2),"expenses":round(data["expenses"],2),"budget":round(data["budget"],2),"balance":round(data["balance"],2),"categories":{k:round(v,2) for k,v in data["by_cat"].items()},"transactions":[{"date":t["txn_date"],"title":t["title"],"amount":round(t["amount"],2),"category":t["category"],"type":t["type"],"payment_method":t["payment_method"],"description":t["description"]} for t in data["txns"]],"previous_month":f"{month_name[prior_month]} {prior_year}","previous_expenses":round(prior["expenses"],2),"previous_categories":{k:round(v,2) for k,v in prior["by_cat"].items()}}


def ollama(prompt, context):
    host=os.getenv("OLLAMA_HOST","http://localhost:11434").rstrip("/"); model=os.getenv("OLLAMA_MODEL","llama3.2")
    payload={"model":model,"stream":False,"messages":[{"role":"system","content":"You are SpendSmart AI, a careful personal finance assistant. Use only the provided verified data. Never invent or infer transaction amounts. If a detail is missing, say so. Give practical, nonjudgmental guidance."},{"role":"user","content":f"Verified database data (JSON): {json.dumps(context,ensure_ascii=False)}\n\nQuestion: {prompt}"}]}
    req=Request(host+"/api/chat",data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"},method="POST")
    try:
        with urlopen(req,timeout=45) as resp: return json.loads(resp.read().decode())["message"]["content"]
    except (URLError, TimeoutError, KeyError, ValueError, OSError) as exc:
        return "Ollama is unavailable right now. Start Ollama and pull the configured model ("+model+"). Your recorded data is safe; try again shortly."


@app.route("/insights",methods=["GET","POST"])
@login_required
def insights():
    y,m=month_args(); context=actual_context(session["user_id"],y,m); answer=None
    if request.method=="POST": answer=ollama("Summarize spending behaviour, highest category, potential discretionary spending, budget status, and compare with the previous month. Clearly distinguish observations from suggestions.",context)
    return render_template("insights.html",year=y,month=m,month_label=context["month"],context=context,answer=answer)


@app.route("/assistant",methods=["GET","POST"])
@login_required
def assistant():
    y,m=month_args(); context=actual_context(session["user_id"],y,m); answer=None; question=""
    if request.method=="POST":
        question=request.form.get("question","").strip()
        if question: answer=ollama(question,context)
    return render_template("assistant.html",year=y,month=m,month_label=context["month"],context=context,answer=answer,question=question)


@app.route("/reports")
@login_required
def reports():
    y,m=month_args(); data=month_data(session["user_id"],y,m); context=actual_context(session["user_id"],y,m)
    with connect() as db: goals=db.execute("SELECT * FROM savings_goals WHERE user_id=? ORDER BY id DESC",(session["user_id"],)).fetchall()
    return render_template("reports.html",data=data,context=context,year=y,month=m,month_label=context["month"],goals=goals)


@app.route("/goals", methods=["POST"])
@login_required
def add_goal():
    try:
        name=request.form.get("name", "").strip(); target=float(request.form.get("target", "")); saved=float(request.form.get("saved", 0) or 0)
        if not name or target<=0 or saved<0: raise ValueError
    except ValueError:
        flash("Enter a goal name and valid target and saved amounts.", "error")
        return redirect(url_for("reports"))
    with connect() as db: db.execute("INSERT INTO savings_goals(user_id,name,target,saved) VALUES (?,?,?,?)", (session["user_id"],name,target,saved))
    flash("Savings goal added.", "success")
    return redirect(url_for("reports"))


@app.route("/goals/<int:goal_id>/delete", methods=["POST"])
@login_required
def delete_goal(goal_id):
    with connect() as db: cur=db.execute("DELETE FROM savings_goals WHERE id=? AND user_id=?", (goal_id,session["user_id"]))
    if cur.rowcount==0: abort(404)
    flash("Savings goal deleted.", "success")
    return redirect(url_for("reports"))


@app.route("/reports.pdf")
@login_required
def report_pdf():
    if not SimpleDocTemplate: abort(503,"Install reportlab to generate PDF reports.")
    y,m=month_args(); data=month_data(session["user_id"],y,m); context=actual_context(session["user_id"],y,m)
    buffer=io.BytesIO(); doc=SimpleDocTemplate(buffer,pagesize=letter); styles=getSampleStyleSheet(); story=[Paragraph(f"SpendSmart AI — {context['month']} report",styles["Title"]),Spacer(1,16)]
    rows=[["Metric","Amount"],["Income",f"₹{data['income']:,.2f}"],["Expenses",f"₹{data['expenses']:,.2f}"],["Remaining balance",f"₹{data['balance']:,.2f}"],["Monthly budget",f"₹{data['budget']:,.2f}"],["Budget remaining",f"₹{data['budget']-data['expenses']:,.2f}"]]
    rows += [["Category", "Spent"]]+[[name,f"₹{value:,.2f}"] for name,value in sorted(data["by_cat"].items(),key=lambda p:-p[1])]
    table=Table(rows,hAlign="LEFT"); table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#142b52")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.4,colors.HexColor("#d9e1ef")),("PADDING",(0,0),(-1,-1),8)])); story.append(table)
    if data["budget"]: story += [Spacer(1,14),Paragraph(f"Budget usage: {data['expenses']/data['budget']*100:.1f}%",styles["BodyText"])]
    summary=ollama("Write a concise monthly report summary including spending behaviour, highest expense category, budget performance, and a comparison with the previous month. Use only the verified figures provided.",context)
    story += [Spacer(1,20),Paragraph("AI monthly summary",styles["Heading2"]),Spacer(1,6),Paragraph(summary.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n","<br/>"),styles["BodyText"])]
    doc.build(story); buffer.seek(0); return send_file(buffer,mimetype="application/pdf",as_attachment=True,download_name=f"spendsmart-{y}-{m:02}.pdf")


@app.route("/settings",methods=["GET","POST"])
@login_required
def settings():
    if request.method=="POST":
        name=request.form.get("name","").strip()
        if len(name)<2: flash("Name must have at least 2 characters.","error")
        else:
            with connect() as db: db.execute("UPDATE users SET name=? WHERE id=?",(name,session["user_id"]))
            session["name"]=name; flash("Profile updated.","success")
    return render_template("settings.html")


@app.route("/api/month")
@login_required
def api_month():
    y,m=month_args(); data=month_data(session["user_id"],y,m)
    return jsonify({"income":data["income"],"expenses":data["expenses"],"budget":data["budget"],"categories":data["by_cat"]})


if __name__ == "__main__":
    app.run(host="127.0.0.1",port=int(os.getenv("PORT",5000)),debug=os.getenv("FLASK_DEBUG","false").lower()=="true")
