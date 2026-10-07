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
from types import SimpleNamespace
from unittest.mock import Mock, patch

import primp
import pytest

from scrapers.google_flights import (
    GOOGLE_FLIGHTS_URL,
    HTTP_TIMEOUT_SECONDS,
    ScraperError,
    LegOption,
    parse as real_parse,
    _fetch_html,
    _request,
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

# Altra forma di risposta valida senza itinerari: lista vuota invece di None.
_empty_list_payload = [None] * 8
_empty_list_payload[3] = [[]]
_empty_list_payload[7] = [None, [[], []]]
EMPTY_LIST_RESULTS_PAGE = _ds1_page(json.dumps(_empty_list_payload))

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
            "https://consent.google.com/save",
            timeout=HTTP_TIMEOUT_SECONDS, data={"gl": "IT"})


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
    """
    Una risposta VALIDA senza voli resta [] (contratto pubblico invariato).
    Una risposta di errore di Google (FlightsNotFound / errorHasStatus) NON e'
    "nessun volo": e' un ScraperError (vedi anche TestFlightsNotFoundIsScraperError).
    """

    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._fetch_html")
    def test_flights_not_found_is_scraper_error_not_no_flights(self, mock_fetch, mock_parse):
        original = FlightsNotFound("no flights found")
        mock_fetch.return_value = "<html></html>"
        mock_parse.side_effect = original

        with pytest.raises(ScraperError) as excinfo:
            search_leg("PSA", "CAG", DEPARTURE)

        assert excinfo.value.__cause__ is original

    @patch("scrapers.google_flights._fetch_html")
    def test_real_parser_google_error_payload_is_scraper_error(self, mock_fetch):
        mock_fetch.return_value = GOOGLE_ERROR_PAYLOAD_PAGE

        with pytest.raises(ScraperError) as excinfo:
            search_leg("PSA", "CAG", DEPARTURE)

        assert isinstance(excinfo.value.__cause__, FlightsNotFound)

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
    def test_http_200_google_error_payload_is_scraper_error_not_none(self, mock_client):
        mock_client.get.return_value = _response(text=GOOGLE_ERROR_PAYLOAD_PAGE)

        with pytest.raises(ScraperError) as excinfo:
            scrape_price("PSA", "CAG", DEPARTURE)

        assert isinstance(excinfo.value.__cause__, FlightsNotFound)

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


# ------------------------------------------------------------- timeout HTTP ---

class TestHttpTimeoutIsApplied:
    """
    Ogni richiesta HTTP (GET dei risultati, POST del consenso) passa a primp un
    timeout esplicito. Il valore e' scritto in chiaro (30) di proposito: se
    cambia, questi test devono fallire. Verificato a mano contro un server
    locale con primp 2.0.1: get/post accettano `timeout=` (int o float) e lo
    applicano all'intera richiesta, corpo della risposta compreso.
    """

    def test_timeout_constant_is_30_seconds(self):
        assert HTTP_TIMEOUT_SECONDS == 30

    @patch("scrapers.google_flights._client")
    def test_get_passes_timeout_30(self, mock_client):
        mock_client.get.return_value = _response()

        _request("get", "https://example.test/flights", params={"q": "x"})

        mock_client.get.assert_called_once_with(
            "https://example.test/flights", timeout=30, params={"q": "x"})
        assert mock_client.get.call_args.kwargs["timeout"] == 30

    @patch("scrapers.google_flights._client")
    def test_post_passes_timeout_30(self, mock_client):
        mock_client.post.return_value = _response()

        _request("post", "https://example.test/save", data={"a": "b"})

        mock_client.post.assert_called_once_with(
            "https://example.test/save", timeout=30, data={"a": "b"})
        assert mock_client.post.call_args.kwargs["timeout"] == 30

    @patch("scrapers.google_flights._client")
    def test_fetch_html_get_has_timeout_30(self, mock_client):
        mock_client.get.return_value = _response(text="<html>voli</html>")

        _fetch_html(_query())

        mock_client.get.assert_called_once_with(
            GOOGLE_FLIGHTS_URL, timeout=30, params={})

    @patch("scrapers.google_flights._client")
    def test_consent_form_post_has_timeout_30(self, mock_client):
        mock_client.post.return_value = _response(text="<html>risultati</html>")

        _submit_consent_form(CONSENT_HTML)

        mock_client.post.assert_called_once_with(
            "https://consent.google.com/save", timeout=30, data={"gl": "IT"})

    @patch("scrapers.google_flights._client")
    def test_consent_path_through_fetch_html_uses_timeout_on_both_requests(self, mock_client):
        mock_client.get.return_value = _response(text=CONSENT_HTML, url=CONSENT_URL)
        mock_client.post.return_value = _response(text="<html>risultati</html>")

        _fetch_html(_query())

        assert mock_client.get.call_args.kwargs["timeout"] == 30
        assert mock_client.post.call_args.kwargs["timeout"] == 30


class TestHttpTimeoutBecomesScraperError:

    def test_primp_timeout_is_covered_by_primp_error(self):
        """Presupposto di _request(): non serve un except separato per i timeout."""
        assert issubclass(primp.TimeoutError, primp.PrimpError)
        assert issubclass(primp.DNSTimeoutError, primp.PrimpError)

    @pytest.mark.parametrize("method", ["get", "post"])
    @pytest.mark.parametrize("error", [
        primp.TimeoutError("operation timed out"),
        primp.DNSTimeoutError("dns timed out"),
    ], ids=lambda e: type(e).__name__)
    @patch("scrapers.google_flights._client")
    def test_request_timeout_raises_scraper_error_with_cause(self, mock_client, method, error):
        getattr(mock_client, method).side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            _request(method, "https://example.test/x")

        assert excinfo.value.__cause__ is error
        assert type(error).__name__ in str(excinfo.value)
        assert method.upper() in str(excinfo.value)

    @patch("scrapers.google_flights._client")
    def test_timeout_during_google_flights_get_is_scraper_error_not_empty_list(self, mock_client):
        error = primp.TimeoutError("operation timed out")
        mock_client.get.side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            _fetch_html(_query())
        assert excinfo.value.__cause__ is error

        # Dentro search_leg: ScraperError, NON [] ("nessun volo").
        with pytest.raises(ScraperError):
            search_leg("PSA", "CAG", DEPARTURE)

    @pytest.mark.parametrize("call", [
        lambda: search_leg("PSA", "CAG", DEPARTURE),
        lambda: scrape_price("PSA", "CAG", DEPARTURE),
        lambda: search_options("PSA", "CAG", DEPARTURE),
        lambda: scrape_price("PSA", "CAG", DEPARTURE, return_date=RETURN),
        lambda: search_options("PSA", "CAG", DEPARTURE, return_date=RETURN),
    ], ids=["search_leg", "scrape_price", "search_options",
            "scrape_price_rt", "search_options_rt"])
    @patch("scrapers.google_flights._client")
    def test_get_timeout_is_never_none_or_empty_in_public_functions(self, mock_client, call):
        mock_client.get.side_effect = primp.TimeoutError("operation timed out")

        with pytest.raises(ScraperError):
            call()

    @patch("scrapers.google_flights._client")
    def test_timeout_during_consent_post_is_scraper_error(self, mock_client):
        error = primp.TimeoutError("operation timed out")
        mock_client.post.side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            _submit_consent_form(CONSENT_HTML)

        assert excinfo.value.__cause__ is error
        assert "POST" in str(excinfo.value)

    @patch("scrapers.google_flights._client")
    def test_timeout_during_consent_post_is_not_reported_as_no_flights(self, mock_client):
        """GET ok ma redirect al consenso, POST in timeout: errore, non [] / None."""
        mock_client.get.return_value = _response(text=CONSENT_HTML, url=CONSENT_URL)
        mock_client.post.side_effect = primp.TimeoutError("operation timed out")

        with pytest.raises(ScraperError):
            search_leg("PSA", "CAG", DEPARTURE)
        with pytest.raises(ScraperError):
            scrape_price("PSA", "CAG", DEPARTURE)
        with pytest.raises(ScraperError):
            search_options("PSA", "CAG", DEPARTURE)

    @patch("scrapers.google_flights._client")
    def test_no_flights_without_timeout_is_still_empty(self, mock_client):
        """Controllo di contrasto: stessa catena, nessun timeout -> contratto 'nessun volo' intatto."""
        mock_client.get.return_value = _response(text=EMPTY_RESULTS_PAGE)

        assert search_leg("PSA", "CAG", DEPARTURE) == []
        assert scrape_price("PSA", "CAG", DEPARTURE) is None
        assert search_options("PSA", "CAG", DEPARTURE) == []


class TestTimeoutOnSecondRequestOnly:
    """
    Round-trip = DUE richieste GET. La prima riesce, la seconda (tratta di
    ritorno) va in timeout: l'errore non deve diventare None / [] e non deve
    produrre un prezzo di sola andata.
    """

    @staticmethod
    def _one_flight():
        return SimpleNamespace(
            price="50.00",
            airlines=["Ryanair"],
            flights=[SimpleNamespace(departure=SimpleNamespace(time=(18, 30)))],
        )

    @pytest.mark.parametrize("call", [
        lambda: scrape_price("PSA", "CAG", DEPARTURE, return_date=RETURN),
        lambda: search_options("PSA", "CAG", DEPARTURE, return_date=RETURN),
    ], ids=["scrape_price", "search_options"])
    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._client")
    def test_return_leg_timeout_raises_scraper_error(self, mock_client, mock_parse, call):
        error = primp.TimeoutError("operation timed out")
        mock_client.get.side_effect = [_response(text="<html>andata</html>"), error]
        mock_parse.return_value = [self._one_flight()]

        with pytest.raises(ScraperError) as excinfo:
            call()

        assert excinfo.value.__cause__ is error
        # Il timeout e' avvenuto davvero sulla seconda richiesta, non sulla prima.
        assert mock_client.get.call_count == 2
        assert all(c.kwargs["timeout"] == 30 for c in mock_client.get.call_args_list)


# ------------------------------------------- FlightsNotFound = errore tecnico ---

OUTBOUND_HTML = "<html>andata</html>"


def _valid_flight(price=50):
    """Itinerario valido nella forma che search_leg legge da parse()."""
    return SimpleNamespace(
        price=f"{price:.2f}",
        airlines=["Ryanair"],
        flights=[SimpleNamespace(departure=SimpleNamespace(time=(18, 30)))],
    )


def _public_call(function, round_trip):
    if function == "scrape_price":
        fn = scrape_price
    else:
        fn = search_options
    if round_trip:
        return lambda: fn("PSA", "CAG", DEPARTURE, return_date=RETURN)
    return lambda: fn("PSA", "CAG", DEPARTURE)


class TestFlightsNotFoundIsScraperError:
    """
    fast-flights solleva FlightsNotFound solo su un payload di errore di Google
    (errorHasStatus: true); una risposta valida senza voli e' una lista vuota.
    Qui FlightsNotFound = errore tecnico: ScraperError con causa originale e
    mai un [] / None / prezzo parziale.
    """

    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._fetch_html")
    def test_search_leg_error_has_cause_and_useful_message(self, mock_fetch, mock_parse):
        original = FlightsNotFound("no flights found; received error")
        mock_fetch.return_value = "<html></html>"
        mock_parse.side_effect = original

        with pytest.raises(ScraperError) as excinfo:
            search_leg("PSA", "CAG", DEPARTURE)

        assert excinfo.value.__cause__ is original
        assert "FlightsNotFound" in str(excinfo.value)
        assert "errorHasStatus" in str(excinfo.value)
        # RuntimeError: compatibile con chi gia' catturava gli errori tecnici.
        assert isinstance(excinfo.value, RuntimeError)

    # ---- A / B / E: tratta di andata e di ritorno, funzioni pubbliche ----
    @pytest.mark.parametrize("function", ["scrape_price", "search_options"])
    @pytest.mark.parametrize("round_trip", [False, True], ids=["one_way", "round_trip"])
    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._fetch_html")
    def test_outbound_flights_not_found_raises_scraper_error(
            self, mock_fetch, mock_parse, function, round_trip):
        original = FlightsNotFound("no flights found; received error")
        mock_fetch.return_value = "<html></html>"
        mock_parse.side_effect = [original]          # la PRIMA ricerca (andata) fallisce

        with pytest.raises(ScraperError) as excinfo:
            _public_call(function, round_trip)()

        assert excinfo.value.__cause__ is original

    @pytest.mark.parametrize("function", ["scrape_price", "search_options"])
    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._fetch_html")
    def test_return_flights_not_found_raises_scraper_error_without_partial_result(
            self, mock_fetch, mock_parse, function):
        original = FlightsNotFound("no flights found; received error")
        mock_fetch.return_value = "<html></html>"
        # andata OK, ritorno in errore: nessun prezzo/opzione parziale, solo eccezione.
        mock_parse.side_effect = [[_valid_flight()], original]

        with pytest.raises(ScraperError) as excinfo:
            _public_call(function, True)()

        assert excinfo.value.__cause__ is original
        assert mock_parse.call_count == 2            # il ritorno e' stato davvero cercato

    # ---- A / B / E con il parser REALE (si mocka solo la rete) ----
    @pytest.mark.parametrize("function", ["scrape_price", "search_options"])
    @pytest.mark.parametrize("failing_leg", ["outbound", "return"])
    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._client")
    def test_real_parser_error_page_on_one_leg_raises_scraper_error(
            self, mock_client, mock_parse, function, failing_leg):
        mock_parse.side_effect = (
            lambda html: [_valid_flight()] if html == OUTBOUND_HTML else real_parse(html))
        if failing_leg == "outbound":
            pages = [_response(text=GOOGLE_ERROR_PAYLOAD_PAGE)]
        else:
            pages = [_response(text=OUTBOUND_HTML), _response(text=GOOGLE_ERROR_PAYLOAD_PAGE)]
        mock_client.get.side_effect = pages

        with pytest.raises(ScraperError) as excinfo:
            _public_call(function, True)()

        assert isinstance(excinfo.value.__cause__, FlightsNotFound)
        assert mock_client.get.call_count == len(pages)

    # ---- C: una risposta valida senza voli resta [] / None ----
    @pytest.mark.parametrize("page", [EMPTY_RESULTS_PAGE, EMPTY_LIST_RESULTS_PAGE],
                             ids=["payload3_none", "payload3_empty_list"])
    @patch("scrapers.google_flights._fetch_html")
    def test_valid_empty_response_is_empty_list_unlike_error_response(self, mock_fetch, page):
        mock_fetch.return_value = page
        assert search_leg("PSA", "CAG", DEPARTURE) == []

        mock_fetch.return_value = GOOGLE_ERROR_PAYLOAD_PAGE      # stessa funzione, risposta di errore
        with pytest.raises(ScraperError):
            search_leg("PSA", "CAG", DEPARTURE)

    @patch("scrapers.google_flights.parse")
    @patch("scrapers.google_flights._client")
    def test_valid_empty_return_leg_is_none_and_empty_list(self, mock_client, mock_parse):
        """Contrasto con il test precedente: ritorno VALIDO ma senza voli -> None / [] (non errore)."""
        mock_parse.side_effect = (
            lambda html: [_valid_flight()] if html == OUTBOUND_HTML else real_parse(html))
        # 4 GET: scrape_price (andata, ritorno) poi search_options (andata, ritorno).
        mock_client.get.side_effect = [
            _response(text=OUTBOUND_HTML), _response(text=EMPTY_RESULTS_PAGE),
            _response(text=OUTBOUND_HTML), _response(text=EMPTY_RESULTS_PAGE),
        ]

        assert scrape_price("PSA", "CAG", DEPARTURE, return_date=RETURN) is None
        assert search_options("PSA", "CAG", DEPARTURE, return_date=RETURN) == []


class TestScraperErrorFromFlightsNotFoundPropagates:
    """
    D / E: se search_leg solleva ScraperError (causa FlightsNotFound), le
    funzioni pubbliche lo propagano IDENTICO: niente [] / None, niente messaggio
    riscritto, niente causa persa.
    """

    @staticmethod
    def _router(failing_origin, error):
        def fake(origin, destination, date, *args, **kwargs):
            if origin == failing_origin:
                raise error
            return [LegOption(price_eur=50.0, time="18:30", airlines=["Ryanair"], stops=0)]
        return fake

    @staticmethod
    def _error():
        original = FlightsNotFound("no flights found; received error")
        error = ScraperError("risposta di errore")
        error.__cause__ = original
        return error, original

    @pytest.mark.parametrize("function", ["scrape_price", "search_options"])
    @pytest.mark.parametrize("failing_origin,round_trip", [
        ("PSA", False), ("PSA", True), ("CAG", True),
    ], ids=["one_way_outbound", "round_trip_outbound", "round_trip_return"])
    @patch("scrapers.google_flights.search_leg")
    def test_scraper_error_is_propagated_unchanged(
            self, mock_leg, function, failing_origin, round_trip):
        error, original = self._error()
        mock_leg.side_effect = self._router(failing_origin, error)

        with pytest.raises(ScraperError) as excinfo:
            _public_call(function, round_trip)()

        assert excinfo.value is error
        assert excinfo.value.__cause__ is original


# --------------------------------- attributi strutturati di ScraperError (status/timeout) ---

class TestScraperErrorStructuredAttributes:
    """
    ScraperError espone `status_code` (HTTP) e `timed_out` (timeout) cosi' che il
    chiamante decida i retry senza conoscere i dettagli di primp. Valgono None / False
    in tutti gli altri casi.
    """

    def test_defaults_and_backward_compatibility(self):
        error = ScraperError("errore generico")

        assert error.status_code is None and error.timed_out is False
        assert str(error) == "errore generico" and error.args == ("errore generico",)
        assert isinstance(error, RuntimeError)
        assert str(ScraperError()) == ""

    def test_attributes_are_keyword_only(self):
        with pytest.raises(TypeError):
            ScraperError("errore", 503)

    @pytest.mark.parametrize("error", [
        primp.TimeoutError("operation timed out"),
        primp.DNSTimeoutError("dns timed out"),
    ], ids=lambda e: type(e).__name__)
    @pytest.mark.parametrize("method", ["get", "post"])
    @patch("scrapers.google_flights._client")
    def test_timeout_sets_timed_out(self, mock_client, error, method):
        getattr(mock_client, method).side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            _request(method, "https://example.test/x")

        assert excinfo.value.timed_out is True
        assert excinfo.value.status_code is None

    @pytest.mark.parametrize("error", [
        primp.ConnectError("connection refused"), primp.DNSError("name not resolved"),
    ], ids=lambda e: type(e).__name__)
    @patch("scrapers.google_flights._client")
    def test_other_network_errors_are_not_timeouts(self, mock_client, error):
        mock_client.get.side_effect = error

        with pytest.raises(ScraperError) as excinfo:
            _request("get", "https://example.test/x")

        assert excinfo.value.timed_out is False
        assert excinfo.value.status_code is None

    @pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 502, 503, 504])
    @patch("scrapers.google_flights._client")
    def test_http_error_sets_status_code(self, mock_client, status):
        mock_client.get.return_value = _response(text="<html>err</html>", status=status)

        with pytest.raises(ScraperError) as excinfo:
            _request("get", "https://example.test/x")

        assert excinfo.value.status_code == status
        assert excinfo.value.timed_out is False
        assert str(status) in str(excinfo.value)             # messaggio invariato

    @patch("scrapers.google_flights._client")
    def test_consent_post_http_error_sets_status_code(self, mock_client):
        mock_client.post.return_value = _response(text="<html>err</html>", status=500)

        with pytest.raises(ScraperError) as excinfo:
            _submit_consent_form(CONSENT_HTML)

        assert excinfo.value.status_code == 500
        assert excinfo.value.timed_out is False

    @patch("scrapers.google_flights._client")
    def test_non_integer_status_is_ignored(self, mock_client):
        """status_code non int (es. oggetto inatteso): None, mai un valore falso."""
        response = Mock()
        response.status_code = Mock()                        # non e' un int
        response.raise_for_status.side_effect = primp.StatusError("HTTP error")
        mock_client.get.return_value = response

        with pytest.raises(ScraperError) as excinfo:
            _request("get", "https://example.test/x")

        assert excinfo.value.status_code is None

    @patch("scrapers.google_flights._client")
    def test_response_without_status_code_gives_none(self, mock_client):
        mock_client.get.return_value = SimpleNamespace(
            raise_for_status=Mock(side_effect=primp.StatusError("HTTP error")))

        with pytest.raises(ScraperError) as excinfo:
            _request("get", "https://example.test/x")

        assert excinfo.value.status_code is None and "?" in str(excinfo.value)

    @pytest.mark.parametrize("page,url", [
        (GOOGLE_ERROR_PAYLOAD_PAGE, "https://www.google.com/travel/flights"),   # FlightsNotFound
        (UNUSABLE_PAGE, "https://www.google.com/travel/flights"),               # errore del parser
        ("<html></html>", CONSENT_URL),                                         # consenso senza form
    ], ids=["flights_not_found", "parser", "consent_structure"])
    @patch("scrapers.google_flights._client")
    def test_non_http_errors_carry_no_status_and_no_timeout(self, mock_client, page, url):
        mock_client.get.return_value = _response(text=page, url=url)

        with pytest.raises(ScraperError) as excinfo:
            search_leg("PSA", "CAG", DEPARTURE)

        assert excinfo.value.status_code is None
        assert excinfo.value.timed_out is False

    @pytest.mark.parametrize("call", [
        lambda: search_leg("PSA", "CAG", DEPARTURE),
        lambda: scrape_price("PSA", "CAG", DEPARTURE),
        lambda: scrape_price("PSA", "CAG", DEPARTURE, return_date=RETURN),
        lambda: search_options("PSA", "CAG", DEPARTURE, return_date=RETURN),
    ], ids=["search_leg", "scrape_price", "scrape_price_rt", "search_options_rt"])
    @patch("scrapers.google_flights._client")
    def test_attributes_reach_the_public_functions_unchanged(self, mock_client, call):
        mock_client.get.return_value = _response(text="<html>err</html>", status=503)
        with pytest.raises(ScraperError) as excinfo:
            call()
        assert excinfo.value.status_code == 503 and excinfo.value.timed_out is False

        mock_client.get.return_value = None
        mock_client.get.side_effect = primp.TimeoutError("operation timed out")
        with pytest.raises(ScraperError) as excinfo:
            call()
        assert excinfo.value.timed_out is True and excinfo.value.status_code is None
