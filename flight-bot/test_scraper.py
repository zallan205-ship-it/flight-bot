"""
Test rapido e a sé stante: verifica che lo scraper Google Flights funzioni
davvero contro il sito reale. Non tocca il database, non manda messaggi
Telegram — stampa solo il risultato a schermo.

Uso: python test_scraper.py
"""
from datetime import datetime, timedelta
from scrapers.google_flights import scrape_price

# Una data abbastanza vicina da avere quasi certamente voli disponibili
data_test = datetime.now() + timedelta(days=30)

print(f"Cerco voli PSA -> CAG per il {data_test:%d/%m/%Y}...")

risultato = scrape_price(
    origin="PSA",
    destination="CAG",
    departure_date=data_test,
)

if risultato is None:
    print("\nNessun risultato: o non ci sono voli quel giorno su quella tratta, "
          "o c'è un problema. Se ti aspettavi voli disponibili, incolla qui "
          "l'eventuale errore/traceback per capire cosa non va.")
else:
    print(f"\nTrovato: {risultato.price_eur}€, orario partenza: {risultato.departure_time}")
    print("\nLo scraper funziona.")
