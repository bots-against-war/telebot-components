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

By default all tests are run with in-memory Redis emulation. But if you have redis installed you can run them
locally on real Redis by specifying

```bash
export REDIS_URL="
```
