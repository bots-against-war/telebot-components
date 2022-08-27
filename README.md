# telebot-components

Framework / toolkit for building bots with [telebot](https://github.com/bots-against-war/telebot).

<!-- ## Development -->


## Development

### Setup

The project requires Poerty 1.2.x or higher (see [installation instruction](https://python-poetry.org/docs/master#installing-with-the-official-installer))).
For example, to install `1.2.0b2` on Unix, run

```bash
curl -sSL https://install.python-poetry.org | python3 - --version 1.2.0b2
```

Then, to install the library with all dependencies, run from project root

```bash
poetry install
```

You might need to manually install dynamic versioning plugin:

```bash
poetry plugin add poetry-dynamic-versioning-plugin
```


### Testing

```bash
poetry run pytest tests -vv
```

By default all tests are run with in-memory Redis emulation. But if you have Redis installed you can run them
locally on real Redis by specifying something like

```bash
export REDIS_URL="redis://localhost:1234"
```

Tests must be able to find an empty Redis DB to use; they also clean up after themselves.
