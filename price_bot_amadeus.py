import os, json, time, datetime as dt
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ====== TELEGRAM ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ====== AMADEUS ======
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")
AMADEUS_ENV = (os.getenv("AMADEUS_ENV") or "test").lower().strip()  # "test" | "prod"

if AMADEUS_ENV == "prod":
    OAUTH_URL = "https://api.amadeus.com/v1/security/oauth2/token"
    SEARCH_URL = "https://api.amadeus.com/v2/shopping/flight-offers"
else:
    OAUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
    SEARCH_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

# ====== CONFIGURACIÓN ======
# IATA: "EZE", "BUE", "MAD", etc.
ORIGEN = "EZE"               # para sandbox, si no devuelve, probá "BUE"
DESTINO = "MAD"
FECHA_DESDE = "01/11/2025"   # DD/MM/YYYY
FECHA_HASTA = "15/12/2025"   # DD/MM/YYYY
SOLO_IDA = False
NOCHES_MIN = 7
NOCHES_MAX = 21

ADULTS = 1
MONEDA = "USD"
PRECIO_OBJETIVO = 600
BAJA_MINIMA_PCT = 8

# Para no exceder límites
MAX_DIAS = 20

# Archivos locales
STATE_FILE = Path("price_state.json")
LOG_FILE = Path("price_log.txt")
TOKEN_FILE = Path(".amadeus_token.json")  # cache sencillo del token

def log(msg: str):
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LOG_FILE.write_text((LOG_FILE.read_text() if LOG_FILE.exists() else "") + f"[{ts}] {msg}\n")

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    r = requests.post(url, data=data, timeout=20)
    r.raise_for_status()

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def to_iso(dmy: str) -> str:
    d, m, y = dmy.split("/")
    return f"{y}-{m.zfill(2)}-{d.zfill(2)}"

def daterange(d1_iso: str, d2_iso: str):
    d1 = dt.date.fromisoformat(d1_iso)
    d2 = dt.date.fromisoformat(d2_iso)
    step = dt.timedelta(days=1)
    while d1 <= d2:
        yield d1
        d1 += step

# ====== AMADEUS AUTH ======
def get_amadeus_token():
    now = int(time.time())
    if TOKEN_FILE.exists():
        cached = json.loads(TOKEN_FILE.read_text())
        if cached.get("access_token") and cached.get("expires_at", 0) - 60 > now:
            return cached["access_token"]

    data = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_CLIENT_ID,
        "client_secret": AMADEUS_CLIENT_SECRET
    }
    r = requests.post(OAUTH_URL, data=data, timeout=20)
    if r.status_code >= 400:
        log(f"ERROR TOKEN BODY: {r.text}")
    r.raise_for_status()
    payload = r.json()
    access_token = payload["access_token"]
    expires_in = payload.get("expires_in", 0)
    TOKEN_FILE.write_text(json.dumps({
        "access_token": access_token,
        "expires_at": now + int(expires_in)
    }))
    return access_token

def buscar_mejor_precio_amadeus():
    """
    Itera días dentro de [FECHA_DESDE, FECHA_HASTA] (hasta MAX_DIAS) y devuelve la mejor oferta encontrada.
    Para ida y vuelta, calcula returnDate = departureDate + noches (NOCHES_MIN..NOCHES_MAX).
    """
    token = get_amadeus_token()
    headers = {"Authorization": f"Bearer {token}"}
    date_from = to_iso(FECHA_DESDE)
    date_to = to_iso(FECHA_HASTA)

    def do_request(params):
        nonlocal headers
        r = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
        if r.status_code == 401:
            # token expirado -> reintento único
            new_token = get_amadeus_token()
            headers = {"Authorization": f"Bearer {new_token}"}
            r = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
        if r.status_code >= 400:
            try:
                log(f"ERROR BODY: {r.text}")
            except Exception:
                pass
        r.raise_for_status()
        return r.json().get("data", [])

    best_offer = None
    count_days = 0

    for dep_date in daterange(date_from, date_to):
        if count_days >= MAX_DIAS:
            break
        count_days += 1

        if SOLO_IDA:
            params = {
                "originLocationCode": ORIGEN,
                "destinationLocationCode": DESTINO,
                "departureDate": dep_date.isoformat(),
                "adults": ADULTS,
                "currencyCode": MONEDA,
                "max": 20
            }
            candidates = do_request(params)
        else:
            candidates = []
            for nights in range(NOCHES_MIN, NOCHES_MAX + 1):
                ret_date = (dep_date + dt.timedelta(days=nights)).isoformat()
                params = {
                    "originLocationCode": ORIGEN,
                    "destinationLocationCode": DESTINO,
                    "departureDate": dep_date.isoformat(),
                    "returnDate": ret_date,
                    "adults": ADULTS,
                    "currencyCode": MONEDA,
                    "max": 20
                }
                data = do_request(params)
                for item in data:
                    item["_nights"] = nights
                candidates.extend(data)

        if not candidates:
            continue

        def price_of(o):
            try:
                return float(o["price"]["total"])
            except Exception:
                return float("inf")

        cheapest = min(candidates, key=price_of)
        if (best_offer is None) or (price_of(cheapest) < price_of(best_offer)):
            best_offer = cheapest
            best_offer["_route_summary"] = f"{ORIGEN}→{DESTINO}"
            if not SOLO_IDA:
                best_offer["_nights"] = cheapest.get("_nights")

    return best_offer

def main():
    for var, name in [
        (TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN"),
        (TELEGRAM_CHAT_ID, "TELEGRAM_CHAT_ID"),
        (AMADEUS_CLIENT_ID, "AMADEUS_CLIENT_ID"),
        (AMADEUS_CLIENT_SECRET, "AMADEUS_CLIENT_SECRET"),
    ]:
        if not var:
            raise RuntimeError(f"Falta {name} en el .env")

    log("Buscando precio (Amadeus)...")
    offer = buscar_mejor_precio_amadeus()

    if not offer:
        log("Sin resultados")
        return

    total_price = float(offer["price"]["total"])
    currency = offer["price"].get("currency", MONEDA)
    route_sum = offer.get("_route_summary", f"{ORIGEN}→{DESTINO}")
    nights = offer.get("_nights", None)

    carriers = set()
    try:
        for it in offer["itineraries"]:
            for seg in it["segments"]:
                carriers.add(seg["carrierCode"])
    except Exception:
        pass
    carriers_str = ", ".join(sorted(list(carriers))) if carriers else "N/D"

    state = load_state()
    prev_min = state.get("min_price")

    debe_alertar = False
    motivos = []

    if prev_min is None or total_price < prev_min:
        if prev_min is None:
            motivos.append("Primer precio (se guarda como mínimo).")
        else:
            drop_pct = round((prev_min - total_price) * 100 / prev_min, 2)
            motivos.append(f"Nuevo mínimo histórico (↓{drop_pct}%).")
        state["min_price"] = total_price
        state["min_when"] = dt.datetime.now().isoformat()
        state["min_route"] = route_sum
        debe_alertar = True
    else:
        drop_pct = round((prev_min - total_price) * 100 / prev_min, 2)
        if drop_pct >= BAJA_MINIMA_PCT:
            motivos.append(f"Baja de {drop_pct}% vs. mínimo guardado.")
            debe_alertar = True

    if total_price <= PRECIO_OBJETIVO:
        motivos.append(f"Precio ≤ objetivo ({PRECIO_OBJETIVO} {MONEDA}).")
        debe_alertar = True

    save_state(state)

    msg = (
        f"✈️ <b>Alerta {ORIGEN} → {DESTINO}</b>\n"
        f"Aerolíneas: {carriers_str}\n"
        + (f"Noches en destino: {nights}\n" if (nights is not None and not SOLO_IDA) else "")
        + f"💰 Precio: <b>{total_price:.2f} {currency}</b>\n"
        + (f"🧭 Mínimo histórico: {state.get('min_price'):.2f} {currency}\n" if state.get("min_price") else "")
        + ("— " + " | ".join(motivos) + "\n" if motivos else "")
        + "\nℹ️ Obtenido con Amadeus Flight Offers Search."
    )

    if debe_alertar:
        send_telegram(msg)
        log(f"ALERTA enviada. Precio: {total_price:.2f} {currency}. Motivos: {motivos}")
    else:
        log(f"Sin alerta. Precio: {total_price:.2f} {currency}. Prev_min: {prev_min} {currency if prev_min else ''}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        try:
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    data={"chat_id": TELEGRAM_CHAT_ID, "text": f"⚠️ Error en bot de precios (Amadeus): {e}"},
                    timeout=20
                )
        except Exception:
            pass
        raise
