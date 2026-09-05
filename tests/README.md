# Tests

Run the offline integration test suite from the repository root:

```powershell
$env:PYTHONPATH="."
pytest -q tests/test_mvp.py
```

The tests use the local fallback runtime and never require a Solari API key.
