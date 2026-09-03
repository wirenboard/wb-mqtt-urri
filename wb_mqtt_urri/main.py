import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from threading import Lock

import jsonschema
import requests
import socketio
from wb_common.mqtt_client import DEFAULT_BROKER_URL, MQTTClient
from wb_mqtt_urri import wbmqtt

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger.setLevel(logging.INFO)


CONFIG_FILEPATH = "/etc/wb-mqtt-urri.conf"
SCHEMA_FILEPATH = "/usr/share/wb-mqtt-confed/schemas/wb-mqtt-urri.schema.json"


class MQTTDevice:
    def __init__(self, mqtt_client: MQTTClient):
        self._client = mqtt_client
        self._device = None
        self._urri_device = None
        self._receiver_has_error = False
        self._warning_controls = set()
        self._root_topic = None
        logger.debug("MQTT device created")

    def set_urri_device(self, urri_device):
        self._urri_device = urri_device
        self._root_topic = "/devices/" + self._urri_device.id
        logger.debug("Set URRI device %s on %s topic", self._urri_device.title, self._root_topic)

    def publicate(self):
        self._create_controls()
        self._subscribe_on_topics()

    @property
    def is_published(self):
        return self._device is not None

    def _create_controls(self):
        self._device = wbmqtt.Device(
            mqtt_client=self._client,
            device_mqtt_name=self._urri_device.id,
            device_title=self._urri_device.title,
            driver_name="wb-mqtt-urri",
        )
        self._device.create_control(
            "Power", wbmqtt.ControlMeta(title="Power", control_type="switch", order=1, read_only=False), "0"
        )
        self._device.create_control(
            "Volume",
            wbmqtt.ControlMeta(title="Volume", control_type="range", order=2, read_only=False, max_value=100),
            0,
        )
        self._device.create_control(
            "Playback",
            wbmqtt.ControlMeta(title="Playback", control_type="switch", order=3, read_only=False),
            "0",
        )
        self._device.create_control(
            "Mute",
            wbmqtt.ControlMeta(title="Mute", control_type="switch", order=4, read_only=False),
            "0",
        )
        self._device.create_control(
            "AUX",
            wbmqtt.ControlMeta(title="AUX", control_type="switch", order=5, read_only=False),
            "0",
        )
        self._device.create_control(
            "Next",
            wbmqtt.ControlMeta(title="Next", control_type="pushbutton", order=6, read_only=False),
            "",
        )
        self._device.create_control(
            "Previous",
            wbmqtt.ControlMeta(title="Previous", control_type="pushbutton", order=7, read_only=False),
            "",
        )
        self._device.create_control(
            "Source Type",
            wbmqtt.ControlMeta(title="Source Type", control_type="text", order=8, read_only=True),
            "",
        )
        self._device.create_control(
            "Radio ID",
            wbmqtt.ControlMeta(title="Radio ID", control_type="value", order=9, read_only=False),
            0,
        )
        self._device.create_control(
            "Preset ID",
            wbmqtt.ControlMeta(
                title="Preset ID", control_type="value", min_value=0, max_value=3, order=10, read_only=False
            ),
            0,
        )
        self._device.create_control(
            "Source Name",
            wbmqtt.ControlMeta(title="Source Name", control_type="text", order=11, read_only=True),
            "",
        )
        self._device.create_control(
            "Song Title",
            wbmqtt.ControlMeta(title="Song Title", control_type="text", order=12, read_only=True),
            "",
        )
        self._device.create_control(
            "IP address",
            wbmqtt.ControlMeta(title="IP address", control_type="text", order=13, read_only=True),
            self._urri_device.ip,
        )
        self._device.create_control(
            "Play Folder",
            wbmqtt.ControlMeta(title="Play Folder", control_type="text", order=14, read_only=False),
            "",
        )
        self._device.create_control(
            "Play Alert",
            wbmqtt.ControlMeta(title="Play Alert", control_type="text", order=15, read_only=False),
            "",
        )
        logger.info("%s device created", self._root_topic)

    def _add_callback(self, control_name, callback):
        def wrapper(client, userdata, msg):
            self._set_control_warning(control_name, False)
            try:
                callback(client, userdata, msg)
            except (requests.exceptions.RequestException, ValueError, KeyError) as error:
                self._set_control_warning(control_name, True)
                logger.warning("URRI %s %s command failed: %s", self._urri_device.title, control_name, error)

        self._device.add_control_message_callback(control_name, wrapper)

    def _subscribe_on_topics(self):
        self._add_callback("Power", self._on_message_power)
        self._add_callback("Volume", self._on_message_volume)
        self._add_callback("Playback", self._on_message_playback)
        self._add_callback("Mute", self._on_message_mute)
        self._add_callback("AUX", self._on_message_aux)
        self._add_callback("Next", self._on_message_next_track)
        self._add_callback("Previous", self._on_message_previous_track)
        self._add_callback("Radio ID", self._on_message_radioid)
        self._add_callback("Preset ID", self._on_message_presetid)
        self._add_callback("Play Folder", self._on_message_play_folder)
        self._add_callback("Play Alert", self._on_message_play_alert)

    def update(self, control_name, value):
        self._device.set_control_value(control_name, value)
        logger.debug("%s %s control updated with value %s", self._urri_device.id, control_name, value)

    def set_readonly(self, control_name, value):
        self._device.set_control_read_only(control_name, value)
        logger.debug("%s %s control readonly set to %s", self._urri_device.id, control_name, value)

    def set_error_state(self, error: bool):
        self._receiver_has_error = error
        for control_name in self._device.get_controls_list():
            if control_name != "IP address":
                self._update_control_error(control_name)

    def _set_control_warning(self, control_name, warning):
        if warning:
            self._warning_controls.add(control_name)
        else:
            self._warning_controls.discard(control_name)
        self._update_control_error(control_name)

    def _update_control_error(self, control_name):
        error = "r" if self._receiver_has_error else ("w" if control_name in self._warning_controls else "")
        self._device.set_control_error(control_name, error)

    def republish(self):
        if self._device is not None:
            self._device.republish_device()
            self._subscribe_on_topics()

    def remove(self):
        if self._device is None:
            return []
        publish_results = self._device.remove_device()
        self._device = None
        logger.info("%s device deleted", self._root_topic)
        return publish_results

    def _on_message_power(self, _, __, msg):
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
            logger.info(
                "URRI %s power state changed to %s",
                self._urri_device.title,
                current_powerstate,
            )

    def _on_message_playback(self, _, __, msg):
        value = "1" in str(msg.payload)
        self._urri_device.set_playback(value)
        logger.info("Set playback %s on URRI %s", value, self._urri_device.title)

    def _on_message_mute(self, _, __, msg):
        value = "1" in str(msg.payload)
        self._urri_device.set_mute(value)
        logger.info("Set mute %s on URRI %s", value, self._urri_device.title)

    def _on_message_aux(self, _, __, msg):
        value = "1" in str(msg.payload)
        self._urri_device.set_aux(value)
        logger.info("Set AUX %s on URRI %s", value, self._urri_device.title)

    def _on_message_volume(self, _, __, msg):
        volume = int(str(msg.payload.decode("utf-8")))
        self._urri_device.set_volume(volume)
        logger.info("Set volume %s on URRI %s", volume, self._urri_device.title)

    def _on_message_radioid(self, _, __, msg):
        radioid = int(str(msg.payload.decode("utf-8")))
        result = self._urri_device.play_radio_by_id(radioid)
        self._set_control_warning("Radio ID", not result)
        if result:
            logger.info("Set radio ID %s on URRI %s", radioid, self._urri_device.title)
        else:
            logger.warning("Radio ID %s not found on URRI %s", radioid, self._urri_device.title)

    def _on_message_presetid(self, _, __, msg):
        presetid = int(str(msg.payload.decode("utf-8")))
        self._urri_device.play_preset_by_number(presetid)
        logger.info("Set preset ID %s on URRI %s", presetid, self._urri_device.title)

    def _on_message_next_track(self, _, __, ___):
        self._urri_device.play_next_track()
        logger.info("Play next track on URRI %s", self._urri_device.title)

    def _on_message_previous_track(self, _, __, ___):
        self._urri_device.play_previous_track()
        logger.info("Play previous track on URRI %s", self._urri_device.title)

    def _on_message_play_folder(self, _, __, msg):
        folder = msg.payload.decode("utf-8")
        result = self._urri_device.play_usb_folder(folder)
        self._set_control_warning("Play Folder", not result)
        if result:
            logger.info("Play USB folder %s on URRI %s", folder, self._urri_device.title)

    def _on_message_play_alert(self, _, __, msg):
        alert = msg.payload.decode("utf-8")
        result = self._urri_device.play_alert_by_name(alert)
        self._set_control_warning("Play Alert", not result)
        if result:
            logger.info("Alert %s played on URRI %s", alert, self._urri_device.title)
        else:
            logger.warning("Alert %s not found on URRI %s", alert, self._urri_device.title)


class URRIDevice:  # pylint: disable=too-many-instance-attributes
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
        self._urri_client = socketio.AsyncClient(logger=False, engineio_logger=False)
        self._mqtt_device = None
        self._properties = {}
        self._connection_error_reported = False

        self._init_callbacks()

        logger.debug("Add device with id " + self._id + " and title " + self._title)

    @property
    def id(self):  # pylint: disable=invalid-name
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

    async def run(self):
        try:
            while True:
                try:
                    await self._urri_client.connect(self._url)
                    await self._urri_client.wait()
                except socketio.exceptions.ConnectionError as e:
                    self._mqtt_device.set_error_state(True)
                    if not self._connection_error_reported:
                        self._connection_error_reported = True
                        logger.error("URRI %s connection error: %s", self._id, e)
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            logger.debug("URRI device %s run task cancelled", self._id)

    async def stop(self):
        await self._urri_client.disconnect()

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
            requests.post(url=f"{self._url}/setVolume/{volume}", timeout=3)

    def play_radio_by_id(self, radioid: int):
        out_headers = {"Content-Type": "application/json"}
        out_data = {"id": radioid}
        response = requests.post(
            headers=out_headers, url=f"{self._url}/radio", data=json.dumps(out_data), timeout=3
        )
        logger.debug("Play radio by id response: %s %s", self._id, response.json())
        return response.json()["success"]

    def play_preset_by_number(self, preset_number: int):
        response = requests.post(url=f"{self._url}/preset/{preset_number}/play", timeout=3)
        logger.debug("Play preset by number response: %s %s", self._id, response.json())

    def get_alert_files(self):
        response = requests.post(url=f"{self._url}/alert/getSongs", timeout=3)
        logger.debug("Get alert files response: %s %s", self._id, response.json())
        return response.json()

    def play_alert_by_name(self, alert_name: str):
        try:
            alert_name = alert_name.removeprefix("/")
            alerts = self.get_alert_files()
            alert_id = alerts.index(alert_name)

            out_headers = {"Content-Type": "application/json"}
            out_data = {"fileIndex": alert_id}
            response = requests.post(
                headers=out_headers, url=f"{self._url}/alert/notify", data=json.dumps(out_data), timeout=3
            )
            logger.debug("Play alert by name response: %s %s", self._id, response.json())
            return response.json()["success"]
        except ValueError:
            logger.debug("Alert %s %s not found", self._id, alert_name)
            return False

    def play_usb_folder(self, path: str):
        _, path_and_file = os.path.splitdrive(path)
        path_and_file = path_and_file.removeprefix("/")
        if not path_and_file.endswith("/"):
            path_and_file += "/"

        path, file = os.path.split(path_and_file)

        if not path.startswith("usb"):
            logger.warning("Play folder on URRI %s failed! Path %s is not USB", self._id, path)
            return False

        if file != "":
            logger.warning("Play folder on URRI %s failed! File %s is not a folder", self._id, file)
            return False

        out_headers = {"Content-Type": "application/json"}
        out_data = {"path": path}
        response = requests.post(
            headers=out_headers,
            url=(self._url + "/sources/usb/play"),
            data=json.dumps(out_data),
            timeout=3,
        )
        logger.debug("Play USB folder response: %s %s", self._id, response.json())
        return response.json()["success"]

    def play_next_track(self):
        response = requests.post(url=f"{self._url}/next", timeout=3)
        logger.debug("Play next track response: %s", response.json())

    def play_previous_track(self):
        requests.post(url=(self._url + "/previous"), timeout=3)

    def _init_callbacks(self):  # pylint: disable=too-many-statements
        @self._urri_client.event
        async def connect():
            if self._connection_error_reported:
                logger.info("URRI %s connection restored", self._id)
            self._connection_error_reported = False
            self._mqtt_device.set_error_state(False)
            logger.info("Connected to URRI %s", self._url)

        @self._urri_client.on("status")
        async def on_status_message(status_dict):  # pylint: disable=too-many-branches
            logger.debug("URRI status message received: %s", status_dict)

            properties = {}
            readonly_properties = {
                "Next": False,
                "Previous": False,
            }

            # get status by request
            properties["Power"] = self.get_power()

            # playback status
            if "playback" in status_dict:
                properties["Playback"] = status_dict["playback"] == "play"

            # AUX status
            if "AUX" in status_dict:
                properties["AUX"] = status_dict["AUX"]

            # muted status
            if "muted" in status_dict:
                properties["Mute"] = status_dict["muted"]

            # volume
            if "volume" in status_dict:
                properties["Volume"] = status_dict["volume"]

            # source type, name, id
            if "source" in status_dict:
                type_id = status_dict["source"]["sourceType"]
                sourcetype = self.SOURCE_TYPES.get(type_id, "Unknown")
                properties["Source Type"] = sourcetype
                properties["Set Source"] = type_id

                if sourcetype in ["Internet Radio", "Preset", "User Internet Radio", "Spotify"]:
                    properties["Source Name"] = status_dict["source"]["name"]
                elif sourcetype == "File System":
                    properties["Source Name"] = status_dict["source"]["path"]
                else:
                    properties["Source Name"] = ""

                if sourcetype in ["Internet Radio", "User Internet Radio"]:
                    properties["Radio ID"] = status_dict["source"]["id"]
                elif sourcetype == "Preset":
                    properties["Radio ID"] = status_dict["source"]["id"]
                    properties["Preset ID"] = status_dict["source"]["index"]

                if sourcetype in ["File System", "Preset"]:
                    readonly_properties.update({"Next": False, "Previous": False})
                elif sourcetype == "Spotify":
                    can_do_next = status_dict["source"].get("nextButton", False)
                    can_do_prev = status_dict["source"].get("prevButton", False)
                    readonly_properties.update({"Next": not can_do_next, "Previous": not can_do_prev})
                else:
                    readonly_properties.update({"Next": True, "Previous": True})

            # song title
            properties["Song Title"] = status_dict.get("songTitle", "No Title")

            # aux
            if properties.get("AUX", False):
                properties.update({"Source Type": "AUX", "Source Name": "AUX", "Song Title": ""})
                readonly_properties.update({"Next": True, "Previous": True})

            self._properties.update(properties)

            for key, value in properties.items():
                if isinstance(value, bool):
                    value = "1" if value else "0"
                self._mqtt_device.update(key, value)

            for key, value in readonly_properties.items():
                self._mqtt_device.set_readonly(key, value)


class URRIClient:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    MQTT_CLEANUP_TIMEOUT_S = 1

    def __init__(self, devices_config) -> None:
        self._devices_config = devices_config
        self._urri_devices = []
        self._mqtt_devices = []
        self._mqtt_client = None
        self._event_loop = None
        self._mqtt_connected_event = None
        self._stop_event = None
        self._exit_code = 1
        self._mqtt_connected = False
        self._shutting_down = False
        self._lock = Lock()

    def _request_stop(self, exit_code):
        if not self._stop_event.is_set():
            self._exit_code = exit_code
            self._stop_event.set()

    def _on_mqtt_client_connect(self, _, __, ___, rc):
        if rc in (4, 5):
            logger.error("MQTT authentication failed with rc %s", rc)
            self._event_loop.call_soon_threadsafe(self._request_stop, 2)
            return
        if rc != 0:
            logger.warning("MQTT connection failed with rc %s, retrying", rc)
            return

        with self._lock:
            self._mqtt_connected = True
            mqtt_devices = [] if self._shutting_down else list(self._mqtt_devices)

        try:
            for mqtt_device in mqtt_devices:
                if mqtt_device.is_published:
                    mqtt_device.republish()
                else:
                    mqtt_device.publicate()
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.exception("Failed to publish MQTT devices: %s", error)
            self._event_loop.call_soon_threadsafe(self._request_stop, 1)
            return

        self._event_loop.call_soon_threadsafe(self._mqtt_connected_event.set)
        logger.info("MQTT client connected")

    def _on_mqtt_client_disconnect(self, _, __, ___):
        with self._lock:
            self._mqtt_connected = False
        logger.info("MQTT client disconnected")

    def _on_term_signal(self):
        self._request_stop(7)
        logger.info("SIGTERM or SIGINT received, exiting")

    def _create_devices(self):
        for device_config in self._devices_config:
            urri_device = URRIDevice(device_config)
            mqtt_device = MQTTDevice(self._mqtt_client)
            mqtt_device.set_urri_device(urri_device)
            urri_device.set_mqtt_device(mqtt_device)
            self._urri_devices.append(urri_device)
            self._mqtt_devices.append(mqtt_device)

    async def _stop_urri_devices(self):
        await asyncio.gather(
            *[urri_device.stop() for urri_device in self._urri_devices], return_exceptions=True
        )

    def _remove_mqtt_devices(self):
        with self._lock:
            self._shutting_down = True
            mqtt_connected = self._mqtt_connected

        publish_results = []
        for mqtt_device in self._mqtt_devices:
            publish_results.extend(mqtt_device.remove())

        deadline = time.monotonic() + self.MQTT_CLEANUP_TIMEOUT_S
        cleanup_succeeded = mqtt_connected and bool(publish_results)
        for publish_result in publish_results:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                cleanup_succeeded = False
                break
            try:
                publish_result.wait_for_publish(timeout=remaining_time)
                if not publish_result.is_published():
                    cleanup_succeeded = False
                    break
            except (RuntimeError, ValueError):
                cleanup_succeeded = False
                break

        if not cleanup_succeeded:
            logger.error("Failed to clear retained MQTT topics: broker is unavailable")

    async def run(self):
        result = 1
        tasks = []
        self._event_loop = asyncio.get_running_loop()
        self._mqtt_connected_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._event_loop.add_signal_handler(signal.SIGTERM, self._on_term_signal)
        self._event_loop.add_signal_handler(signal.SIGINT, self._on_term_signal)

        try:
            self._mqtt_client = MQTTClient("wb-mqtt-urri", DEFAULT_BROKER_URL)
            self._mqtt_client.on_connect = self._on_mqtt_client_connect
            self._mqtt_client.on_disconnect = self._on_mqtt_client_disconnect
            self._create_devices()
            self._mqtt_client.start()
            logger.debug("MQTT client started")

            mqtt_ready_task = asyncio.create_task(self._mqtt_connected_event.wait())
            stop_task = asyncio.create_task(self._stop_event.wait())
            tasks.extend((mqtt_ready_task, stop_task))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if stop_task in done:
                result = self._exit_code
                return result

            urri_tasks = [asyncio.create_task(device.run()) for device in self._urri_devices]
            all_urri_tasks = asyncio.gather(*urri_tasks)
            tasks.append(all_urri_tasks)
            done, _ = await asyncio.wait((all_urri_tasks, stop_task), return_when=asyncio.FIRST_COMPLETED)
            if stop_task in done:
                result = self._exit_code
                return result

            await all_urri_tasks
            logger.error("All URRI device tasks stopped unexpectedly")
            return result
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._stop_urri_devices()
            if result == 7:
                self._remove_mqtt_devices()
            if self._mqtt_client is not None:
                self._mqtt_client.stop()
            logger.debug("MQTT client stopped")


def read_and_validate_config(config_filepath: str, schema_filepath: str) -> dict:
    try:
        with open(config_filepath, "r", encoding="utf-8") as config_file, open(
            schema_filepath, "r", encoding="utf-8"
        ) as schema_file:
            config = json.load(config_file)
            schema = json.load(schema_file)

        if not isinstance(config, dict):
            raise ValueError("The root config value must be an object")
        if config.get("device_id") is not None:
            logger.warning("Old version of config file, please update it")
            device = {}
            for field in ["device_id", "device_title", "urri_ip", "urri_port"]:
                device[field] = config.pop(field, None)
            config.update({"devices": [device]})

        jsonschema.validate(config, schema, format_checker=jsonschema.draft4_format_checker)
        id_list = [device["device_id"] for device in config["devices"]]
        if len(id_list) != len(set(id_list)):
            raise ValueError("Device ID's must be unique")

        return config
    except (OSError, jsonschema.exceptions.ValidationError, ValueError, TypeError) as e:
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


def main(argv):
    logger.info("URRI service starting")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-j",
        action="store_true",
        help="Make JSON for wb-mqtt-confed from /etc/wb-mqtt-urri.conf",
    )
    parser.add_argument("-c", "--config", type=str, default=CONFIG_FILEPATH, help="Config file")
    args = parser.parse_args(argv[1:])

    if args.j:
        config = to_json(args.config)
        json.dump(config, sys.stdout, sort_keys=True, indent=2)
        return 0

    config = read_and_validate_config(args.config, SCHEMA_FILEPATH)
    if config is None:
        return 6  # systemd status=6/NOTCONFIGURED
    if config["debug"]:
        logging.getLogger().setLevel(logging.DEBUG)

    if not config["devices"]:
        logger.info("No devices configured, nothing to do")
        return 7  # systemd status=7/NOTRUNNING

    urri_client = URRIClient(config["devices"])
    result = asyncio.run(urri_client.run())

    logger.info("URRI service stopped")

    return result


if __name__ == "__main__":
    sys.exit(main(sys.argv))
