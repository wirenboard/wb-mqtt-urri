# pylint: disable=protected-access
import asyncio
import json

import requests

from wb_mqtt_urri import main as main_module
from wb_mqtt_urri.main import MQTTDevice, URRIClient, read_and_validate_config

TEST_CONFIG = {
    "debug": True,
    "devices": [
        {
            "device_id": "urr1",
            "device_title": "urr1",
            "urri_ip": "192.0.2.1",
            "urri_port": 9032,
        }
    ],
}


class URRIDeviceMock:
    def __init__(self, properties):
        assert properties == TEST_CONFIG["devices"][0]
        self.id = properties["device_id"]
        self.title = properties["device_title"]
        self.ip = properties["urri_ip"]

    def set_mqtt_device(self, _):
        pass

    async def run(self):
        pass

    async def stop(self):
        pass


def setup_mqtt_mock(mocker):
    publications = []
    mqtt_client_class = mocker.patch("wb_mqtt_urri.main.MQTTClient")
    mqtt_client = mqtt_client_class.return_value

    def publish(topic, value, qos=0, retain=False):
        publish_result = mocker.Mock()
        publish_result.is_published.return_value = True
        publications.append((topic, value, qos, retain))
        return publish_result

    mqtt_client.publish.side_effect = publish
    return mqtt_client, publications


def test_initial_connect_and_reconnect_publish_same_topics(mocker):
    mqtt_client, publications = setup_mqtt_mock(mocker)
    mocker.patch("wb_mqtt_urri.main.URRIDevice", side_effect=URRIDeviceMock)
    urri_client = URRIClient(TEST_CONFIG["devices"])
    publication_snapshots = []

    def reconnect_and_stop():
        publication_snapshots.append(list(publications))
        publications.clear()
        mqtt_client.on_disconnect(None, None, None)
        mqtt_client.on_connect(None, None, None, 0)
        publication_snapshots.append(list(publications))
        publications.clear()
        urri_client._on_term_signal()

    def start():
        assert not publications
        mqtt_client.on_connect(None, None, None, 0)
        asyncio.get_running_loop().call_soon(reconnect_and_stop)

    mqtt_client.start.side_effect = start

    assert asyncio.run(urri_client.run()) == 7
    assert publication_snapshots[0] == publication_snapshots[1]
    assert publications
    assert all(value is None and qos == 1 and retain for _, value, qos, retain in publications)


def test_authentication_failure_returns_2(mocker):
    mqtt_client, publications = setup_mqtt_mock(mocker)
    mocker.patch("wb_mqtt_urri.main.URRIDevice", side_effect=URRIDeviceMock)
    mqtt_client.start.side_effect = lambda: mqtt_client.on_connect(None, None, None, 5)

    assert asyncio.run(URRIClient(TEST_CONFIG["devices"]).run()) == 2
    assert not publications


def test_cleanup_failure_is_logged(mocker, caplog):
    mqtt_client, _ = setup_mqtt_mock(mocker)
    mocker.patch("wb_mqtt_urri.main.URRIDevice", side_effect=URRIDeviceMock)
    urri_client = URRIClient(TEST_CONFIG["devices"])

    def disconnect_and_stop():
        mqtt_client.on_disconnect(None, None, None)
        urri_client._on_term_signal()

    def start():
        mqtt_client.on_connect(None, None, None, 0)
        asyncio.get_running_loop().call_soon(disconnect_and_stop)

    mqtt_client.start.side_effect = start

    assert asyncio.run(urri_client.run()) == 7
    assert "Failed to clear retained MQTT topics" in caplog.text


def test_command_connection_error_does_not_escape_callback(mocker):
    mqtt_client, publications = setup_mqtt_mock(mocker)
    urri_device = mocker.Mock(id="urr1", title="urr1", ip="192.0.2.1")
    urri_device.set_power.side_effect = requests.ConnectionError("unreachable")
    mqtt_device = MQTTDevice(mqtt_client)
    mqtt_device.set_urri_device(urri_device)
    mqtt_device.publicate()
    publications.clear()

    callbacks = {call.args[0]: call.args[1] for call in mqtt_client.message_callback_add.call_args_list}
    callbacks["/devices/urr1/controls/Power/on"](None, None, mocker.Mock(payload=b"1"))

    power_meta = [
        json.loads(value)
        for topic, value, _, _ in publications
        if topic == "/devices/urr1/controls/Power/meta"
    ]
    assert power_meta[-1]["error"] == "w"


def test_missing_config_returns_6(mocker, tmp_path):
    mocker.patch.object(main_module, "SCHEMA_FILEPATH", "wb-mqtt-urri.schema.json")
    assert main_module.main(["wb-mqtt-urri", "-c", str(tmp_path / "missing.conf")]) == 6


def test_empty_config_returns_7(mocker, tmp_path):
    config_path = tmp_path / "empty.conf"
    config_path.write_text('{"devices": [], "debug": false}', encoding="utf-8")
    mocker.patch.object(main_module, "SCHEMA_FILEPATH", "wb-mqtt-urri.schema.json")

    assert main_module.main(["wb-mqtt-urri", "-c", str(config_path)]) == 7


def test_fractional_port_is_invalid(tmp_path):
    config_path = tmp_path / "fractional-port.conf"
    config_path.write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "device_id": "urr1",
                        "device_title": "urr1",
                        "urri_ip": "192.0.2.1",
                        "urri_port": 9032.5,
                    }
                ],
                "debug": False,
            }
        ),
        encoding="utf-8",
    )

    assert read_and_validate_config(config_path, "wb-mqtt-urri.schema.json") is None
