"""Synthetic contract/rubric fixtures, not investment or model-quality benchmarks.

The deterministic model double tests book-regime alignment and causal feature
plumbing. Real fitted-model filtering is covered separately in test_regimes.py.
"""

import csv
import io
from datetime import date, timedelta

import numpy as np
import pytest

from honest_alpha_lab import book_regimes
from honest_alpha_lab.contracts import ContractError


@pytest.fixture
def market():
    days = []
    day = date(2024, 1, 1)
    while len(days) < 40:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return [{"day": day.isoformat(), "close": 100 + i * i / 10,
             "available_at": f"{day.isoformat()}T21:00:00+00:00"}
            for i, day in enumerate(days)]


@pytest.fixture
def models(monkeypatch):
    instances = []

    class DeterministicModel:
        def __init__(self, config):
            self.config = config
            self.converged = True
            instances.append(self)

        def fit(self, dates, features):
            self.training_dates = tuple(dates)
            self.training_features = np.array(features, copy=True)
            return self

        def predict_proba(self, dates, features):
            self.prediction_dates = tuple(dates)
            self.prediction_features = np.array(features, copy=True)
            # Deliberately causal and price-sensitive, with distinct methods.
            factor = 1 if self.config.method == "hmm" else 2
            p = .5 + .4 * np.tanh(factor * self.prediction_features[:, 0])
            self.probabilities = np.column_stack((p, 1 - p))
            return self.probabilities.copy()

    monkeypatch.setattr(book_regimes, "FittedRegimeModel", DeterministicModel)
    return instances


def content(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=("day", "close", "available_at"))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def comparisons(*days):
    return {"fixture_book": {"metrics": {"daily": [
        {"day": day, "return": .01 if i % 2 == 0 else -.02}
        for i, day in enumerate(days)
    ]}}}


def analyze(market, books=None, *, training_end=None, states=2):
    return book_regimes.book_regime_analysis(
        content(market), books or comparisons(*(row["day"] for row in market[23:28])),
        training_end or market[21]["day"], window=2, states=states,
    )


def test_preceding_supplied_session_probabilities_across_weekend(market, models):
    # Index 25 is Monday; the preceding supplied row is Friday, not Sunday.
    friday, monday = market[24]["day"], market[25]["day"]
    assert date.fromisoformat(monday) - date.fromisoformat(friday) == timedelta(days=3)
    output = analyze(market, comparisons(monday))
    assert output["status"] == "evaluated"
    assert {method["method"] for method in output["methods"]} == {"hmm", "gaussian_mixture"}
    for method, model in zip(output["methods"], models, strict=True):
        observed, = method["comparisons"]["fixture_book"]["observations"]
        previous = model.prediction_dates.index(date.fromisoformat(friday))
        same_day = model.prediction_dates.index(date.fromisoformat(monday))
        assert observed["day"] == monday
        np.testing.assert_array_equal(observed["probabilities"], model.probabilities[previous])
        assert observed["probabilities"] != model.probabilities[same_day].tolist()
        assert method["comparisons"]["fixture_book"]["missing_dates"] == []


def test_training_is_cut_off_before_inference_and_shared_across_books(market, models):
    books = comparisons(market[25]["day"])
    books["second_book"] = {"metrics": {"daily": [{"day": market[26]["day"], "return": -.03}]}}
    output = analyze(market, books)
    assert len(models) == 2  # one fit per method, not per book
    for method, model in zip(output["methods"], models, strict=True):
        assert model.training_dates == tuple(date.fromisoformat(row["day"]) for row in market[2:22])
        assert model.prediction_dates[0] == date.fromisoformat(market[22]["day"])
        assert method["training_end"] == market[21]["day"]
        assert set(method["comparisons"]) == {"fixture_book", "second_book"}


@pytest.mark.parametrize("training_index", [23, 24])
def test_training_on_or_after_first_book_return_is_rejected(market, models, training_index):
    with pytest.raises(ContractError, match="training observations were unavailable"):
        analyze(market, comparisons(market[23]["day"]), training_end=market[training_index]["day"])
    assert models == []


@pytest.mark.parametrize("release", ["00:00:00+00:00", "01:00:00+00:00", "02:00:00+02:00"])
def test_training_release_at_or_after_utc_cutoff_is_rejected(market, models, release):
    market[21]["available_at"] = f"{market[23]['day']}T{release}"
    with pytest.raises(ContractError, match="training observations were unavailable"):
        analyze(market)
    assert models == []


def test_late_training_constituent_cannot_hide_behind_timely_feature_row(market, models):
    # The price at index 0 is used by the first trailing feature at index 2.
    market[0]["available_at"] = f"{market[23]['day']}T00:00:00+00:00"
    with pytest.raises(ContractError, match="training observations were unavailable"):
        analyze(market)
    assert models == []


def test_training_release_strictly_before_cutoff_is_allowed(market, models):
    preceding = date.fromisoformat(market[23]["day"]) - timedelta(days=1)
    market[21]["available_at"] = f"{preceding.isoformat()}T23:59:59+00:00"
    assert analyze(market)["status"] == "evaluated"
    assert len(models) == 2


@pytest.mark.parametrize("end_index,states", [(20, 2), (21, 5)])
def test_insufficient_training_after_warmup_never_fits(market, models, end_index, states):
    output = analyze(market, training_end=market[end_index]["day"], states=states)
    assert output["status"] == "insufficient"
    assert "training observations" in output["reason"]
    assert output["methods"] == []
    assert models == []


def test_no_out_of_sample_sessions_is_explicitly_insufficient(market, models):
    output = analyze(market[:22], comparisons(market[25]["day"]))
    assert output == {"status": "insufficient", "reason": "no out-of-sample market sessions", "methods": []}
    assert models == []


def test_missing_calendar_date_and_training_only_predecessor_are_reported(market, models):
    absent_sunday = (date.fromisoformat(market[25]["day"]) - timedelta(days=1)).isoformat()
    books = comparisons(market[22]["day"], absent_sunday, market[25]["day"])
    for method in analyze(market, books)["methods"]:
        book = method["comparisons"]["fixture_book"]
        assert book["missing_dates"] == [market[22]["day"], absent_sunday]
        assert [row["day"] for row in book["observations"]] == [market[25]["day"]]


@pytest.mark.parametrize("late_index", [22, 24])
def test_late_preceding_session_or_its_constituent_marks_return_missing(market, models, late_index):
    monday = market[25]["day"]
    market[late_index]["available_at"] = f"{monday}T00:00:00+00:00"
    for method in analyze(market, comparisons(monday))["methods"]:
        book = method["comparisons"]["fixture_book"]
        assert book == {"states": [], "missing_dates": [monday], "observations": []}


def test_future_price_perturbation_does_not_change_earlier_conditioning(market, models):
    books = comparisons(*(row["day"] for row in market[23:31]))
    baseline = analyze(market, books)
    baseline_models = list(models)
    changed = [dict(row) for row in market]
    changed[27]["close"] *= 7
    perturbed = analyze(changed, books)
    for before, after, old_model, new_model in zip(
        baseline["methods"], perturbed["methods"], baseline_models, models[2:], strict=True,
    ):
        np.testing.assert_array_equal(old_model.training_features, new_model.training_features)
        np.testing.assert_array_equal(old_model.prediction_features[:5], new_model.prediction_features[:5])
        old = before["comparisons"]["fixture_book"]["observations"]
        new = after["comparisons"]["fixture_book"]["observations"]
        # A changed close on date 27 cannot condition that day's book return.
        assert [row for row in old if row["day"] <= market[27]["day"]] == [
            row for row in new if row["day"] <= market[27]["day"]]
        assert old[5]["probabilities"] != new[5]["probabilities"]  # sensitivity control


def test_future_append_preserves_existing_book_results(market, models):
    books = comparisons(*(row["day"] for row in market[23:27]))
    prefix = analyze(market[:27], books)
    extended = analyze(market, books)
    assert prefix == extended


@pytest.mark.parametrize("fault", ["duplicate", "unordered", "naive_clock", "nonpositive_close"])
def test_invalid_market_rows_fail_before_model_fit(market, models, fault):
    if fault == "duplicate":
        market.insert(4, dict(market[4]))
    elif fault == "unordered":
        market[3], market[4] = market[4], market[3]
    elif fault == "naive_clock":
        market[4]["available_at"] = f"{market[4]['day']}T21:00:00"
    else:
        market[4]["close"] = 0
    with pytest.raises(ContractError):
        analyze(market)
    assert models == []
