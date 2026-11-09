#!/bin/bash
# =====================================================================
# Retention et agregats InfluxDB 1.x pour la base teleinfo
#  - autogen     : trames brutes (~1 toutes les 2 s), conservees 400 jours
#                  (13 mois : permet de comparer avec la meme periode l'an dernier)
#  - downsampled : agregats horaires (1h) et journaliers (1d, jours en heure de Paris),
#                  conserves indefiniment ; utilises par grafana/conso-elec-base-standard.json
#
# A lancer sur la machine qui heberge InfluxDB :  ./setup_downsampling.sh [hote]
# ATTENTION : reduire la retention de autogen supprime definitivement les trames plus
# anciennes. Faire d'abord une sauvegarde (influxd backup -portable -db teleinfo <dossier>)
# et recalculer les agregats de l'historique AVANT d'appliquer la retention.
# =====================================================================
set -euo pipefail

HOST="${1:-localhost}"
DB="teleinfo"
RAW_RETENTION="400d"
TZ="tz('Europe/Paris')"

run() { influx -host "$HOST" -database "$DB" -execute "$1"; }

run "CREATE RETENTION POLICY downsampled ON $DB DURATION INF REPLICATION 1" 2>/dev/null || true

cq() {  # $1 mesure  $2 intervalle (1h|1d)  $3 agregations, ex. "mean(value) AS mean, max(value) AS max"
    local name="cq_$(echo "$1" | tr 'A-Z' 'a-z')_$2" resample tz=""
    if [ "$2" = "1h" ]; then resample="RESAMPLE EVERY 10m FOR 2h"; else resample="RESAMPLE EVERY 1h FOR 2d"; tz=" $TZ"; fi
    run "DROP CONTINUOUS QUERY $name ON $DB" 2>/dev/null || true
    run "CREATE CONTINUOUS QUERY $name ON $DB $resample BEGIN SELECT $3 INTO $DB.downsampled.\"$1_$2\" FROM $DB.autogen.\"$1\" GROUP BY time($2), device$tz END"
    echo "CQ $name"
}

# Puissance apparente soutiree (VA)
cq SINSTS 1h "mean(value) AS mean, max(value) AS max, min(value) AS min"
cq SINSTS 1d "mean(value) AS mean, max(value) AS max, min(value) AS min"
# Index d'energie (Wh) : la consommation d'une periode = spread("last") sur les agregats
cq EAST 1h "max(value) AS max, last(value) AS last, spread(value) AS spread"
cq EAST 1d "max(value) AS max, last(value) AS last, spread(value) AS spread"
cq EASF01 1h "max(value) AS max, last(value) AS last, spread(value) AS spread"
cq EASF01 1d "max(value) AS max, last(value) AS last, spread(value) AS spread"
# Tension (V) et courant (A)
cq URMS1 1h "mean(value) AS mean, min(value) AS min, max(value) AS max"
cq URMS1 1d "mean(value) AS mean, min(value) AS min, max(value) AS max"
cq IRMS1 1h "mean(value) AS mean, max(value) AS max"
# Puissance maximale du jour calculee par le compteur (VA)
cq SMAXSN 1d "max(value) AS max"

run "ALTER RETENTION POLICY autogen ON $DB DURATION $RAW_RETENTION SHARD DURATION 168h DEFAULT"
run "SHOW RETENTION POLICIES"
