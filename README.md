# telebot-components

Framework / toolkit for building bots with [telebot](https://github.com/bots-against-war/telebot).

<!-- ## Development -->


## Development

Install with Poetry (requires 1.2.x and higher with plugin support):

```bash
poetry install
```

### Testing

```bash
pytest tests -vv
```

By default all tests are run with in-memory Redis emulation. But if you have Redis installed you can run them
locally on real Redis by specifying something like

```bash
export REDIS_URL="redis://localhost:1234"
```

Tests must be able to find an empty Redis DB to use; they also clean up after themselves.
