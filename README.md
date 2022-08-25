# telebot-components

Framework / toolkit for building bots with [telebot](https://github.com/bots-against-war/telebot).

<!-- ## Development -->

## Development
### Setup
1. Clone repository
   ```bash
   git clone git@github.com:bots-against-war/telebot-components.git baw
   cd ./baw
   ```
2. Install dependencies with Poetry (requires 1.2.x and higher with plugin support - [install instruction](https://python-poetry.org/docs/master#installing-with-the-official-installer)):
   ```bash
   poetry install
   ```
   - For create the virtualenv inside the project’s root directory, use command
   ```bash
   poetry config virtualenvs.in-project false --local
   ```
3. Run pre-commit to set up the git hook scripts
   ```bash
   pre-commit install
   ```

### Testing
Use command below for run tests
```bash
pytest tests -vv
```

By default all tests are run with in-memory Redis emulation. But if you want you can run them
locally on real Redis (**read manual below**) 

> **Note**: Tests must be able to find an empty Redis DB to use; they also clean up after themselves.

### Start example bot
For first start you need to do 3 things:
1. Use command below to generate environment variables file:
    ```bash
    make init-config
    ```
   > **Note**: After generate `.env` file you need to add your [bot's token](https://core.telegram.org/bots#6-botfather).  
   > Also for bot with `trello integration` you need to add `trello` token and api key. You can get it [here](https://trello.com/app-key).
2. Start `redis` inside `docker` container.  
   Run redis on local machine, by default on 6379 port. If you need change it in `./docker/docker-compose.yml`
    ```bash
    make up-redis
    ```
3. Run any bot from `./examples`
    ```bash
    python3 ./examples/feedback_bot.py  // or run with IDE from bot file
    ```
