"""
Scraper per Google Flights basato su `fast-flights` (https://github.com/AWeirdDev/fast-flights,
pacchetto PyPI `fast-flights`).

A differenza di un approccio con Playwright + selettori CSS, questa libreria non
fa scraping del DOM visibile: replica la richiesta HTTP che Google Flights usa
internamente e legge i dati grezzi (JSON) che Google incorpora in un tag
<script> della pagina di risultati.

--- SCOPERTA IMPORTANTE (dopo test reali) sulla struttura andata+ritorno ---
Una query "round-trip" combinata restituisce, per ogni itinerario, un prezzo
totale ma la lista dei segmenti di volo (`flights`) contiene SOLO le tappe
dell'andata — il ritorno non è rappresentato affatto in quella struttura.
Verificato con dati reali: un volo diretto andata+ritorno risultava avere
`len(flights) == 1`, non 2 come inizialmente presunto (e documentato come
assunzione non verificata nella versione precedente di questo file). Questo
significa che da una query round-trip combinata non si può in generale
recuperare né l'orario di ritorno né distinguere in modo affidabile "diretto"
da "con scalo" per la tappa di ritorno.

Soluzione adottata: andata e ritorno vengono interrogate come DUE ricerche
"one-way" separate e indipendenti. Il prezzo totale riportato è la somma delle
due tariffe one-way più economiche (che rispettano gli eventuali filtri di
orario/scali). Per compagnie low-cost a tariffazione indipendente per tratta
(il caso osservato: Ryanair su tutte le rotte testate), questo prezzo coincide
con quello reale o gli è molto vicino. Per compagnie con veri sconti
andata+ritorno "impacchettati", il totale calcolato così potrebbe risultare
leggermente più alto del prezzo combinato reale di Google — un compromesso
consapevole in cambio di dati corretti e completi per entrambe le tratte
(orario reale, distinzione diretto/scalo affidabile per ciascuna tappa).

Filtro orario: `FlightQuery` supporta nativamente `earliest_departure_hour` e
`latest_departure_hour`, inclusi nella richiesta a Google stesso (non è un
filtro fatto in locale sui risultati).

Muro di consenso cookie UE: per i visitatori dall'Unione Europea, la prima
richiesta a Google Flights viene rediretta a una pagina di consenso
(consent.google.com) invece dei risultati. La soluzione funzionante,
verificata con un test reale: individuare il modulo "Accetta" nella pagina di
consenso e inviarlo via POST, come farebbe un browser al click dell'utente.
Il cookie ottenuto viene poi riusato per tutte le richieste successive nello
stesso processo (client HTTP condiviso a livello di modulo).
"""
from dataclasses import dataclass
from datetime import datetime

from primp import Client, PrimpError
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


class ScraperError(RuntimeError):
    """
    Errore TECNICO durante il recupero o l'analisi di una pagina Google Flights
    (rete, risposta HTTP 4xx/5xx, consenso, pagina non interpretabile).

    NON significa "nessun volo trovato": quel caso resta [] / None secondo il
    contratto di search_leg / scrape_price / search_options. Estende
    RuntimeError perche' il codice di consenso sollevava gia' RuntimeError.
    L'eccezione originale e' sempre disponibile in `__cause__`.
    """


def _request(method: str, url: str, **kwargs):
    """
    GET/POST con il client condiviso. Un errore di rete (timeout, connessione,
    DNS...) o una risposta HTTP 4xx/5xx solleva ScraperError e non arriva mai
    al parser: primp, di suo, restituisce le risposte 429/503 come se fossero
    normali, quindi lo status va controllato esplicitamente.
    """
    try:
        response = getattr(_client, method)(url, **kwargs)
    except PrimpError as exc:
        raise ScraperError(
            f"Richiesta {method.upper()} a Google fallita ({type(exc).__name__})"
        ) from exc
    try:
        response.raise_for_status()
    except PrimpError as exc:
        raise ScraperError(
            f"Google ha risposto con errore HTTP "
            f"{getattr(response, 'status_code', '?')} alla richiesta {method.upper()}"
        ) from exc
    return response


@dataclass
class LegOption:
    """Una singola opzione per UNA tratta (sola andata o solo ritorno)."""
    price_eur: float
    time: str
    airlines: list[str]
    stops: int  # 0 = diretto


def _submit_consent_form(consent_html: str) -> str:
    """
    Trova il modulo di accettazione nella pagina di consenso EU e lo invia via
    POST, come farebbe un browser reale al click su "Accetta tutto". Ritorna
    l'HTML della pagina risultante.
    """
    consent_page = LexborHTMLParser(consent_html)
    forms = consent_page.css("form")
    if not forms:
        raise ScraperError(
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
        raise ScraperError("Il modulo di consenso non ha un URL di invio (action).")
    if action.startswith("/"):
        action = "https://consent.google.com" + action

    campi = {}
    for inp in modulo.css("input"):
        nome = inp.attributes.get("name")
        if nome:
            campi[nome] = inp.attributes.get("value", "")

    response = _request("post", action, data=campi)
    return response.text


def _fetch_html(query) -> str:
    response = _request("get", GOOGLE_FLIGHTS_URL, params=query.params())

    if "consent.google.com" in response.url:
        return _submit_consent_form(response.text)

    return response.text


def _format_time(time_tuple: tuple[int, int] | None) -> str | None:
    if time_tuple is None:
        return None
    hour, minute = time_tuple
    return f"{hour:02d}:{minute:02d}"


def _build_one_way_query(origin: str, destination: str, date: datetime,
                          earliest_hour: int | None = None,
                          latest_hour: int | None = None):
    flight = FlightQuery(
        date=date.strftime("%Y-%m-%d"),
        from_airport=origin, to_airport=destination,
        earliest_departure_hour=earliest_hour,
        latest_departure_hour=latest_hour,
    )
    return create_filter(
        flights=[flight],
        trip="one-way",
        seat="economy",
        passengers=Passengers(adults=1),
        language="it",
        currency="EUR",
    )


def search_leg(origin: str, destination: str, date: datetime,
               earliest_hour: int | None = None,
               latest_hour: int | None = None,
               direct_only: bool = False,
               max_results: int | None = None) -> list[LegOption]:
    """
    Cerca le opzioni per UNA sola tratta (sola andata), ordinate per prezzo
    crescente. È la funzione di base su cui si appoggiano sia scrape_price()
    (che prende solo la più economica) sia search_options() per /cerca (che
    ne mostra diverse per costruire le combinazioni andata×ritorno).

    Nessun volo -> []. Errore tecnico (rete, HTTP, consenso, parsing) ->
    ScraperError: i due casi non vanno mai confusi.
    """
    query = _build_one_way_query(origin, destination, date, earliest_hour, latest_hour)

    try:
        html = _fetch_html(query)
        result = parse(html)
    except FlightsNotFound:
        # Contratto invariato: per fast-flights e' "nessun volo" (il suo parse()
        # la solleva su un payload di errore di Google). Non verificato con
        # dati reali se questo payload possa anche indicare un errore transitorio.
        return []
    except ScraperError:
        raise
    except Exception as exc:
        raise ScraperError(
            f"Analisi della pagina Google Flights fallita ({type(exc).__name__})"
        ) from exc

    opzioni = []
    for f in result:
        stops = len(f.flights) - 1
        if direct_only and stops != 0:
            continue
        if not f.flights:
            continue
        opzioni.append(LegOption(
            price_eur=float(f.price),
            time=_format_time(f.flights[0].departure.time),
            airlines=f.airlines,
            stops=stops,
        ))

    opzioni.sort(key=lambda o: o.price_eur)
    return opzioni[:max_results] if max_results else opzioni


def scrape_price(origin: str, destination: str, departure_date: datetime,
                  return_date: datetime | None = None,
                  earliest_departure_hour: int | None = None,
                  latest_departure_hour: int | None = None,
                  earliest_return_hour: int | None = None,
                  latest_return_hour: int | None = None,
                  direct_only: bool = False) -> ScrapedPrice | None:
    """
    Prezzo più economico per la combinazione andata(+ritorno) richiesta.
    Andata e ritorno sono cercate come tratte indipendenti (vedi nota in cima
    al file) e il prezzo totale è la somma delle due tariffe più economiche
    che rispettano i filtri richiesti.

    None = nessun risultato. Un errore tecnico solleva ScraperError (vedi
    search_leg): non viene mai convertito in None.
    """
    andata = search_leg(origin, destination, departure_date,
                         earliest_departure_hour, latest_departure_hour, direct_only)
    if not andata:
        return None
    piu_economica_andata = andata[0]

    return_time = None
    prezzo_totale = piu_economica_andata.price_eur

    if return_date:
        ritorno = search_leg(destination, origin, return_date,
                              earliest_return_hour, latest_return_hour, direct_only)
        if not ritorno:
            return None
        piu_economico_ritorno = ritorno[0]
        prezzo_totale += piu_economico_ritorno.price_eur
        return_time = piu_economico_ritorno.time

    return ScrapedPrice(
        price_eur=prezzo_totale,
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        source="google_flights",
        departure_time=piu_economica_andata.time,
        return_time=return_time,
    )


# Alias per uniformità di nome (nessuna delle operazioni qui sopra è
# realmente asincrona: fast-flights e il client HTTP sottostante sono sincroni).
scrape_price_sync = scrape_price


def search_options(origin: str, destination: str, departure_date: datetime,
                    return_date: datetime | None = None,
                    max_results: int = 6, max_per_leg: int = 4) -> list[FlightOption]:
    """
    Usata da /cerca: ritorna fino a `max_results` opzioni SENZA SCALI, ordinate
    per prezzo crescente. Se c'è anche il ritorno, prende fino a `max_per_leg`
    opzioni per ciascuna tratta e ne calcola TUTTE le combinazioni (fino a
    max_per_leg²), non solo l'abbinamento più economico — così puoi scegliere
    anche una combinazione che non è la più conveniente in assoluto, se ti fa
    comodo un orario diverso.

    [] = nessuna opzione. Un errore tecnico solleva ScraperError (vedi
    search_leg): non viene mai convertito in [].
    """
    andata = search_leg(origin, destination, departure_date, direct_only=True,
                         max_results=max_per_leg)
    if not andata:
        return []

    if not return_date:
        return [
            FlightOption(price_eur=o.price_eur, departure_time=o.time,
                         return_time=None, airlines=o.airlines)
            for o in andata[:max_results]
        ]

    ritorno = search_leg(destination, origin, return_date, direct_only=True,
                          max_results=max_per_leg)
    if not ritorno:
        return []

    combinazioni = []
    for o in andata:
        for r in ritorno:
            combinazioni.append(FlightOption(
                price_eur=o.price_eur + r.price_eur,
                departure_time=o.time,
                return_time=r.time,
                airlines=sorted(set(o.airlines) | set(r.airlines)),
            ))

    combinazioni.sort(key=lambda c: c.price_eur)
    return combinazioni[:max_results]
