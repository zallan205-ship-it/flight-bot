"""
Script di SOLA DIAGNOSTICA: fa la stessa richiesta dello scraper, ma invece
di provare a interpretarla, salva la risposta grezza e stampa indizi utili
per capire perché il parser non trova quello che si aspetta.

Uso: python debug_scraper.py
"""
from datetime import datetime, timedelta

from primp import Client
from fast_flights import FlightQuery, Passengers, create_filter

data_test = datetime.now() + timedelta(days=30)

flights = [FlightQuery(
    date=data_test.strftime("%Y-%m-%d"),
    from_airport="PSA", to_airport="CAG",
)]
query = create_filter(
    flights=flights, trip="one-way", seat="economy",
    passengers=Passengers(adults=1), language="it", currency="EUR",
)

client = Client(
    impersonate="chrome_145",
    impersonate_os="macos",
    referer=True,
    cookie_store=True,
)

print("Faccio la richiesta...")
response = client.get(
    "https://www.google.com/travel/flights",
    params=query.params(),
    cookies={"CONSENT": "YES+"},
)

html = response.text

print(f"\nStatus code: {response.status_code}")
print(f"Lunghezza HTML ricevuto: {len(html)} caratteri")
print(f"URL finale (dopo eventuali redirect): {response.url}")

# Salviamo tutto su file per poterlo ispezionare per intero se serve
with open("risposta_google.html", "w", encoding="utf-8") as f:
    f.write(html)
print("\nHTML completo salvato in risposta_google.html")

# Indizi rapidi senza dover leggere tutto l'HTML a mano
marker_da_cercare = {
    "pagina di consenso cookie": "consent.google.com",
    "richiesta di conferma 'non sono un robot'": "recaptcha",
    "blocco per traffico insolito": "unusual traffic",
    "pagina di errore generica Google": "Error 403",
    "presenza dello script atteso dal parser": 'script class="ds:1"',
    "presenza generica di dati voli": "AF-initDataCallback",
}

print("\n--- Indizi trovati nell'HTML ---")
for descrizione, testo in marker_da_cercare.items():
    trovato = testo.lower() in html.lower()
    print(f"{'✓' if trovato else '✗'} {descrizione} (cerco '{testo}'): {trovato}")

print("\nPrime 500 caratteri dell'HTML ricevuto, per un'occhiata rapida:")
print(html[:500])
