COMPOSE_PROJECT=BAW

COMPOSE=docker-compose -f ./docker/docker-compose.yml -p ${COMPOSE_PROJECT}

COPY_CONFIGS=cp ./examples/example.env ./examples/.env

up-redis:
	${COMPOSE} up -d redis

init-config:
	${COPY_CONFIGS}

