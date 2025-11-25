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
import base64
import time
import json
import pathlib
from functools import partial

import paho.mqtt.client as mqtt
import onnxruntime
import numpy as np

from syscalls_pb2 import Syscalls # Generated at compile time from protoc

MQTT_TOPIC_SYSCALLS = "eArgos/syscall_values"
MQTT_TOPIC_ANOMALY_SCORE = "eArgos/anomaly_score"

class SyscallAnomalyModel:
    def __init__(self, syscall_id_mapping_path: str|pathlib.Path, model_path: str|pathlib.Path, ewma_alpha: float|None=0.75, filtering_size: int=5, filtering_nb: int=2):
        self.syscall_id_mapping_path = syscall_id_mapping_path
        self.model_path = model_path

        # For filtering
        self.ewma_alpha = ewma_alpha
        self.filtering_size = filtering_size
        self.filtering_nb = filtering_nb
        self.anomaly_score_history = []
        self.last_ewma_value = None

        with open(self.syscall_id_mapping_path, 'r') as f:
            self.syscall_id_mapping = json.load(f)['syscall_seq']
        self.ort_session = onnxruntime.InferenceSession(self.model_path, providers=['CPUExecutionProvider'])

    @property
    def nb_inputs(self):
        return len(self.ort_session.get_inputs())

    def compute_score(self, x):
        x_mapped = [str(self.syscall_id_mapping[str(i)]) for i in x if str(i) in self.syscall_id_mapping]
        str_syscall_seq = np.array([" ".join(x_mapped)])

        if self.nb_inputs == 1:
            model_input = {'input_0': str_syscall_seq}
        elif self.nb_inputs == 2:
            nb_syscalls = np.array([len(x_mapped)]).reshape((1, 1)).astype(np.int64)
            model_input = {'input_0': str_syscall_seq.reshape((1, 1)), 'input_1': nb_syscalls}
        else:
            raise ValueError("Models with more than 2 inputs are not supported.")

        return self.ort_session.run(None, model_input)[-1].ravel()[0]

    def filter_score(self, anomaly_score):
        if self.last_ewma_value is None:
            filtered_score = anomaly_score
            self.last_ewma_value = filtered_score
        else:
            filtered_score = self.ewma_alpha * anomaly_score + (1 - self.ewma_alpha) * self.last_ewma_value
            self.last_ewma_value = filtered_score

        self.anomaly_score_history.append(filtered_score)
        if len(self.anomaly_score_history) == self.filtering_size:
            if filtered_score > np.max(self.anomaly_score_history[:-1]):
                filtered_score = np.sort(self.anomaly_score_history)[-self.filtering_nb]
            self.anomaly_score_history.pop(0)

        return filtered_score

    def on_message(self, client, userdata, msg):
        system_id = "50.50.50.47"
        syscall_batch = Syscalls()
        syscall_batch.ParseFromString(msg.payload)

        x_input = [sysc.syscall_id for sysc in syscall_batch.syscalls]

        anomaly_score = float(self.compute_score(x_input))

        filtered_anomaly_score = self.filter_score(anomaly_score)

        payload = {
            "system_id": str(system_id),
            "anomaly_score": float(anomaly_score),
            "filtered_anomaly_score": float(filtered_anomaly_score),
            "syscall_protobuf": base64.b64encode(msg.payload).decode("utf-8")
        }

        client.publish(MQTT_TOPIC_ANOMALY_SCORE, json.dumps(payload))

def on_connect(client, userdata, flags, rc, properties=None):
    logging.info(f"Client connected with result code : {rc}.")
    mqtt_topic_list = [(MQTT_TOPIC_SYSCALLS,2), (MQTT_TOPIC_ANOMALY_SCORE,2)]
    client.subscribe(mqtt_topic_list)
    logging.info(f"Client subscribed to topic '{MQTT_TOPIC_SYSCALLS}' and '{MQTT_TOPIC_ANOMALY_SCORE}'.")

def handle_signal(signum, frame, client):
    logging.info("Stopping MQTT client...")
    try:
        client.disconnect()
        client.loop_stop()
    except Exception as e:
        logging.debug(f"Error while stopping MQTT client: {e}")
    sys.exit(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("bind_addr",    nargs='?', default = "172.16.238.13", type=str)
    parser.add_argument("bind_port",    nargs='?', default = 1883, type=int)
    parser.add_argument("mapping_path", nargs='?', default = "/app/detection_models/consistent-naming-mapping.json", type=str)
    parser.add_argument("model_path",   nargs='?', default = "/app/detection_models/cea_demo_isolation_forest_0_001.onnx", type=str)

    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    broker_ip       = args.bind_addr
    broker_port     = args.bind_port
    mapping_path    = args.mapping_path
    model_path      = args.model_path

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    # Init the AnomalyModel
    syscall_model = SyscallAnomalyModel(mapping_path, model_path, 0.75, 5, 2)

    # Create and configure the MQTT client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.message_callback_add(MQTT_TOPIC_SYSCALLS, syscall_model.on_message)

    # Register SIGINT/SIGTERM handlers for graceful exit
    signal.signal(signal.SIGINT, partial(handle_signal, client=client))
    signal.signal(signal.SIGTERM, partial(handle_signal, client=client))

    # Connect to the MQTT broker and start the network loop
    logging.info("Connecting to MQTT broker...")
    while True:
        try:
            client.connect(broker_ip, broker_port, keepalive=3600)
            logging.info("Successfully connected to MQTT broker.")
            break
        except Exception as e:
            logging.debug(f"Failed to connect to MQTT broker: {e}")
            logging.debug(f"Retrying in 5 seconds...")
            time.sleep(5)

    logging.info("Client started. Waiting for messages...")
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.loop_forever()
