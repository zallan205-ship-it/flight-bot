# Quick Reference: Running the Test Suite

## Prerequisites

```bash
# Install dependencies
cd flight-bot
pip install -r requirements.txt
```

## Basic Commands

```bash
# Run all tests with verbose output
pytest tests/ -v

# Run all tests quietly (just pass/fail)
pytest tests/

# Run a specific test class
pytest tests/test_google_flights.py::TestFormatTime -v

# Run a specific test method
pytest tests/test_google_flights.py::TestFormatTime::test_format_time_single_digit_hours_and_minutes -v

# Run tests matching a pattern
pytest tests/ -k "format_time" -v

# Run tests and stop on first failure
pytest tests/ -x

# Run tests with detailed failure output
pytest tests/ -vv --tb=long

# Run tests with print statements visible
pytest tests/ -v -s

# Run tests and generate coverage report
pip install pytest-cov
pytest tests/ --cov=scrapers.google_flights --cov-report=html
# Report will be in htmlcov/index.html
```

## Test Organization

All tests are organized in `flight-bot/tests/test_google_flights.py` by feature:

| Class | Purpose | Tests |
|-------|---------|-------|
| `TestFormatTime` | Time formatting | 5 tests |
| `TestSearchLegStops` | Stop calculation | 3 tests |
| `TestDirectOnly` | Flight filtering | 2 tests |
| `TestPriceSorting` | Price ordering | 1 test |
| `TestMaxResults` | Result limiting | 1 test |
| `TestScrapePriceOneWay` | One-way pricing | 1 test |
| `TestScrapePriceRoundTrip` | Round-trip pricing | 1 test |
| `TestScrapePriceReturnMissing` | Missing return | 1 test |
| `TestScrapePriceDepartureMissing` | Missing departure | 1 test |
| `TestSearchOptionsOneWay` | One-way options | 1 test |
| `TestSearchOptionsRoundTrip` | Round-trip combinations | 1 test |
| `TestMaxPerLeg` | Per-leg limiting | 1 test |
| `TestSearchOptionsMaxResults` | Final limiting | 1 test |
| `TestFlightsNotFound` | Exception handling | 1 test |
| `TestSubmitConsentForm` | Consent form handling | 6 tests |
| `TestFetchHtml` | HTML fetching | 2 tests |
| `TestBuildOneWayQuery` | Query construction | 2 tests |

## Run Specific Test Classes

```bash
# Format time tests
pytest tests/test_google_flights.py::TestFormatTime -v

# Stop calculation tests
pytest tests/test_google_flights.py::TestSearchLegStops -v

# Direct only filter tests
pytest tests/test_google_flights.py::TestDirectOnly -v

# Price sorting tests
pytest tests/test_google_flights.py::TestPriceSorting -v

# One-way scraping tests
pytest tests/test_google_flights.py::TestScrapePriceOneWay -v

# Round-trip scraping tests
pytest tests/test_google_flights.py::TestScrapePriceRoundTrip -v

# Consent form tests
pytest tests/test_google_flights.py::TestSubmitConsentForm -v

# All scrape_price tests
pytest tests/ -k "scrape_price" -v

# All search_options tests
pytest tests/ -k "search_options" -v
```

## Advanced Options

```bash
# Parallel execution (install pytest-xdist first)
pip install pytest-xdist
pytest tests/ -n auto

# Watch mode (install pytest-watch first)
pip install pytest-watch
ptw tests/

# Generate detailed HTML report
pip install pytest-html
pytest tests/ --html=report.html --self-contained-html

# Run with different verbosity levels
pytest tests/ -q          # quiet
pytest tests/ -v          # verbose
pytest tests/ -vv         # very verbose

# Show local variables in tracebacks
pytest tests/ -l

# Drop into debugger on failure
pytest tests/ --pdb

# Drop into debugger on first failure
pytest tests/ -x --pdb
```

## Expected Output

When all tests pass:

```
======================== 36 passed in 0.XX seconds ========================
```

When tests fail, you'll see:

```
FAILED tests/test_google_flights.py::TestFormatTime::test_format_time_single_digit_hours_and_minutes
```

With details about what was expected vs actual.

## File Structure

```
flight-bot/
├── tests/
│   ├── __init__.py                 # Package init
│   ├── conftest.py                 # Pytest fixtures & config
│   ├── test_google_flights.py       # Main test suite (36 tests)
│   └── README.md                    # Full test documentation
├── pytest.ini                       # Pytest configuration
├── TESTING_SUMMARY.md              # This summary
├── requirements.txt                # Dependencies (includes pytest)
└── scrapers/
    ├── __init__.py                 # ScrapedPrice, FlightOption
    └── google_flights.py           # Module being tested
```

## Key Points

✅ **All tests are mocked** — No network calls, offline execution  
✅ **36 test methods** — Comprehensive coverage of all functions  
✅ **Fast execution** — Complete suite runs in < 1 second  
✅ **No side effects** — Each test is independent  
✅ **Deterministic** — Same results every run  

## Troubleshooting

### Tests won't run: `ModuleNotFoundError: No module named 'pytest'`

```bash
pip install pytest pytest-mock
```

### Tests fail with import errors

```bash
# Make sure you're in the right directory
cd flight-bot
pytest tests/
```

### Mocking isn't working

Mocks use full module paths like `scrapers.google_flights._client`. If importing differs, adjust the patch path in the test.

### Coverage is incomplete

Coverage only includes lines executed during tests. External dependencies that are mocked won't show coverage.

```bash
pytest tests/ --cov=scrapers.google_flights --cov-report=term-missing
```

---

For complete documentation, see `flight-bot/tests/README.md`
