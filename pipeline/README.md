# pipeline

## Tests

    .venv/bin/python -m pytest pipeline/tests -q

`test_r2.py` covers the upload plan, size guards, upload order and prune selection of `r2.py` with a fake S3 client
(no network).

## Parity test

`pipeline/tests/test_parity.py` compares `model.py` with the reference model (`habitat/pilz.js` of shroomzn.app, run in Node). Check out [OutFoxD/shroomzn](https://github.com/OutFoxD/shroomzn) and run

    SHROOMZN_DIR=/path/to/shroomzn .venv/bin/python -m pytest pipeline/tests -q

Without `SHROOMZN_DIR` (or without `node`) the parity tests are skipped.

## Known differences from shroomzn.app

- The 3x3 neighbour mean is computed per region, so cells on the DE band seams and on the AT/CH/DE borders differ slightly from his merged tiles.
- The model samples weather with `dlat = 9.35 / 1039` like `pilz.js`, while the tap-card cells use the exact 0.009 grid step. In the far south about 10 % of the cells therefore use a weather row one off from their tap card (kept for parity).
