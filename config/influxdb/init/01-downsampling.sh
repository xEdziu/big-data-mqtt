#!/bin/bash
# Uruchamiany przez obraz influxdb jednorazowo, po początkowym setupie (CLI influx jest już skonfigurowane).
set -euo pipefail

influx bucket create --org "${DOCKER_INFLUXDB_INIT_ORG}" --name telemetry_1m --retention 0
influx task create --org "${DOCKER_INFLUXDB_INIT_ORG}" --file /etc/influxdb-tasks/downsample_1m.flux
