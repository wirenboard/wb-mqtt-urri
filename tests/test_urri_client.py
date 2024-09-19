import asyncio

from wb_mqtt_urri.main import URRIClient


TEST_CONFIG = {
    "debug": True,
    "devices": [{"device_id": "urr1", "device_title": "urr1", "urri_ip": "192.168.2.103", "urri_port": 9032}],
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


#@pytest.mark.asyncio
def test_mosquitto_restart(mocker):
    publications = []

    def publish(topic, value, retain):  # pylint: disable=unused-argument
        publications.append((topic, value))

    mocked = mocker.patch("wb_mqtt_urri.main.MQTTClient")
    mocked.return_value.publish.side_effect=publish
    mocker.patch("wb_mqtt_urri.main.URRIDevice", side_effect=URRIDeviceMock)
    urri_client = URRIClient(TEST_CONFIG["devices"])
    
    asyncio.run(urri_client.run())
    old_publications = publications
    publications = []

    urri_client._mqtt_client.on_disconnect(None,None,None)
    urri_client._mqtt_client.on_connect(None,None,None,0)
    assert old_publications == publications
