# Flight Bot Scraper Test Suite

## Overview

This test suite provides comprehensive coverage of the Google Flights scraper (`flight-bot/scrapers/google_flights.py`) with fully mocked external dependencies.

**Important**: These tests are designed to establish a baseline of the current scraper behavior before any modifications are made. They do NOT test for correctness or best practices — only for consistency with the current implementation.

## Setup

### Install Test Dependencies

```bash
pip install -r requirements.txt
```

This will install pytest and pytest-mock, which are required to run the tests.

### Directory Structure

```
flight-bot/
├── tests/
│   ├── __init__.py
│   ├── conftest.py           # Pytest configuration and shared fixtures
│   └── test_google_flights.py # Complete test suite
├── pytest.ini                # Pytest configuration
├── requirements.txt          # Dependencies (includes pytest)
└── scrapers/
    ├── __init__.py
    └── google_flights.py     # Module being tested
```

## Running the Tests

### Run All Tests

```bash
pytest flight-bot/tests/
```

### Run Tests with Verbose Output

```bash
pytest flight-bot/tests/ -v
```

### Run a Specific Test Class

```bash
pytest flight-bot/tests/test_google_flights.py::TestFormatTime -v
```

### Run a Specific Test

```bash
pytest flight-bot/tests/test_google_flights.py::TestFormatTime::test_format_time_single_digit_hours_and_minutes -v
```

### Run Tests with Coverage Report

```bash
pytest flight-bot/tests/ --cov=scrapers.google_flights --cov-report=html
```

## Test Coverage

The test suite includes **17 test classes** covering all critical functions:

### 1. **TestFormatTime** — Format Time Utility (5 tests)
   - Tests `_format_time()` with various input combinations
   - Verifies padding of hours and minutes to 2 digits
   - Tests None handling

### 2. **TestSearchLegStops** — Stop Calculation (3 tests)
   - Verifies stop count calculation (flights segments - 1)
   - Tests: 1 segment → 0 stops, 2 segments → 1 stop, 3 segments → 2 stops

### 3. **TestDirectOnly** — Direct Flights Filter (2 tests)
   - Verifies `direct_only=False` keeps both direct and connecting flights
   - Verifies `direct_only=True` filters out flights with stops

### 4. **TestPriceSorting** — Price Ordering (1 test)
   - Confirms results are sorted by price (ascending)

### 5. **TestMaxResults** — Result Limiting (1 test)
   - Verifies `max_results` parameter limits returned options

### 6. **TestScrapePriceOneWay** — One-Way Pricing (1 test)
   - Tests basic one-way price scraping
   - Verifies correct field mapping (origin, destination, departure_time, etc.)

### 7. **TestScrapePriceRoundTrip** — Round-Trip Pricing (1 test)
   - Verifies price calculation as sum of cheapest outbound + cheapest return
   - Confirms both departure_time and return_time are captured

### 8. **TestScrapePriceReturnMissing** — Missing Return (1 test)
   - Confirms `scrape_price()` returns None if return flights unavailable

### 9. **TestScrapePriceDepartureMissing** — Missing Departure (1 test)
   - Confirms `scrape_price()` returns None if outbound flights unavailable

### 10. **TestSearchOptionsOneWay** — One-Way Options (1 test)
   - Verifies `search_options()` creates correct `FlightOption` objects
   - Tests one-way (no return) scenario

### 11. **TestSearchOptionsRoundTrip** — Round-Trip Combinations (1 test)
   - Tests all combinations of outbound × return flights
   - Verifies sorting by total price

### 12. **TestMaxPerLeg** — Per-Leg Limiting (1 test)
   - Verifies `max_per_leg` parameter limits options per leg
   - Confirms cartesian product is not exceeded

### 13. **TestSearchOptionsMaxResults** — Final Result Limiting (1 test)
   - Confirms `search_options()` respects global `max_results` limit

### 14. **TestFlightsNotFound** — Exception Handling (1 test)
   - Verifies `FlightsNotFound` exception returns empty list instead of propagating

### 15. **TestSubmitConsentForm** — EU Consent Form (6 tests)
   - Tests form detection with various button texts: "Accetta", "Accept", "Agree"
   - Tests error handling: no forms, missing action attribute
   - Tests relative URL conversion to absolute URLs

### 16. **TestFetchHtml** — HTML Fetching (2 tests)
   - Tests normal response flow (no consent page)
   - Tests consent page redirect detection and handling

### 17. **TestBuildOneWayQuery** — Query Construction (2 tests)
   - Verifies `FlightQuery` creation with correct parameters
   - Tests hour filter parameters (earliest_hour, latest_hour)

## Mock Strategy

All tests use mocking to eliminate external dependencies:

- **HTTP Client** (`primp.Client`): Mocked to avoid network requests
- **HTML Parsing** (`LexborHTMLParser`): Mocked for consent form tests
- **Google Flights API** (`fast_flights.parse`): Mocked with synthetic flight data
- **Flight Objects**: Created as `Mock()` instances with necessary attributes

## Test Execution

Running the full suite:

```bash
$ cd flight-bot
$ python -m pytest tests/ -v
```

Expected output example (abbreviated):
```
tests/test_google_flights.py::TestFormatTime::test_format_time_single_digit_hours_and_minutes PASSED
tests/test_google_flights.py::TestFormatTime::test_format_time_double_digit_hours_and_minutes PASSED
tests/test_google_flights.py::TestFormatTime::test_format_time_none_input PASSED
...
======================== X passed in Y.XXs ========================
```

## Key Testing Principles

1. **No Network Calls**: All HTTP requests are mocked
2. **No External Services**: Database, Telegram, Google Flights API all mocked
3. **No File I/O**: Tests run entirely in memory
4. **Deterministic**: No randomness or timing dependencies
5. **Fast**: Complete suite should run in < 1 second
6. **Isolated**: Each test is independent; can run in any order

## Known Limitations and Non-Testable Behaviors

The following aspects of the scraper **cannot be tested without modifying production code**:

1. **Module-Level Client Initialization** — `_client` is created at import time without parameters, making it difficult to inject a mock. Tests can only mock methods on an already-instantiated object.

2. **Cookie Store Behavior** — The `cookie_store=True` parameter in `primp.Client` persists cookies across requests. This state management cannot be fully tested in isolation without production code changes.

3. **Real HTML Parsing** — `LexborHTMLParser` parsing logic in `_submit_consent_form()` relies on actual HTML structure. Tests mock the parser but don't verify real HTML parsing behavior.

4. **`create_filter()` Return Value** — The `create_filter()` function from fast-flights is a black box. Tests mock it but cannot verify the actual filter structure created.

5. **URL Parameter Construction** — `query.params()` method is mocked. Real parameter encoding and query string generation are not tested.

6. **Primp Client Options** — Client options like `impersonate`, `impersonate_os`, `referer` are set at module import and cannot be modified per-test.

## Dependencies Added

- **pytest** (≥7.0.0): Test framework
- **pytest-mock** (≥3.10.0): Mocking utilities

Both are added to `requirements.txt` and can be installed with the main dependencies.

## Future Enhancements

To achieve deeper test coverage without modifying production code, consider:

1. Adding dependency injection for the HTTP client
2. Extracting module-level initialization into a factory function
3. Creating separate test fixtures for different HTML structures
4. Adding integration tests that use a test double of Google Flights API

## Notes

- Tests follow the behavior specification outlined in `flight-bot/scrapers/google_flights.py` header comments
- The round-trip strategy (separate one-way queries) is treated as immutable baseline behavior
- Tests are organized by function/feature for clarity and maintainability
