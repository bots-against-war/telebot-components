# telebot-components

Framework / toolkit for building bots with [telebot](https://github.com/bots-against-war/telebot).

## Development

### Setup

1. Clone repository
   ```bash
   git clone git@github.com:bots-against-war/telebot-components.git baw
   cd ./baw
   ```

2. The project requires Poerty 1.5.1 (see [installation instruction](https://python-poetry.org/docs/master#installing-with-the-official-installer)).
   For example, to install `1.5.1` on Unix, run
   ```bash
   curl -sSL https://install.python-poetry.org | python3 - --version 1.5.1
   ```

3. Then, to install the library with all dependencies, run from project root
   ```bash
   poetry install
   ```
   - You might need to manually install dynamic versioning plugin (without it local build will
     always have version `0.0.0`):
     ```bash
     poetry self add poetry-dynamic-versioning-plugin
     ```
   - To create virtualenv inside the project’s root directory, use command
     ```bash
     poetry config virtualenvs.in-project false --local
     ```
4. Run `pre-commit` to set up git hook scripts
   ```bash
   pre-commit install
   ```


### Testing
Use command below for run tests
```bash
poetry run pytest tests -vv
```

By default, all tests are run with in-memory Redis emulation. But if you want you can run them
locally on real Redis by specifying `REDIS_URL` environment variable before running `pytest`.

> **Note**: Tests must be able to find an empty Redis DB to use; they also clean up after themselves.

### Start example bot
For first start you need to do 3 things:
1. Use command below to generate environment variables file:
    ```bash
    cp ./examples/example.env ./examples/.env
    ```
   > **Note**: After `.env` file is generated you will need to add your [bot's token](https://core.telegram.org/bots#6-botfather) to it.  
   > Also for bot with `trello integration` you need to add `trello` token and api key. You can get it [here](https://trello.com/app-key).
2. If you want start redis on local machine, run
    ```bash
    docker run --name baw-redis -d -p 6379:6379 redis redis-server --save 60 1 --loglevel warning
    ```
3. Run any bot from `./examples`
    ```bash
    python3 ./examples/feedback_bot.py  // or run with IDE from bot file
    ```
