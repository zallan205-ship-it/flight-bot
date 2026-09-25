"""
Scraper per Google Flights basato su `fast-flights` (https://github.com/AWeirdDev/fast-flights,
pacchetto PyPI `fast-flights`).

A differenza di un approccio con Playwright + selettori CSS, questa libreria non
fa scraping del DOM visibile: replica la richiesta HTTP che Google Flights usa
internamente e legge i dati grezzi (JSON) che Google incorpora in un tag
<script> della pagina di risultati. È molto più robusto: non si rompe a ogni
redesign grafico del sito, solo se Google cambia la struttura di quel payload
interno (più raro, ma può succedere: se in futuro il parsing smette di
funzionare, il primo posto dove guardare è il changelog del progetto GitHub).

Filtro orario: `FlightQuery` supporta nativamente `earliest_departure_hour`,
che viene incluso nella richiesta a Google stesso (non è un filtro fatto in
locale sui risultati) — l'ho verificato leggendo querying.py del pacchetto.
Per i weekend automatici lo usiamo per restringere ai voli serali (richiesto:
partenza sabato sera, ritorno domenica sera); per i monitoraggi manuali non lo
passiamo, quindi Google restituisce voli di qualunque orario della giornata.

Muro di consenso cookie UE: per i visitatori dall'Unione Europea (il caso del
Raspberry Pi, con IP italiano), la prima richiesta a Google Flights viene
rediretta a una pagina di consenso (consent.google.com) invece dei risultati.
Un semplice cookie statico (il vecchio trucco "CONSENT=YES+", ancora citato in
molte guide) NON basta più — verificato con un test reale, restituiva ancora
la pagina di consenso. L'unico approccio che ha funzionato in un test reale:
individuare il modulo "Accetta" nella pagina di consenso e inviarlo per
davvero via POST, esattamente come fa un browser quando l'utente clicca il
pulsante. Fatto questo una volta, il client (con `cookie_store=True`) tiene il
cookie di consenso ottenuto per tutte le richieste successive nello stesso
processo — quindi questo passaggio extra si paga solo alla primissima
richiesta di ogni avvio del bot, non a ogni scraping.

Non richiede un browser headless: fa richieste HTTP dirette con un client che
imita l'impronta TLS di Chrome (libreria `primp`), quindi resta molto più
leggero di Playwright anche con il passaggio di consenso in più.
"""
from datetime import datetime

from primp import Client
from selectolax.lexbor import LexborHTMLParser
from fast_flights import FlightQuery, Passengers, create_filter
from fast_flights.parser import parse
from fast_flights.exceptions import FlightsNotFound

from . import ScrapedPrice, FlightOption

GOOGLE_FLIGHTS_URL = "https://www.google.com/travel/flights"

# Un solo client, riusato tra le chiamate: dopo il primo superamento del muro
# di consenso, il cookie ottenuto resta valido per tutte le richieste
# successive nello stesso processo (cookie_store=True), evitando di rifare
# l'intero handshake di consenso a ogni singolo volo controllato.
_client = Client(
    impersonate="chrome_145",
    impersonate_os="macos",
    referer=True,
    cookie_store=True,
)


def _submit_consent_form(consent_html: str) -> str:
    """
    Trova il modulo di accettazione nella pagina di consenso EU e lo invia via
    POST, come farebbe un browser reale al click su "Accetta tutto". Ritorna
    l'HTML della pagina risultante (idealmente i risultati veri di Google
    Flights, se il consenso viene accettato correttamente).
    """
    consent_page = LexborHTMLParser(consent_html)
    forms = consent_page.css("form")
    if not forms:
        raise RuntimeError(
            "Pagina di consenso Google senza nessun <form>: struttura della "
            "pagina cambiata rispetto a quella vista in fase di test."
        )

    modulo = None
    for f in forms:
        testo = f.text().lower()
        if "accetta" in testo or "agree" in testo or "accept" in testo:
            modulo = f
            break
    if modulo is None:
        modulo = forms[0]

    action = modulo.attributes.get("action")
    if not action:
        raise RuntimeError("Il modulo di consenso non ha un URL di invio (action).")
    if action.startswith("/"):
        action = "https://consent.google.com" + action

    campi = {}
    for inp in modulo.css("input"):
        nome = inp.attributes.get("name")
        if nome:
            campi[nome] = inp.attributes.get("value", "")

    response = _client.post(action, data=campi)
    return response.text


def _fetch_html(query) -> str:
    response = _client.get(GOOGLE_FLIGHTS_URL, params=query.params())

    if "consent.google.com" in response.url:
        return _submit_consent_form(response.text)

    return response.text


def _format_time(time_tuple: tuple[int, int] | None) -> str | None:
    if time_tuple is None:
        return None
    hour, minute = time_tuple
    return f"{hour:02d}:{minute:02d}"


def _build_query(origin: str, destination: str, departure_date: datetime,
                  return_date: datetime | None,
                  earliest_departure_hour: int | None = None,
                  latest_departure_hour: int | None = None,
                  earliest_return_hour: int | None = None,
                  latest_return_hour: int | None = None):
    flights = [FlightQuery(
        date=departure_date.strftime("%Y-%m-%d"),
        from_airport=origin, to_airport=destination,
        earliest_departure_hour=earliest_departure_hour,
        latest_departure_hour=latest_departure_hour,
    )]
    if return_date:
        flights.append(FlightQuery(
            date=return_date.strftime("%Y-%m-%d"),
            from_airport=destination, to_airport=origin,
            earliest_departure_hour=earliest_return_hour,
            latest_departure_hour=latest_return_hour,
        ))

    return create_filter(
        flights=flights,
        trip="round-trip" if return_date else "one-way",
        seat="economy",
        passengers=Passengers(adults=1),
        language="it",
        currency="EUR",
    )


def scrape_price(origin: str, destination: str, departure_date: datetime,
                  return_date: datetime | None = None,
                  earliest_departure_hour: int | None = None,
                  latest_departure_hour: int | None = None,
                  earliest_return_hour: int | None = None,
                  latest_return_hour: int | None = None,
                  direct_only: bool = False) -> ScrapedPrice | None:
    query = _build_query(
        origin, destination, departure_date, return_date,
        earliest_departure_hour, latest_departure_hour,
        earliest_return_hour, latest_return_hour,
    )

    try:
        html = _fetch_html(query)
        result = parse(html)
    except FlightsNotFound:
        # Nessun volo per questa data/fascia oraria: può voler dire che il volo
        # non è ancora "rilasciato" dalla compagnia, oppure che quel giorno non
        # ci sono voli nella finestra oraria richiesta (es. nessun volo serale
        # quel sabato). In entrambi i casi non è un errore da loggare.
        return None
    except Exception:
        # Errori di rete/parsing vanno loggati a monte dallo scheduler.
        raise

    if not result:
        return None

    candidati = result
    if direct_only:
        tappe_attese = 2 if return_date else 1
        candidati = [f for f in result if len(f.flights) == tappe_attese]
        if not candidati:
            # Ci sono voli quel giorno, ma nessuno senza scali: trattiamolo
            # come "nessun risultato utile", non come prezzo da segnalare.
            return None

    cheapest = min(candidati, key=lambda f: f.price)

    departure_time = None
    return_time = None
    if cheapest.flights:
        departure_time = _format_time(cheapest.flights[0].departure.time)
        # NOTA non verificata contro il sito reale: per una ricerca andata+ritorno
        # assumo che l'ultima tappa in `cheapest.flights` sia il volo di ritorno
        # (il payload di Google elenca le tappe in ordine cronologico). Da
        # confermare al primo test reale con una ricerca andata+ritorno: se
        # l'orario di ritorno mostrato negli alert risultasse sbagliato, è qui
        # che va corretto.
        if return_date and len(cheapest.flights) > 1:
            return_time = _format_time(cheapest.flights[-1].departure.time)

    return ScrapedPrice(
        price_eur=float(cheapest.price),
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        source="google_flights",
        departure_time=departure_time,
        return_time=return_time,
    )


# Alias per uniformità di nome (nessuna delle due operazioni qui sopra è
# realmente asincrona: fast-flights e il client HTTP sottostante sono sincroni).
scrape_price_sync = scrape_price


def search_options(origin: str, destination: str, departure_date: datetime,
                    return_date: datetime | None = None,
                    max_results: int = 6) -> list[FlightOption]:
    """
    Usata da /cerca: ritorna fino a `max_results` opzioni di volo SENZA SCALI,
    ordinate per prezzo crescente. Nessun filtro orario (copre tutta la
    giornata) — il filtro sull'orario si applica dopo, quando l'utente sceglie
    un'opzione e nasce il monitoraggio vero e proprio.

    "Senza scali" qui significa: la tappa di andata è un unico segmento
    (nessuno scalo), e se c'è anche il ritorno, anche quella è un unico
    segmento. Assunzione NON verificata contro il sito reale per il caso
    andata+ritorno: presumo che un'andata diretta + un ritorno diretto diano
    esattamente 2 elementi in `flights`. Se in pratica risultasse diverso
    (es. Google restituisse più di 2 tappe anche per un volo diretto per
    qualche motivo), è qui che va corretto il conteggio.
    """
    query = _build_query(origin, destination, departure_date, return_date)

    try:
        html = _fetch_html(query)
        result = parse(html)
    except FlightsNotFound:
        return []

    if not result:
        return []

    tappe_attese = 2 if return_date else 1
    diretti = [f for f in result if len(f.flights) == tappe_attese]
    diretti.sort(key=lambda f: f.price)

    opzioni = []
    for f in diretti[:max_results]:
        departure_time = _format_time(f.flights[0].departure.time)
        return_time = None
        if return_date and len(f.flights) > 1:
            return_time = _format_time(f.flights[-1].departure.time)
        opzioni.append(FlightOption(
            price_eur=float(f.price),
            departure_time=departure_time,
            return_time=return_time,
            airlines=f.airlines,
        ))
    return opzioni
