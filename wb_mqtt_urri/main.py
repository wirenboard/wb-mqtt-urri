import argparse
import json
import logging
import signal
import sys
import threading

import jsonschema
import pyinotify
import requests
import socketio
from wb_common.mqtt_client import DEFAULT_BROKER_URL, MQTTClient

from wb_mqtt_urri import wbmqtt

logger = logging.getLogger(__name__)

CONFIG_FILEPATH = "/etc/wb-mqtt-urri.conf"
SCHEMA_FILEPATH = "/usr/share/wb-mqtt-confed/schemas/wb-mqtt-urri.schema.json"

stop_event = threading.Event()


class MQTTDevice:
    def __init__(self, mqtt_client: MQTTClient):
        self._client = mqtt_client
        self._device = None
        self._urri_device = None
        self._root_topic = None
        logger.debug("MQTT device created")

    def set_urri_device(self, urri_device):
        self._urri_device = urri_device
        self._root_topic = "/devices/" + self._urri_device.id
        logger.debug("Set URRI device %s on %s topic", self._urri_device.title, self._root_topic)

    def publicate(self):
        self._device = wbmqtt.Device(
            mqtt_client=self._client,
            device_mqtt_name=self._urri_device.id,
            device_title=self._urri_device.title,
            driver_name="wb-mqtt-urri",
        )
        self._device.create_control(
            "Power", wbmqtt.ControlMeta(title="Статус", control_type="switch", order=1, read_only=False), ""
        )
        self._device.add_control_message_callback("Power", self.on_message_power)

        self._device.create_control(
            "Volume",
            wbmqtt.ControlMeta(
                title="Громкость", control_type="range", order=2, read_only=False, max_value=100
            ),
            "",
        )
        self._device.add_control_message_callback("Volume", self.on_message_volume)

        self._device.create_control(
            "Playback",
            wbmqtt.ControlMeta(title="Playback", control_type="switch", order=3, read_only=False),
            "",
        )
        self._device.add_control_message_callback("Playback", self.on_message_playback)

        self._device.create_control(
            "Mute", wbmqtt.ControlMeta(title="Mute", control_type="switch", order=4, read_only=False), ""
        )
        self._device.add_control_message_callback("Mute", self.on_message_mute)

        self._device.create_control(
            "AUX", wbmqtt.ControlMeta(title="AUX", control_type="switch", order=5, read_only=False), ""
        )
        self._device.add_control_message_callback("AUX", self.on_message_aux)

        self._device.create_control(
            "Next",
            wbmqtt.ControlMeta(title="Next", control_type="pushbutton", order=6, read_only=False),
            "",
        )
        self._device.add_control_message_callback("Next", self.on_message_next)

        self._device.create_control(
            "Previous",
            wbmqtt.ControlMeta(title="Previous", control_type="pushbutton", order=7, read_only=False),
            "",
        )
        self._device.add_control_message_callback("Previous", self.on_message_previous)

        self._device.create_control(
            "Source Type",
            wbmqtt.ControlMeta(title="Source Type", control_type="text", order=8, read_only=True),
            "",
        )

        self._device.create_control(
            "Radio ID",
            wbmqtt.ControlMeta(title="Radio ID", control_type="value", order=9, read_only=False),
            "",
        )
        self._device.add_control_message_callback("Radio ID", self.on_message_radioid)

        self._device.create_control(
            "Source Name",
            wbmqtt.ControlMeta(title="Source Name", control_type="text", order=10, read_only=True),
            "",
        )
        self._device.create_control(
            "Song Title",
            wbmqtt.ControlMeta(title="Song Title", control_type="text", order=11, read_only=True),
            "",
        )
        self._device.create_control(
            "IP address",
            wbmqtt.ControlMeta(title="IP address", control_type="text", order=12, read_only=True),
            self._urri_device.ip,
        )
        logger.info("%s device created", self._root_topic)

    def update(self, control_name, value):
        self._device.set_control_value(control_name, value)
        logger.debug("%s %s control updated with value %s", self._urri_device.id, control_name, value)

    def set_readonly(self, control_name, value):
        self._device.set_control_read_only(control_name, value)
        logger.debug("%s %s control readonly set to %s", self._urri_device.id, control_name, value)

    def on_message_power(self, _, __, msg):
        new_powerstate = "1" in str(msg.payload)
        self._urri_device.set_power(new_powerstate)
        current_powerstate = self._urri_device.get_power()

        if new_powerstate != current_powerstate:
            logger.warning(
                "URRI %s power state not changed! Current state: %s",
                self._urri_device.title,
                current_powerstate,
            )
        else:
            logger.info("URRI %s power state changed to %s", self._urri_device.title, current_powerstate)

    def on_message_playback(self, _, __, msg):
        value = "1" in str(msg.payload)
        self._urri_device.set_playback(value)
        logger.info("Set playback %s on URRI %s", value, self._urri_device.title)

    def on_message_mute(self, _, __, msg):
        value = "1" in str(msg.payload)
        self._urri_device.set_mute(value)
        logger.info("Set mute %s on URRI %s", value, self._urri_device.title)

    def on_message_aux(self, _, __, msg):
        value = "1" in str(msg.payload)
        self._urri_device.set_aux(value)
        logger.info("Set AUX %s on URRI %s", value, self._urri_device.title)

    def on_message_volume(self, _, __, msg):
        volume = int(str(msg.payload.decode("utf-8")))
        self._urri_device.set_volume(volume)
        logger.info("Set volume %s on URRI %s", volume, self._urri_device.title)

    def on_message_radioid(self, _, __, msg):
        radioid = int(str(msg.payload.decode("utf-8")))
        self._urri_device.set_radioid(radioid)
        logger.info("Set radio ID %s on URRI %s", radioid, self._urri_device.title)

    def on_message_next(self, _, __, ___):
        self._urri_device.set_next()
        logger.info("Set next track on URRI %s", self._urri_device.title)

    def on_message_previous(self, _, __, ___):
        self._urri_device.set_previous()
        logger.info("Set previous track on URRI %s", self._urri_device.title)


class URRIDevice:
    SOURCE_TYPES = {
        0: "Internet Radio",
        1: "File System",
        2: "Preset",
        3: "Multiroom Slave",
        4: "Airplay",
        5: "User Internet Radio",
        6: "Spotify",
    }

    def __init__(self, properties):
        self._id = properties["device_id"]
        self._title = properties["device_title"]
        self._ip = properties["urri_ip"]
        self._url = f"http://{properties['urri_ip']}:{properties['urri_port']}"
        self._urri_client = socketio.Client(logger=False, engineio_logger=False)
        self._mqtt_device = None

        logger.debug("Add device with id " + self._id + " and title " + self._title)

    @property
    def id(self):  # pylint: disable=C0103
        return self._id

    @property
    def title(self):
        return self._title

    @property
    def ip(self):
        return self._ip

    def set_mqtt_device(self, mqtt_device: MQTTDevice):
        self._mqtt_device = mqtt_device
        logger.debug("Set MQTT device for URRI %s", self._id)

    def establish_connection(self):
        self._init_callbacks()
        self._urri_client.connect(self._url)

    def close_connection(self):
        self._urri_client.disconnect()

    def get_power(self):
        response = requests.post(url=(self._url + "/getPower"), timeout=3)
        return "1" in str(response.content)

    def set_power(self, power: bool):
        out_url = "/wakeUp" if power else "/standby"
        requests.post(url=(self._url + out_url), timeout=3)

    def set_playback(self, play: bool):
        out_url = "/play" if play else "/stop"
        requests.post(url=(self._url + out_url), timeout=3)

    def set_mute(self, mute: bool):
        out_url = "/mute" if mute else "/unmute"
        requests.post(url=(self._url + out_url), timeout=3)

    def set_aux(self, aux: bool):
        out_url = "/enableAUX" if aux else "/disableAUX"
        requests.post(url=(self._url + out_url), timeout=3)

    def set_volume(self, volume: int):
        if 0 <= volume <= 100:
            out_url = "/setVolume/" + str(volume)
            requests.post(url=(self._url + out_url), timeout=3)

    def set_radioid(self, radioid: int):
        out_headers = {"Content-Type": "application/json"}
        out_url = "/radio"
        out_data = {"id": radioid}
        out_json = json.dumps(out_data)
        requests.post(headers=out_headers, url=(self._url + out_url), data=out_json, timeout=3)

    def set_next(self):
        requests.post(url=(self._url + "/next"), timeout=3)

    def set_previous(self):
        requests.post(url=(self._url + "/previous"), timeout=3)

    def _init_callbacks(self):
        @self._urri_client.event
        def connect():
            logger.info("Connected to URRI %s", self._url)

        @self._urri_client.event
        def connect_error(data):
            raise ConnectionError(str(data))

        @self._urri_client.on("status")
        def on_status_message(status_dict):
            logger.debug("URRI status message received: %s", status_dict)

            properties = {}
            readonly_properties = {"Radio ID": False, "Next": False, "Previous": False}

            # get status by request
            properties["Power"] = self.get_power()

            # playback status
            if "playback" in status_dict:
                properties["Playback"] = status_dict["playback"] == "play"

            # AUX status
            if "AUX" in status_dict:
                properties["AUX"] = status_dict["AUX"] == "True"

            # muted status
            if "muted" in status_dict:
                properties["Mute"] = status_dict["muted"] == "True"

            # volume
            if "volume" in status_dict:
                properties["Volume"] = status_dict["volume"]

            # source type, name, id
            if "source" in status_dict:
                type_id = status_dict["source"]["sourceType"]
                sourcetype = self.SOURCE_TYPES.get(type_id, "Unknown")
                properties["Source Type"] = sourcetype

                if sourcetype in ["Internet Radio", "Preset", "User Internet Radio", "Spotify"]:
                    properties["Source Name"] = status_dict["source"]["name"]
                elif sourcetype == "File System":
                    properties["Source Name"] = status_dict["source"]["path"]
                else:
                    properties["Source Name"] = ""

                if sourcetype in ["Internet Radio", "Preset", "User Internet Radio"]:
                    properties["Radio ID"] = status_dict["source"]["id"]
                    readonly_properties["Radio ID"] = False
                else:
                    readonly_properties["Radio ID"] = True

                if sourcetype in ["File System", "Presets"]:
                    readonly_properties.update({"Next": False, "Previous": False})
                elif sourcetype == "Spotify":
                    can_do_next = status_dict["source"].get("nextButton", False)
                    can_do_previous = status_dict["source"].get("prevButton", False)
                    readonly_properties.update({"Next": not can_do_next, "Previous": not can_do_previous})
                else:
                    readonly_properties.update({"Next": True, "Previous": True})

            # song title
            properties["Song Title"] = status_dict.get("songTitle", "No Title")

            logger.debug(properties)

            # aux
            if properties.get("AUX", False):
                properties.update({"Source Type": "AUX", "Source Name": "AUX", "Song Title": ""})
                readonly_properties.update({"Radio ID": True, "Next": True, "Previous": True})

            for key, value in properties.items():
                if type(value) is bool:
                    value = "1" if value else "0"
                self._mqtt_device.update(key, value)

            for key, value in readonly_properties.items():
                self._mqtt_device.set_readonly(key, value)


class ConfigHandler(pyinotify.ProcessEvent):
    def __init__(self, path):
        self.path = path
        super().__init__()

    def process_IN_MODIFY(self, event):  # pylint: disable=C0103
        if event.pathname == self.path:
            logger.info("Config file has been modified")
            sys.exit("Config " + self.path + " edited, restarting")


def read_and_validate_config(config_filepath: str, schema_filepath: str) -> dict:
    with open(config_filepath, "r", encoding="utf-8") as config_file, open(
        schema_filepath, "r", encoding="utf-8"
    ) as schema_file:
        try:
            config = json.load(config_file)
            schema = json.load(schema_file)
            jsonschema.validate(config, schema)

            if config.get("device_id") is not None:
                logger.error("Old version of config file! Please update it")
                device = {}
                for field in ["device_id", "device_title", "urri_ip", "urri_port"]:
                    device[field] = config.pop(field, None)
                config.update({"devices": [device]})

            id_list = [device["device_id"] for device in config["devices"]]
            if len(id_list) != len(set(id_list)):
                raise ValueError("Device ID's must be unique")

            return config
        except (jsonschema.exceptions.ValidationError, ValueError, DeprecationWarning) as e:
            logger.error("Config file validation failed! Error: %s", e)
            return None


def to_json(config_filepath: str) -> dict:
    with open(config_filepath, "r", encoding="utf-8") as config_file:
        config = json.load(config_file)

        if config.get("urri_ip") is not None:  # old version of config
            device = {}
            device["device_id"] = config.pop("device_id", "urri")
            device["device_title"] = config.pop("device_title", "Network Receiver URRI")
            device["urri_ip"] = config.pop("urri_ip", "")
            device["urri_port"] = config.pop("urri_port", 9032)
            config.update({"devices": [device], "debug": config.pop("debug", False)})

        return config


def _signal(*_):
    stop_event.set()


def main(argv):
    logger.info("URRI service starting")

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-j", action="store_true", help="Make JSON for wb-mqtt-confed from /etc/wb-mqtt-urri.conf"
    )
    args = parser.parse_args(argv[1:])

    if args.j:
        config = to_json(CONFIG_FILEPATH)
        json.dump(config, sys.stdout, sort_keys=True, indent=2)
        sys.exit(0)

    config = read_and_validate_config(CONFIG_FILEPATH, SCHEMA_FILEPATH)
    if config is None:
        sys.exit(6)  # systemd status=6/NOTCONFIGURED

    urri_devices = []
    for json_device in config["devices"]:
        urri_devices.append(URRIDevice(json_device))

    logger.setLevel(logging.DEBUG if bool(config["debug"]) else logging.INFO)

    try:
        watch_manager = pyinotify.WatchManager()
        notifier = pyinotify.ThreadedNotifier(watch_manager, ConfigHandler(CONFIG_FILEPATH))
        watch_manager.add_watch(CONFIG_FILEPATH, pyinotify.IN_MODIFY, rec=False)  # pylint: disable=E1101
        notifier.start()

        mqtt_client = MQTTClient("wb-mqtt-urri", DEFAULT_BROKER_URL)
        mqtt_client.start()

        logger.debug("MQTT client started")

        for urri_device in urri_devices:
            mqtt_device = MQTTDevice(mqtt_client)
            mqtt_device.set_urri_device(urri_device)
            urri_device.set_mqtt_device(mqtt_device)
            mqtt_device.publicate()
            urri_device.establish_connection()

        stop_event.wait()
    except ConnectionError as e:
        logger.error("Connection to URRI failed! Error: %s", e)
    finally:
        mqtt_client.stop()
        notifier.stop()

        for urri_device in urri_devices:
            urri_device.close_connection()

        logger.info("URRI service stopped")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
