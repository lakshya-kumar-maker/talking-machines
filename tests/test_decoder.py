"""The decoder names every tag correctly, without seeing the answer key."""

import pytest

import decoder


@pytest.mark.parametrize("seed", [7, 42, 911])
def test_decoder_scores_53_of_53(make_run, seed):
    run = make_run(seed)
    result, _ = decoder.decode(str(run.csv))
    assert decoder.score(result, str(run.answers)) == (53, 53)
