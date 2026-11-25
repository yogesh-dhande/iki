# ikisci: Iki's Python SDK

Work with [Iki](https://ikisci.org) from notebooks and scripts: search and read records, change
them on the record, run actions and wait for them, and turn what you find into pandas DataFrames.

```sh
pip install 'ikisci[pandas]'    # or just ikisci, without DataFrames
```

```python
import iki

iki.login("https://iki.example.org")  # once: you enter a code in your browser

samples = iki.entities.search("iki/samples.Sample", project="qc-lab")
table = samples.to_pandas()  # one row per record, a column per field
```

Everything you do is recorded in Iki as done by you, through the API, with this SDK.

Signing in, every part of the client, the errors it raises, and Iki's MCP server for AI agents
are in [the Python SDK and MCP server reference](https://ikisci.org/reference/python-sdk/).
