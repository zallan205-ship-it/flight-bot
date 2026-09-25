"""
Tentativo più robusto di superare il muro di consenso EU di Google: invece di
un cookie statico (che nel test precedente non ha funzionato), analizza la
pagina di consenso che Google restituisce, trova il modulo "Accetta tutto" e
lo invia per davvero via POST — lo stesso identico meccanismo che scatta
quando un utente reale clicca il pulsante nel browser.

Uso: python debug_scraper2.py

ATTENZIONE: non ho potuto testare questo script contro il sito reale (nessun
accesso di rete a google.com da dove scrivo il codice). Questo è il primo
vero collaudo.
"""
from datetime import datetime, timedelta

from primp import Client
from selectolax.lexbor import LexborHTMLParser
from fast_flights import FlightQuery, Passengers, create_filter
from fast_flights.parser import parse

data_test = datetime.now() + timedelta(days=30)

flights = [FlightQuery(
    date=data_test.strftime("%Y-%m-%d"),
    from_airport="PSA", to_airport="CAG",
)]
query = create_filter(
    flights=flights, trip="one-way", seat="economy",
    passengers=Passengers(adults=1), language="it", currency="EUR",
)

# cookie_store=True: il client ricorda automaticamente i cookie che Google
# imposta durante tutta la sequenza di richieste (fondamentale qui, perché
# il cookie di consenso vero arriva solo DOPO aver inviato il modulo).
client = Client(
    impersonate="chrome_145",
    impersonate_os="macos",
    referer=True,
    cookie_store=True,
)

print("1) Richiesta iniziale a Google Flights...")
resp1 = client.get(
    "https://www.google.com/travel/flights",
    params=query.params(),
)
print(f"   URL finale: {resp1.url}")

if "consent.google.com" not in resp1.url:
    print("   Nessun muro di consenso incontrato stavolta, provo a interpretare subito...")
    html_finale = resp1.text
else:
    print("2) Trovato muro di consenso, cerco il modulo da inviare...")
    consent_page = LexborHTMLParser(resp1.text)
    forms = consent_page.css("form")
    print(f"   Moduli trovati nella pagina: {len(forms)}")

    modulo_scelto = None
    for f in forms:
        testo_bottoni = f.text().lower()
        if "accetta" in testo_bottoni or "agree" in testo_bottoni or "accept" in testo_bottoni:
            modulo_scelto = f
            break
    if modulo_scelto is None and forms:
        modulo_scelto = forms[0]
        print("   Nessun modulo con testo 'Accetta' trovato esplicitamente, uso il primo disponibile")

    if modulo_scelto is None:
        print("   ERRORE: nessun <form> trovato nella pagina di consenso. "
              "Struttura della pagina diversa da quella attesa.")
        html_finale = None
    else:
        action = modulo_scelto.attributes.get("action")
        campi = {}
        for inp in modulo_scelto.css("input"):
            nome = inp.attributes.get("name")
            valore = inp.attributes.get("value", "")
            if nome:
                campi[nome] = valore

        print(f"   Azione del modulo: {action}")
        print(f"   Campi trovati: {list(campi.keys())}")

        if not action:
            print("   ERRORE: il modulo non ha un 'action' (URL di invio). Impossibile procedere.")
            html_finale = None
        else:
            if action.startswith("/"):
                action = "https://consent.google.com" + action
            print("3) Invio il modulo di consenso...")
            resp2 = client.post(action, data=campi)
            print(f"   URL finale dopo l'invio: {resp2.url}")
            html_finale = resp2.text

if html_finale:
    with open("risposta_dopo_consenso.html", "w", encoding="utf-8") as f:
        f.write(html_finale)

    trovato_script = 'script class="ds:1"'.lower() in html_finale.lower() or "ds:1" in html_finale
    print(f"\n4) Script atteso dal parser presente nella risposta finale: {trovato_script}")

    if trovato_script:
        print("\nProvo a interpretare i risultati con il parser di fast-flights...")
        try:
            risultati = parse(html_finale)
            if risultati:
                economico = min(risultati, key=lambda x: x.price)
                print(f"\nSUCCESSO: trovato un volo a {economico.price}€")
            else:
                print("\nParser OK ma nessun volo trovato per questa data/rotta (lista vuota).")
        except Exception as e:
            print(f"\nIl parser ha comunque dato errore: {e}")
    else:
        print("\nAncora nessun risultato utile. HTML finale salvato in "
              "risposta_dopo_consenso.html per ispezione.")
