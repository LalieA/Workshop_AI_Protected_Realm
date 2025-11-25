# Copyright (C) 2025 CEA - All Rights Reserved
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of  MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import logging
import signal
import sys
import time
import base64
from os import makedirs
from datetime import datetime
from functools import partial
import json

import paho.mqtt.client as mqtt
from google.protobuf.message import DecodeError
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from syscalls_pb2 import Syscalls, Syscall # Generated at compile time from protoc

MQTT_TOPIC_ANOMALY_SCORE = "eArgos/anomaly_score"
MQTT_TOPIC_INFO_LOADER = "eArgos/info_loader"


class SyscallMessageHandler:

    def __init__(self, influxdb_url, influxdb_token, influxdb_org, influxdb_bucket, save_protobuf, storage_dir):
        # InfluxDB client used to write aggregated syscall counts
        self.influxdb_client = InfluxDBClient(url=influxdb_url, token=influxdb_token, org=influxdb_org)
        self.influxdb_org = influxdb_org
        self.influxdb_bucket = influxdb_bucket

        # Boolean flag indicating whether to store raw Protobuf messages
        self.save_protobuf = save_protobuf

        # Directory used to store raw Protobuf data
        self.storage_dir = storage_dir
        makedirs(storage_dir, exist_ok=True)

        # Maps each system ID to the file handle used for raw Protobuf recording
        self.output_file_by_system = {}

        self.clock_by_system = {}


    # -------------------- System State and Data Aggregation --------------------

    def close_all_output_files(self):
        for file_handle in self.output_file_by_system.values():
            if file_handle is None:
                continue

            try:
                file_handle.close()
            except Exception as e:
                logging.debug(f"Failed to close output file : {e}")

    def refresh_clock_offset(self, system_id, syscall_batch, current_time_ns):
        last_syscall = syscall_batch.syscalls[-1]
        clock_offset_ns = self.clock_by_system.get(system_id)
        drift_ns = abs(last_syscall.timestamp_enter + clock_offset_ns - current_time_ns)

        if drift_ns > 10000000000:
            logging.info("Clock drift exceeds 10 seconds, updating clock offset.")
            clock_offset_ns = current_time_ns - last_syscall.timestamp_enter
            self.clock_by_system[system_id] = clock_offset_ns


    def write_window_counts(self, system_id, syscall_counts, window_end_ns, clock_offset_ns):
        point = Point("syscall_counts").tag("system_id", str(system_id))

        # Attach category counts as fields
        for category, count in syscall_counts.items():
            point.field(category, count)

        # Timestamp the point at the window boundary, adjusted by the clock offset
        point.time(window_end_ns + clock_offset_ns, write_precision=WritePrecision.NS)

        # Write the window’s counts to InfluxDB.
        try:
            write_api = self.influxdb_client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket=self.influxdb_bucket , org=self.influxdb_org, record=point)
        except Exception as e:
            logging.debug(f"Failed to write to InfluxDB : {e}")


    # -------------------- MQTT Callbacks --------------------

    def handle_anomaly_score_message(self, client, userdata, msg):
        """
        MQTT callback triggered when an anomaly score message is received.
        It parses the JSON payload, decodes the embedded Protobuf data to extract a reference timestamp,
        adjusts it using the system’s clock offset, and writes both the raw and filtered anomaly scores to InfluxDB.
        """
        current_time_ns = time.time_ns()

        logging.info("MQTT: score message")
        payload = json.loads(msg.payload.decode("utf-8"))
        system_id = str(payload.get("system_id"))
        anomaly_score = float(payload.get("anomaly_score"))
        filtered_anomaly_score = float(payload.get("filtered_anomaly_score"))
        syscall_protobuf = base64.b64decode(payload.get("syscall_protobuf"))

        try :
            syscall_batch = Syscalls()
            syscall_batch.ParseFromString(syscall_protobuf)
        except DecodeError as e:
            logging.debug(f"Failed to parse Protobuf data for system_id={system_id} : {e}.")
            logging.debug(f"Ensure that 'syscalls_pb2' matches the Protobuf schema used by the data source.")
            return

        if system_id not in self.clock_by_system:
            self.clock_by_system[system_id] = 0

        self.refresh_clock_offset(system_id, syscall_batch, current_time_ns)

        timestamp = syscall_batch.syscalls[0].timestamp_enter
        clock_offset_ns = self.clock_by_system.get(system_id)

        point = Point("anomaly_score").tag("system_id", str(system_id))
        point.field("score", anomaly_score)
        point.field("filtered_score", filtered_anomaly_score)
        point.time(timestamp + clock_offset_ns, write_precision=WritePrecision.NS)

        try:
            write_api = self.influxdb_client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket=self.influxdb_bucket, org=self.influxdb_org, record=point)
        except Exception as e:
            logging.debug(f"Failed to write to InfluxDB: {e}")


    def handle_kill_signal_message(self, client, userdata, msg):
        system_id = "50.50.50.47"
        msg = msg.payload.decode('utf-8')

        if self.save_protobuf :
            file = self.output_file_by_system.get(system_id, None)
            if msg == "END" and file is not None :
                logging.info(f"Closing output file for system_id={system_id}.")
                self.output_file_by_system[system_id] = None
                file.close()



# -------------------- MQTT subscriber setup --------------------

def on_connect(client, userdata, flags, rc, properties=None):
    logging.info(f"Client connected with result code : {rc}.")
    mqtt_topic_list = [(MQTT_TOPIC_ANOMALY_SCORE, 2), (MQTT_TOPIC_INFO_LOADER, 2)]
    client.subscribe(mqtt_topic_list)
    logging.info(f"Client subscribed to topic '{MQTT_TOPIC_ANOMALY_SCORE}' and '{MQTT_TOPIC_INFO_LOADER}'.")


def handle_signal(signum, frame, client, handler):
    logging.info("Stopping MQTT client...")
    try:
        client.disconnect()
        client.loop_stop()
    except Exception as e:
        logging.debug(f"Error while stopping MQTT client: {e}")
    finally:
        handler.close_all_output_files()
        handler.influxdb_client.close()
        logging.info("Output files closed. Exiting.")
    sys.exit(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("mqtt_host",  nargs = '?', default = "172.16.238.13", type=str)
    parser.add_argument("mqtt_port",  nargs = '?', default = 1883, type=int)

    parser.add_argument("-S", "--save-protobuf",   action  = "store_true")
    parser.add_argument("-D", "--storage-dir",     default = "/app/protobuf_data", type=str)

    parser.add_argument("-U", "--influxdb-url",    default = "http://172.16.238.11:8086", type=str)
    parser.add_argument("-T", "--influxdb-token",  default = "KRAVgrITdoSVF0nLcP_jR5N4oa0FGnNt8kheOCGljgUZx80zvTab7ySOZmcBHxlAm4inHAWzYNWpfyEla2xXIA==", type=str)
    parser.add_argument("-O", "--influxdb-org",    default = "cea.org", type=str)
    parser.add_argument("-B", "--influxdb-bucket", default = "sec", type=str)

    parser.add_argument("-v", "--verbose", action = "store_true")

    args = parser.parse_args()
    mqtt_broker_ip   = args.mqtt_host
    mqtt_broker_port = args.mqtt_port
    save_protobuf  = args.save_protobuf
    storage_dir      = args.storage_dir
    influxdb_url     = args.influxdb_url
    influxdb_token   = args.influxdb_token
    influxdb_org     = args.influxdb_org
    influxdb_bucket  = args.influxdb_bucket

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    # Initialize the MQTT message handler
    message_handler = SyscallMessageHandler(influxdb_url, influxdb_token, influxdb_org, influxdb_bucket, save_protobuf ,storage_dir)

    # Create and configure the MQTT client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    # client.message_callback_add(MQTT_TOPIC_SYSCALLS, message_handler.handle_syscall_batch_message)
    client.message_callback_add(MQTT_TOPIC_ANOMALY_SCORE, message_handler.handle_anomaly_score_message)
    client.message_callback_add(MQTT_TOPIC_INFO_LOADER, message_handler.handle_kill_signal_message)

    # Register SIGINT/SIGTERM handlers for graceful exit
    signal.signal(signal.SIGINT, partial(handle_signal, client=client, handler=message_handler))
    signal.signal(signal.SIGTERM, partial(handle_signal, client=client, handler=message_handler))

    # Connect to the MQTT broker and start the network loop
    logging.info("Connecting to MQTT broker...")
    while True:
        try:
            client.connect(mqtt_broker_ip, mqtt_broker_port, keepalive=3600)
            logging.info("Successfully connected to MQTT broker.")
            break
        except Exception as e:
            logging.debug(f"Failed to connect to MQTT broker: {e}")
            logging.debug(f"Retrying in 5 seconds...")
            time.sleep(5)

    logging.info("Client started. Waiting for messages...")
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.loop_forever()
