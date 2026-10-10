"""Golden-repository evaluation of the repository analyzer.

A golden case is a public repository pinned to a commit plus an expected
Product Profile. The benchmark drafts a profile for each case several times
and says, per field, how close the drafts are, with the cost next to it.

Three parts are kept apart, and a fourth joins them:

- ``cases.py`` loads and validates the cases;
- ``runner.py`` runs the analyzer on a case, through the same operation the
  worker runs, and keeps what it produced;
- ``scoring.py`` is pure functions over two profiles and the judge's verdicts:
  one named function per metric, no model, no file;
- ``judge.py`` puts to the ``judge`` role the questions scoring cannot answer
  in code. Its verdicts are stored with the raw output, so scores are
  recomputed from them without a model call.

``claims.py`` says what in a profile counts as a claim, ``results.py`` is the
folder a benchmark run leaves behind, ``report.py`` turns the scores of several
runs into what a person reads, and ``benchmark.py`` is the benchmark as one
operation for the command line.
"""
