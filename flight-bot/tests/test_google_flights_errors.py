"""
Test della gestione degli errori tecnici dello scraper Google Flights.

Obiettivo: un errore tecnico (rete, HTTP, consenso, parsing) NON deve mai essere
confondibile con "nessun volo trovato".

    nessun volo      -> search_leg: []   scrape_price: None   search_options: []
    errore tecnico   -> ScraperError (con l'eccezione originale in __cause__)

Tutti i test sono offline: si mocka solo il client HTTP (scrapers.google_flights._client).
Dove serve il comportamento reale del parser si usa fast_flights.parser.parse
(vera) su HTML sintetici: la pagina di errore non ha lo <script class="ds:1">,
il payload "errorHasStatus" e il payload vuoto riproducono i tre casi che
parse() distingue nella versione di fast-flights attualmente in uso.
"""
import json
from datetime import datetime
from unittest.mock import Mock, patch

import primp
import pytest

from scrapers.google_flights import (
    ScraperError,
    LegOption,
    _fetch_html,
    _submit_consent_form,
    scrape_price,
    search_leg,
    search_options,
)
from fast_flights.exceptions import FlightsNotFound


DEPARTURE = datetime(2026, 12, 25)
RETURN = datetime(2026, 12, 31)
LEG = LegOption(price_eur=50.0, time="18:30", airlines=["Ryanair"], stops=0)

CONSENT_URL = "https://consent.google.com/m?continue=https://www.google.com/travel/flights"
CONSENT_HTML = (
    '<html><body><form action="/save" method="POST">'
    '<input type="hidden" name="gl" value="IT">'
    '<button type="submit">Accetta tutto</button></form></body></html>'
)


# ---------------------------------------------------------------- helper ----

def _response(text="<html>ok</html>", url="https://www.google.com/travel/flights", status=200):
    """
    Risposta finta con l'interfaccia che lo scraper usa (.text .url .status_code
    .raise_for_status()). Come nel primp reale (verificato contro un server
    locale) una risposta 4xx/5xx NON solleva da sola: e' raise_for_status() a
    sollevare primp.StatusError.
    """
    resp = Mock()
    resp.text = text
    resp.url = url
    resp.status_code = status
    if status >= 400:
        resp.raise_for_status.side_effect = primp.StatusError(f"HTTP {status}")
    return resp


def _query():
    q = Mock()
    q.params.return_value = {}
    return q


def _ds1_page(js_data: str) -> str:
    """Pagina con lo <script class="ds:1"> che fast_flights.parser.parse() cerca."""
    return (
        "<html><body><script class=\"ds:1\">"
        f"AF_initDataCallback({{key: 'ds:1', hash: '2', data:{js_data}, sideChannel: {{}}}});"
        "</script></body></html>"
    )


# Google segnala un errore: parse() solleva FlightsNotFound (contratto attuale: "nessun volo").
GOOGLE_ERROR_PAYLOAD_PAGE = _ds1_page("errorHasStatus: true")

# Risposta valida ma senza itinerari: parse() restituisce una lista vuota.
_empty_payload = [None] * 8
_empty_payload[3] = [None]
_empty_payload[7] = [None, [[], []]]
EMPTY_RESULTS_PAGE = _ds1_page(json.dumps(_empty_payload))

# Pagina che non e' quella dei risultati (captcha, blocco, errore Google...).
UNUSABLE_PAGE = "<html><body>Il nostro sistema ha rilevato traffico insolito</body></html>"


# --------------------------------------------------- 1-2. rete e HTTP --------

class TestFetchHtmlNetworkAndHttpFailures:

    @pytest.mark.parametrize("error", [
        primp.TimeoutError("operation timed out"),
        primp.ConnectError("connection refused"),
        primp.DNSError("dns lookup failed"),
    ], ids=lambda e: type(e).__name__)
    @patch("scrapers.google_flights._client")
    def test_network_exception_becomes_scraper_error(self, mock_client, error):
        mock_client.get.side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            _fetch_html(_query())

        assert excinfo.value.__cause__ is error
        assert type(error).__name__ in str(excinfo.value)

    @pytest.mark.parametrize("status", [403, 429, 500, 503])
    @patch("scrapers.google_flights._client")
    def test_http_error_status_becomes_scraper_error(self, mock_client, status):
        mock_client.get.return_value = _response(
            text="<html>Too Many Requests</html>", status=status)

        with pytest.raises(ScraperError) as excinfo:
            _fetch_html(_query())

        assert str(status) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, primp.StatusError)

    @patch("scrapers.google_flights._client")
    def test_http_error_page_is_never_returned_as_html(self, mock_client):
        """La pagina di errore HTTP non deve arrivare al parser come se fosse HTML valido."""
        mock_client.get.return_value = _response(text="<html>503</html>", status=503)

        with patch("scrapers.google_flights.parse") as mock_parse:
            with pytest.raises(ScraperError):
                search_leg("PSA", "CAG", DEPARTURE)

        mock_parse.assert_not_called()

    @patch("scrapers.google_flights._client")
    def test_successful_response_is_still_returned(self, mock_client):
        response = _response(text="<html>voli</html>")
        mock_client.get.return_value = response

        assert _fetch_html(_query()) == "<html>voli</html>"
        response.raise_for_status.assert_called_once()


# ------------------------------------------------------------- 4. consenso ---

class TestConsentTechnicalFailures:

    @patch("scrapers.google_flights._client")
    def test_consent_post_network_error_is_not_masked(self, mock_client):
        error = primp.TimeoutError("timed out")
        mock_client.get.return_value = _response(text=CONSENT_HTML, url=CONSENT_URL)
        mock_client.post.side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            _fetch_html(_query())

        assert excinfo.value.__cause__ is error

    @patch("scrapers.google_flights._client")
    def test_consent_post_http_error_is_not_masked(self, mock_client):
        mock_client.get.return_value = _response(text=CONSENT_HTML, url=CONSENT_URL)
        mock_client.post.return_value = _response(text="<html>errore</html>", status=500)

        with pytest.raises(ScraperError) as excinfo:
            _fetch_html(_query())

        assert "500" in str(excinfo.value)

    @pytest.mark.parametrize("html", [
        "<html><body>nessun modulo qui</body></html>",
        '<html><body><form><input type="hidden" name="a" value="b"></form></body></html>',
    ], ids=["senza_form", "form_senza_action"])
    @patch("scrapers.google_flights._client")
    def test_consent_page_structure_change_is_scraper_error(self, mock_client, html):
        with pytest.raises(ScraperError) as excinfo:
            _submit_consent_form(html)

        # Compatibilita': prima di ScraperError veniva sollevato RuntimeError.
        assert isinstance(excinfo.value, RuntimeError)
        mock_client.post.assert_not_called()

    @patch("scrapers.google_flights._client")
    def test_consent_failure_is_not_reported_as_no_flights(self, mock_client):
        """Consenso fallito dentro search_leg: ScraperError, non [] ("nessun volo")."""
        mock_client.get.return_value = _response(text=CONSENT_HTML, url=CONSENT_URL)
        mock_client.post.side_effect = primp.ConnectError("refused")

        with pytest.raises(ScraperError):
            search_leg("PSA", "CAG", DEPARTURE)

    @patch("scrapers.google_flights._client")
    def test_consent_success_path_is_unchanged(self, mock_client):
        mock_client.get.return_value = _response(text=CONSENT_HTML, url=CONSENT_URL)
        mock_client.post.return_value = _response(text="<html>risultati</html>")

        assert _fetch_html(_query()) == "<html>risultati</html>"
        mock_client.post.assert_called_once_with(
            "https://consent.google.com/save", data={"gl": "IT"})


# -------------------------------------------------------------- 3. parser ----

class TestParserFailures:

    @pytest.mark.parametrize("error", [
        AttributeError("'NoneType' object has no attribute 'text'"),
        IndexError("list index out of range"),
        json.JSONDecodeError("Expecting value", "", 0),
        KeyError(7),
        TypeError("'NoneType' object is not subscriptable"),
    ], ids=lambda e: type(e).__name__)
    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._fetch_html")
    def test_parser_exception_becomes_scraper_error(self, mock_fetch, mock_parse, error):
        mock_fetch.return_value = "<html></html>"
        mock_parse.side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            search_leg("PSA", "CAG", DEPARTURE)

        assert excinfo.value.__cause__ is error
        assert type(error).__name__ in str(excinfo.value)

    @patch("scrapers.google_flights._fetch_html")
    def test_unexpected_exception_while_fetching_becomes_scraper_error(self, mock_fetch):
        error = OSError("socket error")
        mock_fetch.side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            search_leg("PSA", "CAG", DEPARTURE)

        assert excinfo.value.__cause__ is error

    @patch("scrapers.google_flights._fetch_html")
    def test_scraper_error_from_fetch_is_propagated_unchanged(self, mock_fetch):
        error = ScraperError("rete")
        mock_fetch.side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            search_leg("PSA", "CAG", DEPARTURE)

        assert excinfo.value is error

    @patch("scrapers.google_flights._fetch_html")
    def test_real_parser_on_unusable_page_is_scraper_error(self, mock_fetch):
        """parse() reale su una pagina senza risultati (captcha/blocco): errore, non []."""
        mock_fetch.return_value = UNUSABLE_PAGE

        with pytest.raises(ScraperError) as excinfo:
            search_leg("PSA", "CAG", DEPARTURE)

        assert isinstance(excinfo.value.__cause__, AttributeError)


# ------------------------------------ 5. nessun volo vs errore tecnico -------

class TestNoFlightsIsNotAnError:
    """Il caso "nessun volo" mantiene il contratto pubblico esistente."""

    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._fetch_html")
    def test_flights_not_found_still_means_no_flights(self, mock_fetch, mock_parse):
        mock_fetch.return_value = "<html></html>"
        mock_parse.side_effect = FlightsNotFound("no flights found")

        assert search_leg("PSA", "CAG", DEPARTURE) == []

    @patch("scrapers.google_flights._fetch_html")
    def test_real_parser_google_error_payload_is_no_flights(self, mock_fetch):
        mock_fetch.return_value = GOOGLE_ERROR_PAYLOAD_PAGE

        assert search_leg("PSA", "CAG", DEPARTURE) == []

    @patch("scrapers.google_flights._fetch_html")
    def test_real_parser_empty_results_is_no_flights(self, mock_fetch):
        mock_fetch.return_value = EMPTY_RESULTS_PAGE

        assert search_leg("PSA", "CAG", DEPARTURE) == []

    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._fetch_html")
    def test_empty_parse_result_is_no_flights(self, mock_fetch, mock_parse):
        mock_fetch.return_value = "<html></html>"
        mock_parse.return_value = []

        assert search_leg("PSA", "CAG", DEPARTURE) == []


class TestErrorsPropagateThroughPublicFunctions:
    """scrape_price e search_options non trasformano ScraperError in None / []."""

    @staticmethod
    def _legs(fail_on_origin):
        """Fake di search_leg: la tratta con origine `fail_on_origin` fallisce, l'altra e' valida."""
        def fake(origin, destination, date, *args, **kwargs):
            if origin == fail_on_origin:
                raise ScraperError("errore tecnico")
            return [LEG]
        return fake

    @pytest.mark.parametrize("fail_on_origin", ["PSA", "CAG"], ids=["andata", "ritorno"])
    @patch("scrapers.google_flights.search_leg")
    def test_scrape_price_round_trip_leg_error_is_raised(self, mock_leg, fail_on_origin):
        mock_leg.side_effect = self._legs(fail_on_origin)

        with pytest.raises(ScraperError):
            scrape_price("PSA", "CAG", DEPARTURE, return_date=RETURN)

    @patch("scrapers.google_flights.search_leg")
    def test_scrape_price_one_way_error_is_raised(self, mock_leg):
        mock_leg.side_effect = self._legs("PSA")

        with pytest.raises(ScraperError):
            scrape_price("PSA", "CAG", DEPARTURE)

    @pytest.mark.parametrize("fail_on_origin", ["PSA", "CAG"], ids=["andata", "ritorno"])
    @patch("scrapers.google_flights.search_leg")
    def test_search_options_round_trip_leg_error_is_raised(self, mock_leg, fail_on_origin):
        mock_leg.side_effect = self._legs(fail_on_origin)

        with pytest.raises(ScraperError):
            search_options("PSA", "CAG", DEPARTURE, return_date=RETURN)

    @patch("scrapers.google_flights.search_leg")
    def test_search_options_one_way_error_is_raised(self, mock_leg):
        mock_leg.side_effect = self._legs("PSA")

        with pytest.raises(ScraperError):
            search_options("PSA", "CAG", DEPARTURE)

    @patch("scrapers.google_flights.search_leg")
    def test_no_flights_contract_is_preserved(self, mock_leg):
        mock_leg.return_value = []

        assert scrape_price("PSA", "CAG", DEPARTURE, return_date=RETURN) is None
        assert search_options("PSA", "CAG", DEPARTURE, return_date=RETURN) == []


class TestEndToEndOnlyNetworkMocked:
    """Percorso completo (fetch + parser reale + search_leg + funzioni pubbliche): si mocka solo il client HTTP."""

    @patch("scrapers.google_flights._client")
    def test_http_503_is_error_not_none(self, mock_client):
        mock_client.get.return_value = _response(text=UNUSABLE_PAGE, status=503)

        with pytest.raises(ScraperError):
            scrape_price("PSA", "CAG", DEPARTURE)

    @patch("scrapers.google_flights._client")
    def test_timeout_is_error_not_empty_list(self, mock_client):
        mock_client.get.side_effect = primp.TimeoutError("timed out")

        with pytest.raises(ScraperError):
            search_options("PSA", "CAG", DEPARTURE)

    @patch("scrapers.google_flights._client")
    def test_http_200_with_unusable_page_is_error_not_none(self, mock_client):
        mock_client.get.return_value = _response(text=UNUSABLE_PAGE)

        with pytest.raises(ScraperError):
            scrape_price("PSA", "CAG", DEPARTURE)

    @patch("scrapers.google_flights._client")
    def test_http_200_google_error_payload_is_none(self, mock_client):
        mock_client.get.return_value = _response(text=GOOGLE_ERROR_PAYLOAD_PAGE)

        assert scrape_price("PSA", "CAG", DEPARTURE) is None

    @patch("scrapers.google_flights._client")
    def test_http_200_empty_results_is_none_and_empty_list(self, mock_client):
        mock_client.get.return_value = _response(text=EMPTY_RESULTS_PAGE)

        assert scrape_price("PSA", "CAG", DEPARTURE) is None
        assert search_options("PSA", "CAG", DEPARTURE) == []


class TestScraperErrorType:

    def test_is_runtime_error_and_distinct_from_flights_not_found(self):
        assert issubclass(ScraperError, RuntimeError)
        assert not issubclass(ScraperError, FlightsNotFound)
        assert not issubclass(FlightsNotFound, ScraperError)
