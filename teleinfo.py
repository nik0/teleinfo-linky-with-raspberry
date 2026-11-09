#!/usr/bin/python3
# -*- coding: utf-8 -*-
# __author__ = "Charles Peltier"
# __credit__ = "Sébastien Reuiller, gizmo15"
# __licence__ = "Apache License 2.0"
"""Send teleinfo to influxdb."""

import os
import sys
import logging
import time
from datetime import datetime
import json

import serial
import requests
from influxdb import InfluxDBClient
from lib_enedis import energy_meter
import paho.mqtt.client as mqtt

# Ensure unbuffered stdout for Docker / journalctl logs
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# Config
LOGGING_MODE = "INFO"      # DEBUG, WARNING, INFO
TIC_MODE = "STANDARD"      # STANDARD, HISTORIQUE
TIC_PORT = '/dev/ttyAMA0'
PROBE_NAME = 'Raspberry'
DUT = 'Linky'
LOGGER_FILE = "/logs/releve.log"
INFLUXDB_HOST = "localhost"
DB = "teleinfo"

# dictionaries and keys
LABELS_HISTORIQUE_FILE = "/app/liste_champs_mode_historique.txt"
LABELS_STANDARD_FILE = "/app/liste_champs_mode_standard.txt"
DICT_OEM_FILE = "/app/dictionnaire_fabriquants_linky.txt"
DICT_MODEL_FILE = "/app/dictionnaire_type_linky.txt"

## type of key char, not integer
CHAR_MEASURE_KEYS = [
    'OPTARIF', 'HHPHC', 'PTEC', 'MOTDETAT', 'DATE', 'NGTF', 'LTARF', 'MSG1',
    'NJOURF', 'NJOURF+1', 'PJOURF', 'PJOURF+1', 'EASD02', 'STGE', 'RELAIS'
]

# Creation of directory for log
log_dir = os.path.dirname(LOGGER_FILE)
if log_dir and not os.path.exists(log_dir):
    try:
        os.makedirs(log_dir)
    except Exception:
        pass

# creation of logger
numeric_level = getattr(logging, LOGGING_MODE.upper(), logging.INFO)
handlers = [logging.StreamHandler(sys.stdout)]
try:
    if os.path.exists(log_dir):
        handlers.append(logging.FileHandler(LOGGER_FILE))
except Exception:
    pass

logging.basicConfig(
    level=numeric_level,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=handlers
)
logging.info("Teleinfo starting (TIC_MODE=%s)...", TIC_MODE)

# connexion to InfluxDB database
CLIENT = InfluxDBClient(INFLUXDB_HOST, 8086)
CONNECTED = False
while not CONNECTED:
    try:
        logging.info("Check if database %s exists", DB)
        if {'name': DB} not in CLIENT.get_list_database():
            logging.info("Database %s creation..", DB)
            CLIENT.create_database(DB)
            logging.info("Database %s created!", DB)
        CLIENT.switch_database(DB)
        logging.info("Connected to InfluxDB %s!", DB)
        CONNECTED = True
    except (requests.exceptions.ConnectionError, Exception) as e:
        logging.info('InfluxDB is not reachable (%s). Waiting 5 seconds to retry.', e)
        time.sleep(5)


# add a measure point in frame
def add_measures(measures, mqttc=None):
    """Add measures to array and publish to MQTT if mqttc is provided and connected."""
    points = []
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    for measure, value in measures.items():
        point = {
            "measurement": measure,
            "tags": {
                "device": 'raspberry'
            },
            "time": timestamp,
            "fields": {
                "value": value
            }
        }
        points.append(point)

    try:
        CLIENT.write_points(points)
        logging.debug("add measures to InfluxDB (%d points)", len(points))
    except Exception as e:
        logging.error("Erreur écriture InfluxDB: %s", e)

    if mqttc is not None and mqtt_connected:
        try:
            mqttc.publish("teleinfo/frame", json.dumps(measures))
            logging.debug("MQTT published: %s", measures)
        except Exception as e:
            logging.error("MQTT publish error: %s", e)


# Check checksum of the line
def verif_checksum(clean_line, sep, mode):
    """
    Vérifie le checksum d'un groupe Linky nettoyé de CR, LF, STX, ETX.
    - clean_line[-1] : caractère checksum
    - clean_line[-2] : séparateur obligatoire précédant le checksum
    - STANDARD (CPT_54E) : payload = clean_line[:-1] (séparateur HT inclus)
    - HISTORIQUE (CPT_02E) : payload = clean_line[:-2] (séparateur SP exclu)
    """
    if len(clean_line) < 3 or clean_line[-2] != sep:
        return False, None, None

    checksum_received = clean_line[-1]

    if mode == "STANDARD":
        payload = clean_line[:-1]
    else:
        payload = clean_line[:-2]

    computed_checksum = chr((sum(ord(c) for c in payload) & 0x3F) + 0x20)
    return (computed_checksum == checksum_received), checksum_received, computed_checksum


# Label from ENEDIS specifications normally send by the Linky
def know_labels_from_file(file):
    """Get valid labels from file."""
    labels = []
    with open(file) as keys_file:
        for line in keys_file:
            information = line.split("\t")
            if len(information) > 1:
                labels.append(information[1].strip())
    return labels


# ID and name of manufacturer of Linky authorized by ENEDIS
def oem_from_file(file):
    """Get OEM name from file."""
    information = {}
    with open(file) as dico_file:
        for line in dico_file:
            line = line.replace("\n", "").strip()
            if not line:
                continue
            decoupage = line.split("\t")
            if len(decoupage) >= 2:
                try:
                    code_fabricant = int(decoupage[0])
                    nom_fabricant = decoupage[1]
                    information[code_fabricant] = nom_fabricant
                except ValueError:
                    continue
    return information


# ID and name of known Linky models
def models_from_file(file):
    """Get Linky model name from file."""
    information = {}
    with open(file) as dico_file:
        for line in dico_file:
            line = line.replace("\n", "").strip()
            if not line:
                continue
            decoupage = line.split("\t")
            if len(decoupage) >= 2:
                try:
                    code_modele = int(decoupage[0])
                    nom_modele = decoupage[1]
                    information[code_modele] = nom_modele
                except ValueError:
                    continue
    return information


# Configuration of serial port speed and dictionary of label
def mode_config(mode):
    global TIC_SPEED
    global LABELS_LINKY
    global SPLIT_CHAR
    global ADDRESS_LINKY

    if mode == "STANDARD":
        TIC_SPEED = 9600
        LABELS_LINKY = know_labels_from_file(LABELS_STANDARD_FILE)
        SPLIT_CHAR = "\t"
        ADDRESS_LINKY = 'ADSC'
    else:
        TIC_SPEED = 1200
        LABELS_LINKY = know_labels_from_file(LABELS_HISTORIQUE_FILE)
        SPLIT_CHAR = " "
        ADDRESS_LINKY = 'ADCO'
    logging.debug("Know labels count: %d", len(LABELS_LINKY))


# find OEM from serial number of linky
def oem_linky(serial):
    try:
        manufacturer_id = int(str(serial)[2:4])
        return manufacturer_id
    except Exception:
        return 0


# Add calculate CosPhi for standard mode
def calculate_cosphi(trame):
    try:
        if trame.get("IRMS1") and trame.get("URMS1") and trame.get("SINSTS"):
            cosphi = trame["SINSTS"] / (trame["IRMS1"] * trame["URMS1"])
            trame["COSPHI"] = round(cosphi, 3)
            logging.debug("COSPHI: %s", trame["COSPHI"])
    except Exception as e:
        logging.debug("calculate_cosphi error: %s", e)


# MQTT state and callbacks
mqtt_connected = False

def on_connect(client, userdata, flags, rc, properties=None):
    global mqtt_connected
    rc_code = getattr(rc, "value", rc)
    if rc_code == 0:
        mqtt_connected = True
        logging.info("MQTT connecté avec succès au broker")
    else:
        logging.error("Échec connexion MQTT, code retour: %s", rc)

def on_disconnect(client, userdata, disconnect_flags, rc=None, properties=None):
    global mqtt_connected
    mqtt_connected = False
    logging.warning("MQTT déconnecté (code %s)", rc)


def main():
    """Main function to read teleinfo."""
    global mqttc

    mode_config(TIC_MODE)

    # Initialisation du client MQTT (compatible paho-mqtt v2)
    try:
        mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="teleinfo")
    except AttributeError:
        mqttc = mqtt.Client(client_id="teleinfo")

    mqtt_user = os.environ.get("MQTT_USER", "mqtt")
    mqtt_password = os.environ.get("MQTT_PASSWORD")
    if mqtt_user and mqtt_password:
        mqttc.username_pw_set(mqtt_user, mqtt_password)
    elif mqtt_user:
        logging.warning("Variable MQTT_PASSWORD non définie dans l'environnement")
    mqttc.reconnect_delay_set(min_delay=1, max_delay=60)
    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect

    mqtt_host = os.environ.get("MQTT_HOST", "localhost")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    try:
        mqttc.connect_async(mqtt_host, mqtt_port, 60)
        mqttc.loop_start()
    except Exception as e:
        logging.error("Impossible d'initialiser MQTT au démarrage: %s", e)

    with serial.Serial(port=TIC_PORT, baudrate=TIC_SPEED, parity=serial.PARITY_NONE,
                       stopbits=serial.STOPBITS_ONE,
                       bytesize=serial.SEVENBITS, timeout=1) as tic:
        logging.info("Teleinfo is reading on %s (baudrate=%s)", TIC_PORT, TIC_SPEED)
        logging.info("Mode %s", TIC_MODE)
        logging.info("Serial configuration: %s", tic)

        dict_linky_oem = oem_from_file(DICT_OEM_FILE)
        logging.debug("Known manufacturer count: %d", len(dict_linky_oem))

        dict_linky_models = models_from_file(DICT_MODEL_FILE)
        logging.debug("Known models count: %d", len(dict_linky_models))

        trame = dict()
        nb_checksum_ok = 0
        nb_checksum_err = 0

        # Read information from serial
        line = tic.readline()
        logging.info("Start of frames analysis - waiting for frame start")

        # loop and search beginning of frame STX (0x02)
        while b'\x02' not in line:
            line = tic.readline()

        logging.info("Start of frame detected")
        line = tic.readline()
        while True:
            # 1. Détection de fin de trame sur les octets bruts AVANT nettoyage
            is_end_of_frame = (b'\x03' in line)

            # 2. Décodage strict et rejet des octets corrompus
            try:
                line_str = line.decode("ascii", errors="strict")
            except UnicodeDecodeError:
                logging.warning("Octet série non-ASCII détecté, ligne rejetée : %r", line)
                line = tic.readline()
                continue

            clean_line = line_str.strip("\r\n\x02\x03")

            # 3. Traitement de la ligne de données si non vide
            if clean_line:
                is_valid, ck_recv, ck_calc = verif_checksum(clean_line, SPLIT_CHAR, TIC_MODE)
                if not is_valid:
                    nb_checksum_err += 1
                    logging.warning("Checksum invalide sur %r (reçu: %r, calculé: %r)",
                                    clean_line, ck_recv, ck_calc)
                else:
                    nb_checksum_ok += 1
                    # Découpage des données (exclusion du checksum et de son séparateur)
                    content = clean_line[:-2]
                    parts = content.split(SPLIT_CHAR)
                    label = parts[0]

                    if label in LABELS_LINKY:
                        if label == "DATE":
                            val = parts[1] if len(parts) > 1 else ""
                        else:
                            raw_val = parts[-1]
                            if len(parts) >= 3:
                                trame[label + "_HORODATE"] = parts[1]

                            if label in CHAR_MEASURE_KEYS:
                                val = raw_val
                            else:
                                try:
                                    val = int(raw_val)
                                except ValueError:
                                    val = 0
                        trame[label] = val
                        logging.debug("-> %s = %s", label, val)
                    else:
                        trame['verification_error'] = "1"
                        logging.debug("Étiquette inconnue ignorée : %s", label)

            # 4. Traitement de clôture de trame
            if is_end_of_frame and trame:
                total_fields = nb_checksum_ok + nb_checksum_err
                pct_ok = (nb_checksum_ok / total_fields * 100) if total_fields else 0
                logging.info("Fin de trame: %d/%d champs valides (%.1f%%)",
                             nb_checksum_ok, total_fields, pct_ok)

                # Décodage OEM sécurisé
                addr = str(trame.get(ADDRESS_LINKY, ""))
                if len(addr) >= 4 and addr[2:4].isdigit():
                    trame['OEM'] = oem_linky(addr)

                # Décodage registre STGE corrigé
                if 'STGE' in trame:
                    try:
                        dict_register = energy_meter.register_analyze(trame['STGE'])
                        for key, value in dict_register.items():
                            trame[key] = value
                    except Exception as e:
                        logging.debug("Erreur analyse STGE: %s", e)

                # Calcul cosphi facultatif
                calculate_cosphi(trame)

                # Insertion InfluxDB et publication MQTT
                add_measures(trame, mqttc)

                # Réinitialisation pour la trame suivante
                trame = dict()
                nb_checksum_ok = 0
                nb_checksum_err = 0

            # Lecture ligne suivante
            line = tic.readline()


if __name__ == '__main__':
    if CONNECTED:
        main()
