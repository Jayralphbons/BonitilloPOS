"""
Pulse POS - backend (Flask + Firebase Firestore)

Setup:
    pip install flask flask-cors firebase-admin
    Put your service-account key (firebaseAPI.json) next to this file.
    Put index.html inside a folder called "templates".
    python backend.py   ->   open http://localhost:5000/

Optional: set GOOGLE_APPLICATION_CREDENTIALS_FILE to use another key path.
"""
import os
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

# ---------- Firebase ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS_FILE", os.path.join(BASE_DIR, "firebaseAPI.json")
)
firebase_admin.initialize_app(credentials.Certificate(KEY_PATH))

# Your Firestore database is named "default" (shown without parentheses in the console).
# If you recreate it with the standard ID "(default)", set this to None.
DATABASE_ID = "default"
db = firestore.client(database_id=DATABASE_ID) if DATABASE_ID else firestore.client()

PRODUCTS = db.collection("products")
SALES = db.collection("sales")
SETTINGS = db.collection("settings").document("store")  # holds lowStock + nextSaleNo

MANILA = timezone(timedelta(hours=8))  # store day boundaries use Philippine time

app = Flask(__name__)
CORS(app)


@app.errorhandler(Exception)
def handle_any_error(e):
    """Return the real error as JSON (and log the traceback) instead of a blank 500."""
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    app.logger.exception("Unhandled error")
    return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# ---------- Page ----------
@app.get("/")
def home():
    return render_template("index.html")


# ---------- Helpers ----------
def to_cents(amount) -> int:
    return round(float(amount) * 100)


def error(msg, code=400):
    return jsonify({"error": msg}), code


def product_to_dict(doc):
    d = doc.to_dict()
    d["id"] = doc.id
    d.setdefault("active", True)
    return d


def manila_day_start_utc(date_str, plus_days=0):
    """'YYYY-MM-DD' (Manila date) -> ISO UTC timestamp of that day's midnight."""
    d = datetime.fromisoformat(date_str).replace(tzinfo=MANILA) + timedelta(days=plus_days)
    return d.astimezone(timezone.utc).isoformat()


def validate_product(data):
    name = str(data.get("name", "")).strip()
    category = str(data.get("category", "")).strip() or "Uncategorized"
    try:
        price = float(data.get("price"))
        stock = int(data.get("stock"))
    except (TypeError, ValueError):
        return None, "Price and stock must be numbers."
    if not name:
        return None, "Product name is required."
    if price <= 0 or abs(price * 100 - round(price * 100)) > 1e-6:
        return None, "Price must be > 0 with at most 2 decimals."
    if stock < 0:
        return None, "Stock must be 0 or more."
    return {"name": name, "category": category, "price": round(price, 2), "stock": stock}, None


# ---------- Products ----------
@app.get("/api/products")
def list_products():
    status = request.args.get("status", "active")  # active | inactive | all
    items = [product_to_dict(d) for d in PRODUCTS.stream()]
    if status != "all":
        items = [p for p in items if p["active"] == (status == "active")]
    items.sort(key=lambda p: p["name"].lower())
    return jsonify(items)


@app.post("/api/products")
def create_product():
    data, err = validate_product(request.get_json(silent=True) or {})
    if err:
        return error(err)
    for d in PRODUCTS.stream():  # unique name (case-insensitive)
        if d.to_dict()["name"].lower() == data["name"].lower():
            return error("A product with this name already exists.", 409)
    data["active"] = True
    _, ref = PRODUCTS.add(data)
    return jsonify({"id": ref.id, **data}), 201


@app.put("/api/products/<pid>")
def update_product(pid):
    data, err = validate_product(request.get_json(silent=True) or {})
    if err:
        return error(err)
    ref = PRODUCTS.document(pid)
    if not ref.get().exists:
        return error("Product not found.", 404)
    for d in PRODUCTS.stream():
        if d.id != pid and d.to_dict()["name"].lower() == data["name"].lower():
            return error("A product with this name already exists.", 409)
    ref.update(data)
    return jsonify({"id": pid, **data})


@app.patch("/api/products/<pid>/active")
def set_active(pid):
    active = bool((request.get_json(silent=True) or {}).get("active", True))
    ref = PRODUCTS.document(pid)
    if not ref.get().exists:
        return error("Product not found.", 404)
    ref.update({"active": active})
    return jsonify({"id": pid, "active": active})


@app.get("/api/products/<pid>")
def get_product(pid):
    snap = PRODUCTS.document(pid).get()
    if not snap.exists:
        return error("Product not found.", 404)
    return jsonify(product_to_dict(snap))


@app.delete("/api/products/<pid>")
def delete_product(pid):
    """Permanently remove a product. Past sales keep their own copy of name/price."""
    ref = PRODUCTS.document(pid)
    if not ref.get().exists:
        return error("Product not found.", 404)
    ref.delete()
    return jsonify({"deleted": pid})


# ---------- Checkout (atomic) ----------
@firestore.transactional
def _checkout_tx(tx, items, cash_cents):
    """Validate stock, deduct it, number and record the sale - all or nothing."""
    lines, total = [], 0
    for it in items:
        ref = PRODUCTS.document(str(it["pid"]))
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise ValueError("A product in the cart no longer exists.")
        p = snap.to_dict()
        qty = int(it["qty"])
        if not p.get("active", True):
            raise ValueError(f'"{p["name"]}" is inactive.')
        if qty < 1:
            raise ValueError(f'Invalid quantity for "{p["name"]}".')
        if qty > p["stock"]:
            raise ValueError(f'Not enough stock for "{p["name"]}" (available: {p["stock"]}).')
        price_c = to_cents(p["price"])
        lines.append({
            "pid": ref.id, "name": p["name"], "category": p["category"],
            "priceCents": price_c, "qty": qty, "subtotalCents": price_c * qty,
            "_ref": ref, "_newStock": p["stock"] - qty,
        })
        total += price_c * qty

    if cash_cents < total:
        raise ValueError(f"Cash is short by {(total - cash_cents) / 100:.2f}.")

    # sequential receipt number (read before any write)
    next_no = (SETTINGS.get(transaction=tx).to_dict() or {}).get("nextSaleNo", 1)

    for l in lines:
        tx.update(l.pop("_ref"), {"stock": l.pop("_newStock")})
    tx.set(SETTINGS, {"nextSaleNo": next_no + 1}, merge=True)

    sale_ref = SALES.document()
    sale = {
        "number": next_no,
        "ts": datetime.now(timezone.utc).isoformat(),
        "items": lines,
        "totalCents": total,
        "tenderedCents": cash_cents,
        "changeCents": cash_cents - total,
    }
    tx.set(sale_ref, sale)
    return {"id": sale_ref.id, **sale}


@app.post("/api/checkout")
def checkout():
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not items:
        return error("Add at least one item.")
    try:
        cash_cents = to_cents(body.get("cash"))
        sale = _checkout_tx(db.transaction(), items, cash_cents)
    except (ValueError, TypeError, KeyError) as e:
        return error(str(e))
    return jsonify(sale), 201


# ---------- Sales history ----------
@app.get("/api/sales")
def list_sales():
    """Newest first. Optional ?from=YYYY-MM-DD&to=YYYY-MM-DD (Manila dates)."""
    q = SALES.order_by("ts", direction=firestore.Query.DESCENDING)
    d_from, d_to = request.args.get("from"), request.args.get("to")
    try:
        if d_from:
            q = q.where("ts", ">=", manila_day_start_utc(d_from))
        if d_to:
            q = q.where("ts", "<", manila_day_start_utc(d_to, plus_days=1))
    except ValueError:
        return error("Dates must look like YYYY-MM-DD.")
    sales = [{"id": d.id, **d.to_dict()} for d in q.stream()]
    return jsonify({
        "sales": sales,
        "totalCents": sum(s["totalCents"] for s in sales),
        "count": len(sales),
    })


@app.get("/api/sales/<sid>")
def get_sale(sid):
    snap = SALES.document(sid).get()
    if not snap.exists:
        return error("Sale not found.", 404)
    return jsonify({"id": snap.id, **snap.to_dict()})


@app.put("/api/sales/<sid>")
def update_sale(sid):
    """Correct the cash received on a sale (change is recalculated)."""
    ref = SALES.document(sid)
    snap = ref.get()
    if not snap.exists:
        return error("Sale not found.", 404)
    try:
        cash_cents = to_cents((request.get_json(silent=True) or {}).get("cash"))
    except (TypeError, ValueError):
        return error("Enter the cash received.")
    total = snap.to_dict()["totalCents"]
    if cash_cents < total:
        return error(f"Cash must be at least {total / 100:.2f}.")
    ref.update({"tenderedCents": cash_cents, "changeCents": cash_cents - total})
    return jsonify({"id": sid, "tenderedCents": cash_cents, "changeCents": cash_cents - total})


@firestore.transactional
def _delete_sale_tx(tx, sale_ref, restock):
    """Delete a sale and (optionally) put its items back in stock - all or nothing."""
    snap = sale_ref.get(transaction=tx)
    if not snap.exists:
        raise LookupError("Sale not found.")
    restores = []
    if restock:
        for it in snap.to_dict().get("items", []):
            pref = PRODUCTS.document(str(it["pid"]))
            ps = pref.get(transaction=tx)
            if ps.exists:  # product may have been deleted since
                restores.append((pref, ps.to_dict()["stock"] + int(it["qty"])))
    for pref, new_stock in restores:
        tx.update(pref, {"stock": new_stock})
    tx.delete(sale_ref)


@app.delete("/api/sales/<sid>")
def delete_sale(sid):
    restock = request.args.get("restock", "1") not in ("0", "false", "no")
    try:
        _delete_sale_tx(db.transaction(), SALES.document(sid), restock)
    except LookupError as e:
        return error(str(e), 404)
    return jsonify({"deleted": sid, "restocked": restock})


# ---------- Dashboard & settings ----------
@app.get("/api/settings")
def get_settings():
    return jsonify({"lowStock": (SETTINGS.get().to_dict() or {}).get("lowStock", 5)})


@app.put("/api/settings/low-stock")
def set_low_stock():
    try:
        level = int((request.get_json(silent=True) or {}).get("lowStock"))
        if level < 0:
            raise ValueError
    except (TypeError, ValueError):
        return error("Enter a whole number, 0 or more.")
    SETTINGS.set({"lowStock": level}, merge=True)
    return jsonify({"lowStock": level})


@app.get("/api/dashboard")
def dashboard():
    low_level = (SETTINGS.get().to_dict() or {}).get("lowStock", 5)
    today = datetime.now(MANILA).date().isoformat()
    todays = [d.to_dict() for d in SALES.where("ts", ">=", manila_day_start_utc(today)).stream()]
    products = [product_to_dict(d) for d in PRODUCTS.stream()]
    low = [p for p in products if p["active"] and p["stock"] <= low_level]
    return jsonify({
        "todayTotalCents": sum(s["totalCents"] for s in todays),
        "todayTransactions": len(todays),
        "productCount": len(products),
        "lowStock": sorted(low, key=lambda p: p["stock"]),
        "lowStockLevel": low_level,
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)