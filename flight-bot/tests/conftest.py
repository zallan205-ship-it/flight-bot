"""
Pytest configuration e fixture globali per la suite di test dello scraper Google Flights.
"""
import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime


@pytest.fixture
def mock_client():
    """Mock del client HTTP (primp.Client)."""
    with patch("scrapers.google_flights._client") as mock:
        yield mock


@pytest.fixture
def mock_parse():
    """Mock della funzione parse di fast-flights."""
    with patch("scrapers.google_flights.parse") as mock:
        yield mock


@pytest.fixture
def mock_fetch_html():
    """Mock di _fetch_html."""
    with patch("scrapers.google_flights._fetch_html") as mock:
        yield mock


@pytest.fixture
def sample_date():
    """Una data di esempio per i test."""
    return datetime(2026, 12, 25)


@pytest.fixture
def sample_parsed_flight():
    """Una struttura mockate per un risultato di parsed flight da fast-flights.parse()."""
    flight = Mock()
    flight.price = "50.00"
    flight.airlines = ["Ryanair"]
    
    segment = Mock()
    segment.departure.time = (9, 30)
    flight.flights = [segment]
    
    return flight


@pytest.fixture
def sample_parsed_flight_two_stops():
    """Una struttura mockate per un volo con 2 segmenti (1 scalo)."""
    flight = Mock()
    flight.price = "75.00"
    flight.airlines = ["Lufthansa"]
    
    seg1 = Mock()
    seg1.departure.time = (8, 0)
    seg2 = Mock()
    seg2.departure.time = (10, 30)
    flight.flights = [seg1, seg2]
    
    return flight


@pytest.fixture
def sample_parsed_flight_three_stops():
    """Una struttura mockate per un volo con 3 segmenti (2 scali)."""
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
    
    return flight
