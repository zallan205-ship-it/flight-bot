"""
Test suite per Google Flights scraper - test_google_flights.py

Verifica il comportamento attuale dello scraper con mock di tutte le dipendenze esterne.
"""
import inspect
from types import SimpleNamespace

import pytest
import primp
from unittest.mock import Mock, MagicMock, patch, call
from datetime import datetime

# Importa le funzioni e classi da testare
from scrapers.google_flights import (
    _format_time,
    _build_one_way_query,
    search_leg,
    scrape_price,
    search_options,
    _submit_consent_form,
    _fetch_html,
    LegOption,
    HTTP_TIMEOUT_SECONDS,
)
from scrapers import ScrapedPrice, FlightOption
from fast_flights.exceptions import FlightsNotFound


class TestFormatTime:
    """TEST 1: Verifica _format_time() con vari input."""
    
    def test_format_time_single_digit_hours_and_minutes(self):
        """(9, 5) -> "09:05" """
        result = _format_time((9, 5))
        assert result == "09:05"
    
    def test_format_time_double_digit_hours_and_minutes(self):
        """(18, 30) -> "18:30" """
        result = _format_time((18, 30))
        assert result == "18:30"
    
    def test_format_time_none_input(self):
        """None -> None"""
        result = _format_time(None)
        assert result is None
    
    def test_format_time_midnight(self):
        """(0, 0) -> "00:00" """
        result = _format_time((0, 0))
        assert result == "00:00"
    
    def test_format_time_late_evening(self):
        """(23, 59) -> "23:59" """
        result = _format_time((23, 59))
        assert result == "23:59"


class TestSearchLegStops:
    """TEST 2: Verifica calcolo scali in search_leg()."""
    
    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights.parse")
    def test_search_leg_direct_flight_zero_stops(self, mock_parse, mock_fetch_html):
        """1 segmento -> 0 scali"""
        # Prepara mock
        flight = Mock()
        flight.price = "50.00"
        flight.airlines = ["Ryanair"]
        
        seg1 = Mock()
        seg1.departure.time = (9, 30)
        flight.flights = [seg1]
        
        mock_parse.return_value = [flight]
        mock_fetch_html.return_value = "<html></html>"
        
        # Esegui
        result = search_leg("PSA", "CAG", datetime(2026, 12, 25))
        
        # Verifica
        assert len(result) == 1
        assert result[0].stops == 0
        assert result[0].price_eur == 50.0
    
    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights.parse")
    def test_search_leg_one_stop(self, mock_parse, mock_fetch_html):
        """2 segmenti -> 1 scalo"""
        flight = Mock()
        flight.price = "75.00"
        flight.airlines = ["Lufthansa"]
        
        seg1 = Mock()
        seg1.departure.time = (8, 0)
        seg2 = Mock()
        seg2.departure.time = (10, 30)
        flight.flights = [seg1, seg2]
        
        mock_parse.return_value = [flight]
        mock_fetch_html.return_value = "<html></html>"
        
        result = search_leg("PSA", "CAG", datetime(2026, 12, 25))
        
        assert len(result) == 1
        assert result[0].stops == 1
        assert result[0].price_eur == 75.0
    
    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights.parse")
    def test_search_leg_two_stops(self, mock_parse, mock_fetch_html):
        """3 segmenti -> 2 scali"""
        flight = Mock()
        flight.price = "100.00"
        flight.airlines = ["Air France"]
        
        seg1 = Mock()
        seg1.departure.time = (7, 0)
        seg2 = Mock()
        seg2.departure.time = (9, 15)
        seg3 = Mock()
        seg3.departure.time = (11, 45)
        flight.flights = [seg1, seg2, seg3]
        
        mock_parse.return_value = [flight]
        mock_fetch_html.return_value = "<html></html>"
        
        result = search_leg("PSA", "CAG", datetime(2026, 12, 25))
        
        assert len(result) == 1
        assert result[0].stops == 2
        assert result[0].price_eur == 100.0


class TestDirectOnly:
    """TEST 3: Verifica filtro direct_only."""
    
    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights.parse")
    def test_direct_only_false_keeps_all_flights(self, mock_parse, mock_fetch_html):
        """direct_only=False mantiene risultati diretti e con scalo"""
        # Flight diretto
        flight_direct = Mock()
        flight_direct.price = "50.00"
        flight_direct.airlines = ["Ryanair"]
        seg1 = Mock()
        seg1.departure.time = (9, 30)
        flight_direct.flights = [seg1]
        
        # Flight con scalo
        flight_with_stop = Mock()
        flight_with_stop.price = "45.00"
        flight_with_stop.airlines = ["Air France"]
        seg2 = Mock()
        seg2.departure.time = (8, 0)
        seg3 = Mock()
        seg3.departure.time = (10, 30)
        flight_with_stop.flights = [seg2, seg3]
        
        mock_parse.return_value = [flight_with_stop, flight_direct]
        mock_fetch_html.return_value = "<html></html>"
        
        result = search_leg("PSA", "CAG", datetime(2026, 12, 25), direct_only=False)
        
        assert len(result) == 2
        assert result[0].stops == 1  # Più economico, con scalo
        assert result[1].stops == 0  # Diretto
    
    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights.parse")
    def test_direct_only_true_filters_stops(self, mock_parse, mock_fetch_html):
        """direct_only=True elimina i risultati con scalo"""
        # Flight diretto
        flight_direct = Mock()
        flight_direct.price = "50.00"
        flight_direct.airlines = ["Ryanair"]
        seg1 = Mock()
        seg1.departure.time = (9, 30)
        flight_direct.flights = [seg1]
        
        # Flight con scalo
        flight_with_stop = Mock()
        flight_with_stop.price = "45.00"
        flight_with_stop.airlines = ["Air France"]
        seg2 = Mock()
        seg2.departure.time = (8, 0)
        seg3 = Mock()
        seg3.departure.time = (10, 30)
        flight_with_stop.flights = [seg2, seg3]
        
        mock_parse.return_value = [flight_with_stop, flight_direct]
        mock_fetch_html.return_value = "<html></html>"
        
        result = search_leg("PSA", "CAG", datetime(2026, 12, 25), direct_only=True)
        
        assert len(result) == 1
        assert result[0].stops == 0


class TestPriceSorting:
    """TEST 4: Verifica ordinamento per prezzo."""
    
    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights.parse")
    def test_search_leg_sorts_by_price(self, mock_parse, mock_fetch_html):
        """Risultati ordinati 50, 75, 100 per prezzo crescente"""
        flights = []
        prices = [100, 50, 75]
        
        for price in prices:
            flight = Mock()
            flight.price = str(float(price))
            flight.airlines = ["TestAirline"]
            seg = Mock()
            seg.departure.time = (9, 0)
            flight.flights = [seg]
            flights.append(flight)
        
        mock_parse.return_value = flights
        mock_fetch_html.return_value = "<html></html>"
        
        result = search_leg("PSA", "CAG", datetime(2026, 12, 25))
        
        assert len(result) == 3
        assert result[0].price_eur == 50.0
        assert result[1].price_eur == 75.0
        assert result[2].price_eur == 100.0


class TestMaxResults:
    """TEST 5: Verifica max_results limita il numero di risultati."""
    
    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights.parse")
    def test_search_leg_max_results_limit(self, mock_parse, mock_fetch_html):
        """max_results=2 restituisce al massimo due risultati"""
        flights = []
        for price in [50, 75, 100, 125]:
            flight = Mock()
            flight.price = str(float(price))
            flight.airlines = ["TestAirline"]
            seg = Mock()
            seg.departure.time = (9, 0)
            flight.flights = [seg]
            flights.append(flight)
        
        mock_parse.return_value = flights
        mock_fetch_html.return_value = "<html></html>"
        
        result = search_leg("PSA", "CAG", datetime(2026, 12, 25), max_results=2)
        
        assert len(result) == 2
        assert result[0].price_eur == 50.0
        assert result[1].price_eur == 75.0


def _parsed_flight(price, airlines, *segment_times):
    """Itinerario nella forma che search_leg legge da fast_flights.parse():
    .price, .airlines, .flights[i].departure.time. SimpleNamespace (non Mock):
    se il codice legge un attributo non previsto il test fallisce."""
    return SimpleNamespace(
        price=f"{price:.2f}",
        airlines=airlines,
        flights=[SimpleNamespace(departure=SimpleNamespace(time=t)) for t in segment_times],
    )


class TestSearchLegFieldMapping:
    """TEST 18: search_leg() mappa correttamente orario e compagnie di ogni volo."""

    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights.parse")
    def test_search_leg_maps_first_segment_time_and_airlines(self, mock_parse, mock_fetch_html):
        # Volo con scalo: due segmenti con orari diversi. L'orario del volo e'
        # quello di partenza del PRIMO segmento (06:10), non dell'ultimo (09:45).
        connecting = _parsed_flight(120, ["Lufthansa", "ITA Airways"], (6, 10), (9, 45))
        direct = _parsed_flight(60, ["Ryanair"], (18, 30))
        # Ordine volutamente non ordinato per prezzo.
        mock_parse.return_value = [connecting, direct]
        mock_fetch_html.return_value = "<html></html>"

        result = search_leg("PSA", "CAG", datetime(2026, 12, 25))

        assert result == [
            LegOption(price_eur=60.0, time="18:30", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=120.0, time="06:10",
                      airlines=["Lufthansa", "ITA Airways"], stops=1),
        ]


class TestSearchLegQueryFilters:
    """TEST 19: search_leg() propaga tratta, data e filtri orari alla query."""

    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights._build_one_way_query")
    def test_search_leg_passes_route_date_and_hour_filters_to_query(
            self, mock_build_query, mock_fetch_html, mock_parse):
        date = datetime(2026, 12, 25)
        query = object()
        mock_build_query.return_value = query
        mock_parse.return_value = []

        search_leg("PSA", "CAG", date, earliest_hour=6, latest_hour=12)

        mock_build_query.assert_called_once()
        assert _bound_args(mock_build_query.call_args, _build_one_way_query) == {
            "origin": "PSA",
            "destination": "CAG",
            "date": date,
            "earliest_hour": 6,
            "latest_hour": 12,
        }
        # La query costruita e' proprio quella usata per scaricare la pagina.
        mock_fetch_html.assert_called_once_with(query)


class TestScrapePriceOneWay:
    """TEST 6: Verifica scrape_price() per ricerca one-way."""
    
    @patch("scrapers.google_flights.search_leg")
    def test_scrape_price_one_way_basic(self, mock_search_leg):
        """Ricerca andata con prezzo €35 restituisce ScrapedPrice corretto"""
        departure = datetime(2026, 12, 25)
        
        leg_option = LegOption(
            price_eur=35.0,
            time="19:35",
            airlines=["Ryanair"],
            stops=0
        )
        mock_search_leg.return_value = [leg_option]
        
        result = scrape_price("PSA", "CAG", departure)
        
        assert result is not None
        assert result.price_eur == 35.0
        assert result.origin == "PSA"
        assert result.destination == "CAG"
        assert result.departure_date == departure
        assert result.return_date is None
        assert result.departure_time == "19:35"
        assert result.return_time is None
        assert result.source == "google_flights"


def _bound_args(mock_call, func=None):
    """Normalizza una call in {nome_parametro: valore} usando la firma reale di
    `func` (default: search_leg), sia che il chiamante passi gli argomenti in
    modo posizionale o per keyword."""
    func = func or search_leg
    bound = inspect.signature(func).bind(*mock_call.args, **mock_call.kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


def _leg_router(table):
    """
    Fake di search_leg che sceglie la risposta in base a (origin, destination,
    date) e NON all'ordine delle chiamate: se scrape_price scambia le tratte o
    usa la data sbagliata, il test fallisce invece di passare per caso.
    """
    def fake(origin, destination, date, earliest_hour=None, latest_hour=None,
             direct_only=False, max_results=None):
        return table[(origin, destination, date)]
    return fake


class TestScrapePriceRoundTrip:
    """TEST 7: Verifica scrape_price() per ricerca round-trip."""

    @patch("scrapers.google_flights.search_leg")
    def test_scrape_price_round_trip(self, mock_search_leg):
        """
        Andata (PSA->CAG) 50 EUR 18:30, ritorno (CAG->PSA) 70 EUR 20:15.
        Prezzo, orari e mapping delle tratte devono restare distinti.
        """
        departure = datetime(2026, 12, 25)
        return_date = datetime(2026, 12, 31)

        outbound = LegOption(price_eur=50.0, time="18:30", airlines=["Ryanair"], stops=0)
        outbound_pricier = LegOption(price_eur=90.0, time="07:00", airlines=["ITA"], stops=0)
        inbound = LegOption(price_eur=70.0, time="20:15", airlines=["Wizz Air"], stops=0)
        inbound_pricier = LegOption(price_eur=95.0, time="06:10", airlines=["ITA"], stops=0)

        mock_search_leg.side_effect = _leg_router({
            ("PSA", "CAG", departure): [outbound, outbound_pricier],
            ("CAG", "PSA", return_date): [inbound, inbound_pricier],
        })

        result = scrape_price(
            "PSA", "CAG", departure, return_date=return_date,
            earliest_departure_hour=6, latest_departure_hour=12,
            earliest_return_hour=17, latest_return_hour=22,
            direct_only=True,
        )

        assert result is not None
        # 50 + 70: distinguibile da 50+50 (100), 70+70 (140), 50+90 (140), ecc.
        assert result.price_eur == 120.0
        assert result.departure_time == "18:30"
        assert result.return_time == "20:15"
        assert result.origin == "PSA"
        assert result.destination == "CAG"
        assert result.departure_date == departure
        assert result.return_date == return_date
        assert result.source == "google_flights"

        # Mapping delle due ricerche: tratta, data, filtri orari e direct_only
        assert mock_search_leg.call_count == 2
        first = _bound_args(mock_search_leg.call_args_list[0])
        second = _bound_args(mock_search_leg.call_args_list[1])

        assert (first["origin"], first["destination"], first["date"]) == ("PSA", "CAG", departure)
        assert (first["earliest_hour"], first["latest_hour"]) == (6, 12)
        assert first["direct_only"] is True

        assert (second["origin"], second["destination"], second["date"]) == ("CAG", "PSA", return_date)
        assert (second["earliest_hour"], second["latest_hour"]) == (17, 22)
        assert second["direct_only"] is True


class TestScrapePriceReturnMissing:
    """TEST 8: Verifica scrape_price() quando ritorno è assente."""
    
    @patch("scrapers.google_flights.search_leg")
    def test_scrape_price_return_empty(self, mock_search_leg):
        """Andata disponibile ma ritorno vuoto -> None"""
        departure = datetime(2026, 12, 25)
        return_date = datetime(2026, 12, 31)
        
        leg_andata = LegOption(
            price_eur=30.0,
            time="08:00",
            airlines=["Ryanair"],
            stops=0
        )
        
        mock_search_leg.side_effect = [[leg_andata], []]  # Ritorno vuoto
        
        result = scrape_price("PSA", "CAG", departure, return_date=return_date)
        
        assert result is None


class TestScrapePriceDepartureMissing:
    """TEST 9: Verifica scrape_price() quando andata è assente."""
    
    @patch("scrapers.google_flights.search_leg")
    def test_scrape_price_departure_empty(self, mock_search_leg):
        """Andata vuota -> None"""
        departure = datetime(2026, 12, 25)
        
        mock_search_leg.return_value = []  # Andata vuota
        
        result = scrape_price("PSA", "CAG", departure)
        
        assert result is None


class TestScrapePriceMissingLegsRoundTrip:
    """
    TEST 8b: comportamento ATTUALE di scrape_price() se una sola tratta di una
    ricerca round-trip e' vuota: ritorna None (nessun risultato parziale).
    """

    DEPARTURE = datetime(2026, 12, 25)
    RETURN = datetime(2026, 12, 31)
    VALID_OUT = LegOption(price_eur=50.0, time="18:30", airlines=["Ryanair"], stops=0)
    VALID_RET = LegOption(price_eur=70.0, time="20:15", airlines=["Wizz Air"], stops=0)

    @patch("scrapers.google_flights.search_leg")
    def test_outbound_empty_return_valid_returns_none(self, mock_search_leg):
        """outbound = [], return = [valido] -> None"""
        mock_search_leg.side_effect = _leg_router({
            ("PSA", "CAG", self.DEPARTURE): [],
            ("CAG", "PSA", self.RETURN): [self.VALID_RET],
        })

        result = scrape_price("PSA", "CAG", self.DEPARTURE, return_date=self.RETURN)

        assert result is None

    @patch("scrapers.google_flights.search_leg")
    def test_outbound_valid_return_empty_returns_none(self, mock_search_leg):
        """outbound = [valido], return = [] -> None (non un prezzo di sola andata)"""
        mock_search_leg.side_effect = _leg_router({
            ("PSA", "CAG", self.DEPARTURE): [self.VALID_OUT],
            ("CAG", "PSA", self.RETURN): [],
        })

        result = scrape_price("PSA", "CAG", self.DEPARTURE, return_date=self.RETURN)

        assert result is None


class TestSearchOptionsOneWay:
    """TEST 10: Verifica search_options() produce FlightOption corretti (one-way)."""
    
    @patch("scrapers.google_flights.search_leg")
    def test_search_options_one_way(self, mock_search_leg):
        """search_options one-way crea FlightOption corretti"""
        departure = datetime(2026, 12, 25)
        
        leg_options = [
            LegOption(price_eur=30.0, time="08:00", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=40.0, time="18:00", airlines=["Air France"], stops=0),
        ]
        mock_search_leg.return_value = leg_options
        
        result = search_options("PSA", "CAG", departure)
        
        assert len(result) == 2
        assert isinstance(result[0], FlightOption)
        assert result[0].price_eur == 30.0
        assert result[0].departure_time == "08:00"
        assert result[0].return_time is None
        assert result[1].price_eur == 40.0


class TestSearchOptionsRoundTrip:
    """TEST 11: Verifica search_options() crea tutte le combinazioni round-trip."""
    
    @patch("scrapers.google_flights.search_leg")
    def test_search_options_round_trip_combinations(self, mock_search_leg):
        """
        2 opzioni andata (€30 08:00, €40 18:00) +
        2 opzioni ritorno (€20 09:00, €25 19:00) =
        4 combinazioni ordinate per prezzo
        """
        departure = datetime(2026, 12, 25)
        return_date = datetime(2026, 12, 31)
        
        andata_options = [
            LegOption(price_eur=30.0, time="08:00", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=40.0, time="18:00", airlines=["Air France"], stops=0),
        ]
        
        ritorno_options = [
            LegOption(price_eur=20.0, time="09:00", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=25.0, time="19:00", airlines=["Air France"], stops=0),
        ]
        
        mock_search_leg.side_effect = [andata_options, ritorno_options]
        
        result = search_options("PSA", "CAG", departure, return_date=return_date)
        
        # 2 x 2 = 4 combinazioni
        assert len(result) == 4
        
        # Ordinate per prezzo
        assert result[0].price_eur == 50.0  # 30 + 20
        assert result[1].price_eur == 55.0  # 30 + 25
        assert result[2].price_eur == 60.0  # 40 + 20
        assert result[3].price_eur == 65.0  # 40 + 25


class TestMaxPerLeg:
    """TEST 12: Verifica max_per_leg limita le opzioni per tratta."""
    
    @patch("scrapers.google_flights.search_leg")
    def test_search_options_max_per_leg_passed_to_search_leg(self, mock_search_leg):
        """max_per_leg=2 viene passato a search_leg() come max_results"""
        departure = datetime(2026, 12, 25)
        return_date = datetime(2026, 12, 31)
        
        andata_options = [
            LegOption(price_eur=30.0, time="08:00", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=40.0, time="18:00", airlines=["Air France"], stops=0),
        ]
        
        ritorno_options = [
            LegOption(price_eur=20.0, time="09:00", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=25.0, time="19:00", airlines=["Air France"], stops=0),
        ]
        
        mock_search_leg.side_effect = [andata_options, ritorno_options]
        
        result = search_options("PSA", "CAG", departure, return_date=return_date, max_per_leg=2)
        
        # Verifica che search_leg sia stato chiamato con max_results=2
        assert mock_search_leg.call_count == 2
        
        # Verifica i parametri delle due chiamate
        calls = mock_search_leg.call_args_list
        # Prima chiamata (andata): max_results=2
        assert calls[0].kwargs.get('max_results') == 2
        # Seconda chiamata (ritorno): max_results=2
        assert calls[1].kwargs.get('max_results') == 2
        
        # Verifica risultato finale
        assert len(result) <= 4


class TestSearchOptionsMaxResults:
    """TEST 13: Verifica max_results in search_options()."""
    
    @patch("scrapers.google_flights.search_leg")
    def test_search_options_respects_max_results(self, mock_search_leg):
        """search_options() non restituisce più di max_results"""
        departure = datetime(2026, 12, 25)
        return_date = datetime(2026, 12, 31)
        
        andata_options = [
            LegOption(price_eur=30.0, time="08:00", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=40.0, time="18:00", airlines=["Air France"], stops=0),
            LegOption(price_eur=50.0, time="10:00", airlines=["Lufthansa"], stops=0),
        ]
        
        ritorno_options = [
            LegOption(price_eur=20.0, time="09:00", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=25.0, time="19:00", airlines=["Air France"], stops=0),
            LegOption(price_eur=30.0, time="14:00", airlines=["Lufthansa"], stops=0),
        ]
        
        mock_search_leg.side_effect = [andata_options, ritorno_options]
        
        result = search_options("PSA", "CAG", departure, return_date=return_date, max_results=3)
        
        # max_results=3 limita il risultato finale
        assert len(result) <= 3


class TestSearchOptionsRoundTripMapping:
    """
    TEST 20: search_options() round-trip con fake di search_leg che risponde
    per (origin, destination, date) e non per ordine di chiamata.
    """

    DEPARTURE = datetime(2026, 12, 25)
    RETURN = datetime(2026, 12, 31)

    OUT_CHEAP = LegOption(price_eur=50.0, time="18:30", airlines=["Ryanair"], stops=0)
    OUT_PRICEY = LegOption(price_eur=80.0, time="07:00", airlines=["ITA Airways"], stops=0)
    RET_CHEAP = LegOption(price_eur=70.0, time="20:15", airlines=["Wizz Air"], stops=0)
    RET_PRICEY = LegOption(price_eur=90.0, time="06:10", airlines=["Volotea"], stops=0)

    def _table(self):
        return {
            ("PSA", "CAG", self.DEPARTURE): [self.OUT_CHEAP, self.OUT_PRICEY],
            ("CAG", "PSA", self.RETURN): [self.RET_CHEAP, self.RET_PRICEY],
        }

    @patch("scrapers.google_flights.search_leg")
    def test_combinations_use_correct_leg_times_airlines_and_prices(self, mock_search_leg):
        mock_search_leg.side_effect = _leg_router(self._table())

        result = search_options("PSA", "CAG", self.DEPARTURE, return_date=self.RETURN)

        # Se il ritorno venisse cercato su PSA->CAG il router non troverebbe la
        # chiave e il test fallirebbe. departure_time viene dall'andata,
        # return_time dal ritorno, le compagnie sono l'unione ordinata.
        assert result == [
            FlightOption(price_eur=120.0, departure_time="18:30", return_time="20:15",
                         airlines=["Ryanair", "Wizz Air"]),
            FlightOption(price_eur=140.0, departure_time="18:30", return_time="06:10",
                         airlines=["Ryanair", "Volotea"]),
            FlightOption(price_eur=150.0, departure_time="07:00", return_time="20:15",
                         airlines=["ITA Airways", "Wizz Air"]),
            FlightOption(price_eur=170.0, departure_time="07:00", return_time="06:10",
                         airlines=["ITA Airways", "Volotea"]),
        ]

    @patch("scrapers.google_flights.search_leg")
    def test_both_legs_are_searched_for_direct_flights_only(self, mock_search_leg):
        mock_search_leg.side_effect = _leg_router(self._table())

        search_options("PSA", "CAG", self.DEPARTURE, return_date=self.RETURN)

        assert mock_search_leg.call_count == 2
        outbound_call = _bound_args(mock_search_leg.call_args_list[0])
        return_call = _bound_args(mock_search_leg.call_args_list[1])

        assert (outbound_call["origin"], outbound_call["destination"], outbound_call["date"]) == (
            "PSA", "CAG", self.DEPARTURE)
        assert outbound_call["direct_only"] is True

        assert (return_call["origin"], return_call["destination"], return_call["date"]) == (
            "CAG", "PSA", self.RETURN)
        assert return_call["direct_only"] is True

    @patch("scrapers.google_flights.search_leg")
    def test_empty_return_leg_returns_empty_list(self, mock_search_leg):
        table = self._table()
        table[("CAG", "PSA", self.RETURN)] = []
        mock_search_leg.side_effect = _leg_router(table)

        result = search_options("PSA", "CAG", self.DEPARTURE, return_date=self.RETURN)

        # Niente combinazioni "solo andata" quando il ritorno non esiste.
        assert result == []


class TestSearchOptionsMaxResultsExact:
    """TEST 21: max_results tronca ESATTAMENTE a N quando i risultati bastano."""

    DEPARTURE = datetime(2026, 12, 25)
    RETURN = datetime(2026, 12, 31)

    @patch("scrapers.google_flights.search_leg")
    def test_one_way_max_results_returns_exactly_n_cheapest(self, mock_search_leg):
        legs = [
            LegOption(price_eur=30.0, time="08:00", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=40.0, time="12:15", airlines=["Vueling"], stops=0),
            LegOption(price_eur=50.0, time="16:40", airlines=["Wizz Air"], stops=0),
            LegOption(price_eur=60.0, time="21:05", airlines=["Volotea"], stops=0),
        ]
        mock_search_leg.side_effect = _leg_router({("PSA", "CAG", self.DEPARTURE): legs})

        result = search_options("PSA", "CAG", self.DEPARTURE, max_results=2)

        assert len(result) == 2
        assert result == [
            FlightOption(price_eur=30.0, departure_time="08:00", return_time=None,
                         airlines=["Ryanair"]),
            FlightOption(price_eur=40.0, departure_time="12:15", return_time=None,
                         airlines=["Vueling"]),
        ]

    @patch("scrapers.google_flights.search_leg")
    def test_round_trip_max_results_returns_exactly_n_cheapest_combinations(self, mock_search_leg):
        outbound = [
            LegOption(price_eur=30.0, time="08:00", airlines=["Ryanair"], stops=0),
            LegOption(price_eur=47.0, time="12:30", airlines=["Vueling"], stops=0),
            LegOption(price_eur=100.0, time="17:45", airlines=["ITA Airways"], stops=0),
        ]
        inbound = [
            LegOption(price_eur=20.0, time="09:15", airlines=["Wizz Air"], stops=0),
            LegOption(price_eur=33.0, time="13:05", airlines=["Volotea"], stops=0),
            LegOption(price_eur=95.0, time="21:40", airlines=["Aegean"], stops=0),
        ]
        mock_search_leg.side_effect = _leg_router({
            ("PSA", "CAG", self.DEPARTURE): outbound,
            ("CAG", "PSA", self.RETURN): inbound,
        })

        result = search_options("PSA", "CAG", self.DEPARTURE,
                                return_date=self.RETURN, max_results=4)

        # 3 x 3 = 9 combinazioni: 50, 63, 67, 80, 120, 125, 133, 142, 195.
        # Nell'ordine di generazione le prime 4 sarebbero 50, 63, 125, 67:
        # il test distingue "le 4 piu' economiche" da "le prime 4 generate".
        assert len(result) == 4
        assert result == [
            FlightOption(price_eur=50.0, departure_time="08:00", return_time="09:15",
                         airlines=["Ryanair", "Wizz Air"]),
            FlightOption(price_eur=63.0, departure_time="08:00", return_time="13:05",
                         airlines=["Ryanair", "Volotea"]),
            FlightOption(price_eur=67.0, departure_time="12:30", return_time="09:15",
                         airlines=["Vueling", "Wizz Air"]),
            FlightOption(price_eur=80.0, departure_time="12:30", return_time="13:05",
                         airlines=["Volotea", "Vueling"]),
        ]


class TestFlightsNotFound:
    """TEST 14: Verifica gestione FlightsNotFound."""
    
    @patch("scrapers.google_flights._fetch_html")
    @patch("scrapers.google_flights.parse")
    def test_search_leg_flights_not_found_returns_empty_list(self, mock_parse, mock_fetch_html):
        """FlightsNotFound da parse() -> lista vuota instead of exception"""
        mock_fetch_html.return_value = "<html></html>"
        mock_parse.side_effect = FlightsNotFound()
        
        result = search_leg("PSA", "CAG", datetime(2026, 12, 25))
        
        assert result == []
        assert isinstance(result, list)


class TestSubmitConsentForm:
    """TEST 15: Verifica _submit_consent_form() con vari moduli."""
    
    @patch("scrapers.google_flights._client")
    def test_submit_consent_form_with_accetta_text(self, mock_client_module):
        """Form con testo 'Accetta' viene trovato e inviato"""
        # Prepara HTML della pagina di consenso
        consent_html = """
        <html>
            <form action="/consent_action" method="POST">
                <button>Accetta tutto</button>
                <input name="consent_choice" value="yes">
            </form>
        </html>
        """
        
        # Mock del client.post
        mock_post_response = Mock()
        mock_post_response.text = "<html>risultato</html>"
        mock_client_module.post.return_value = mock_post_response
        
        result = _submit_consent_form(consent_html)
        
        assert result == "<html>risultato</html>"
        # Verifica che post sia stato chiamato
        assert mock_client_module.post.called
    
    @patch("scrapers.google_flights._client")
    def test_submit_consent_form_with_accept_text(self, mock_client_module):
        """Form con testo 'Accept' viene trovato e inviato"""
        consent_html = """
        <html>
            <form action="/consent_action" method="POST">
                <button>Accept all cookies</button>
                <input name="consent_choice" value="yes">
            </form>
        </html>
        """
        
        mock_post_response = Mock()
        mock_post_response.text = "<html>risultato</html>"
        mock_client_module.post.return_value = mock_post_response
        
        result = _submit_consent_form(consent_html)
        
        assert result == "<html>risultato</html>"
        assert mock_client_module.post.called
    
    @patch("scrapers.google_flights._client")
    def test_submit_consent_form_with_agree_text(self, mock_client_module):
        """Form con testo 'Agree' viene trovato e inviato"""
        consent_html = """
        <html>
            <form action="/consent_action" method="POST">
                <button>I agree to all terms</button>
                <input name="consent_choice" value="yes">
            </form>
        </html>
        """
        
        mock_post_response = Mock()
        mock_post_response.text = "<html>risultato</html>"
        mock_client_module.post.return_value = mock_post_response
        
        result = _submit_consent_form(consent_html)
        
        assert result == "<html>risultato</html>"
        assert mock_client_module.post.called
    
    def test_submit_consent_form_no_forms_raises_error(self):
        """Nessun form -> RuntimeError"""
        consent_html = "<html><body>No forms here</body></html>"
        
        with pytest.raises(RuntimeError, match="Pagina di consenso Google senza nessun"):
            _submit_consent_form(consent_html)
    
    @patch("scrapers.google_flights._client")
    def test_submit_consent_form_no_action_raises_error(self, mock_client_module):
        """Form senza action -> RuntimeError"""
        consent_html = """
        <html>
            <form>
                <button>Accetta</button>
                <input name="consent_choice" value="yes">
            </form>
        </html>
        """
        
        with pytest.raises(RuntimeError, match="non ha un URL di invio"):
            _submit_consent_form(consent_html)
    
    @patch("scrapers.google_flights._client")
    def test_submit_consent_form_relative_action_url(self, mock_client_module):
        """Action relativa (/...) viene convertita in URL assoluto"""
        consent_html = """
        <html>
            <form action="/consent_submit" method="POST">
                <button>Accetta</button>
                <input name="consent_choice" value="yes">
            </form>
        </html>
        """
        
        mock_post_response = Mock()
        mock_post_response.text = "<html>risultato</html>"
        mock_client_module.post.return_value = mock_post_response
        
        result = _submit_consent_form(consent_html)
        
        # Verifica che l'URL sia stato costruito correttamente
        calls = mock_client_module.post.call_args_list
        assert len(calls) > 0
        posted_url = calls[0][0][0]
        assert posted_url == "https://consent.google.com/consent_submit"


class TestSubmitConsentFormRealParsing:
    """
    TEST 15b: _submit_consent_form() eseguita per intero, con parsing HTML
    reale (selectolax, la libreria usata dal codice di produzione). Si mocka
    solo il POST di rete (primp.Client.post).
    """

    CONSENT_HTML = """
    <html><body>
      <form action="/save" method="POST">
        <input type="hidden" name="gl" value="IT">
        <input type="hidden" name="set_eom" value="true">
        <input type="hidden" name="bl" value="boq_identityfrontenduiserver_REJECT">
        <button type="submit">Rifiuta tutto</button>
      </form>
      <form action="/save" method="POST">
        <input type="hidden" name="gl" value="IT">
        <input type="hidden" name="m" value="0">
        <input type="hidden" name="continue" value="https://www.google.com/travel/flights?hl=it">
        <input type="hidden" name="set_eom" value="false">
        <input type="hidden" name="bl" value="boq_identityfrontenduiserver_ACCEPT">
        <input type="checkbox" name="remember" value="on">
        <input type="text" name="empty_value_field">
        <input type="submit" value="ignored: no name attribute">
        <button type="submit">Accetta tutto</button>
      </form>
    </body></html>
    """

    def test_parses_accept_form_and_posts_its_fields(self):
        with patch.object(primp.Client, "post") as mock_post:
            mock_post.return_value = Mock(text="<html>risultati</html>")

            result = _submit_consent_form(self.CONSENT_HTML)

        assert result == "<html>risultati</html>"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args

        # action relativa risolta contro consent.google.com
        assert args == ("https://consent.google.com/save",)

        # Dati del form "Accetta" (non quelli del form "Rifiuta"): hidden +
        # altri input con name; l'input senza name e' escluso, il value
        # mancante diventa stringa vuota.
        assert kwargs == {"timeout": HTTP_TIMEOUT_SECONDS, "data": {
            "gl": "IT",
            "m": "0",
            "continue": "https://www.google.com/travel/flights?hl=it",
            "set_eom": "false",
            "bl": "boq_identityfrontenduiserver_ACCEPT",
            "remember": "on",
            "empty_value_field": "",
        }}


class TestFetchHtml:
    """TEST 16: Verifica _fetch_html() con vari scenari."""
    
    @patch("scrapers.google_flights._client")
    @patch("scrapers.google_flights._submit_consent_form")
    def test_fetch_html_normal_response(self, mock_consent, mock_client_module):
        """Risposta normale -> restituisce direttamente HTML"""
        mock_response = Mock()
        mock_response.text = "<html>flights</html>"
        mock_response.url = "https://www.google.com/travel/flights"
        mock_client_module.get.return_value = mock_response
        
        mock_query = Mock()
        mock_query.params.return_value = {}
        
        result = _fetch_html(mock_query)
        
        assert result == "<html>flights</html>"
        assert not mock_consent.called
    
    @patch("scrapers.google_flights._client")
    @patch("scrapers.google_flights._submit_consent_form")
    def test_fetch_html_consent_redirect(self, mock_consent, mock_client_module):
        """URL contenente 'consent.google.com' -> chiama _submit_consent_form()"""
        mock_response = Mock()
        mock_response.text = "<html>consent page</html>"
        mock_response.url = "https://consent.google.com/consent"
        mock_client_module.get.return_value = mock_response
        
        mock_consent.return_value = "<html>after consent</html>"
        
        mock_query = Mock()
        mock_query.params.return_value = {}
        
        result = _fetch_html(mock_query)
        
        assert mock_consent.called
        assert result == "<html>after consent</html>"


class TestBuildOneWayQuery:
    """TEST 17: Verifica _build_one_way_query() costruisce query corrette."""
    
    @patch("scrapers.google_flights.create_filter")
    @patch("scrapers.google_flights.FlightQuery")
    def test_build_one_way_query_basic(self, mock_flight_query_cls, mock_create_filter):
        """_build_one_way_query() costruisce FlightQuery con parametri corretti"""
        mock_query_instance = Mock()
        mock_flight_query_cls.return_value = mock_query_instance
        mock_filter = Mock()
        mock_create_filter.return_value = mock_filter
        
        date = datetime(2026, 12, 25)
        result = _build_one_way_query("PSA", "CAG", date)
        
        # Verifica che FlightQuery sia stato chiamato con i parametri giusti
        mock_flight_query_cls.assert_called_once()
        call_args = mock_flight_query_cls.call_args
        assert call_args.kwargs["from_airport"] == "PSA"
        assert call_args.kwargs["to_airport"] == "CAG"
        assert call_args.kwargs["date"] == "2026-12-25"
        assert call_args.kwargs["earliest_departure_hour"] is None
        assert call_args.kwargs["latest_departure_hour"] is None
        
        # Verifica che create_filter sia stato chiamato
        assert mock_create_filter.called
        assert result == mock_filter
    
    @patch("scrapers.google_flights.create_filter")
    @patch("scrapers.google_flights.FlightQuery")
    def test_build_one_way_query_with_hour_filters(self, mock_flight_query_cls, mock_create_filter):
        """_build_one_way_query() include earliest_hour e latest_hour"""
        mock_query_instance = Mock()
        mock_flight_query_cls.return_value = mock_query_instance
        mock_filter = Mock()
        mock_create_filter.return_value = mock_filter
        
        date = datetime(2026, 12, 25)
        result = _build_one_way_query("PSA", "CAG", date, earliest_hour=8, latest_hour=20)
        
        call_args = mock_flight_query_cls.call_args
        assert call_args.kwargs["earliest_departure_hour"] == 8
        assert call_args.kwargs["latest_departure_hour"] == 20
