import asyncio
import logging
import multiprocessing
import os
import sys
from multiprocessing import Event, Process
import pytest
import uvicorn
from fastapi_websocket_pubsub import PubSubClient

# Add parent path to use local src as package for tests
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir, os.path.pardir))
sys.path.append(root_dir)

from opal_client import config
from opal_client.data.rpc import TenantAwareRpcEventClientMethods
from opal_client.data.updater import DataSourceEntry, DataUpdate, DataUpdater
from opal_client.policy_store import (PolicyStoreClientFactory,
                                      PolicyStoreTypes)
from opal_common.schemas.data import DataSourceConfig
from opal_common.utils import get_authorization_header
from opal_server.config import DATA_CONFIG_ROUTE
from opal_server.server import OpalServer



PORT = int(os.environ.get("PORT") or "9123")
UPDATES_URL = f"ws://localhost:{PORT}/ws"
DATA_ROUTE = "/fetchable_data"
DATA_URL = f"http://localhost:{PORT}{DATA_ROUTE}"
DATA_CONFIG_URL = f"http://localhost:{PORT}{DATA_CONFIG_ROUTE}"
DATA_TOPICS = ["policy_data"]
TEST_DATA = {
    "hello": "world"
}

DATA_SOURCES_CONFIG = DataSourceConfig(entries=[
    {"url": DATA_URL, "topics": DATA_TOPICS}
])


def setup_server(event):
    # Server without git watcher and with a test specifc url for data, and without broadcasting
    server = OpalServer(init_git_watcher=False, data_sources_config=DATA_SOURCES_CONFIG, broadcaster_uri=None)
    server_app = server.app

    # add a url to fetch data from
    @server_app.get(DATA_ROUTE)
    def fetchable_data():
        return TEST_DATA

    @server_app.on_event("startup")
    async def startup_event():
        await asyncio.sleep(0.4)
        # signal the server is ready
        event.set()

    uvicorn.run(server_app, port=PORT)


@pytest.fixture(scope="module")
def server():
    event = Event()
    # Run the server as a separate process
    proc = Process(target=setup_server, args=(event,), daemon=True)
    proc.start()
    yield event
    proc.kill()  # Cleanup after test


def trigger_update():
    async def run():
        # trigger an update
        entries = [DataSourceEntry(url=DATA_URL)]
        update = DataUpdate(reason="Test", entries=entries)
        async with PubSubClient(
            server_uri=UPDATES_URL,
            methods_class=TenantAwareRpcEventClientMethods,
            extra_headers=[get_authorization_header(config.CLIENT_TOKEN)]
        ) as client:
            # Channel must be ready before we can publish on it
            await asyncio.wait_for(client.wait_until_ready(), 5)
            logging.info("Publishing data event")
            await client.publish(DATA_TOPICS, data=update)
    asyncio.run(run())


@pytest.mark.asyncio
async def test_data_updater(server):
    """
    Disable auto-update on connect (fetch_on_connect=False)
    Connect to OPAL-server trigger a Data-update and check our policy store gets the update
    """
    # Wait for the server to start
    server.wait(5)
    # config to use mock OPA
    policy_store = PolicyStoreClientFactory.create(store_type=PolicyStoreTypes.MOCK)
    updater = DataUpdater(pubsub_url=UPDATES_URL, policy_store=policy_store,
                          fetch_on_connect=False, data_topics=DATA_TOPICS)
    # start the updater (terminate on exit)
    await updater.start()
    try:
        proc = multiprocessing.Process(target=trigger_update, daemon=True)
        proc.start()
        # wait until new data arrives into the strore via the updater
        await asyncio.wait_for(policy_store.wait_for_data(), 5)
    # cleanup
    finally:
        await updater.stop()
        proc.terminate()


@pytest.mark.asyncio
async def test_client_get_initial_data(server):
    """
    Connect to OPAL-server and make sure data is fetched on-connect
    """
    # Wait for the server to start
    server.wait(5)
    # config to use mock OPA
    policy_store = PolicyStoreClientFactory.create(store_type=PolicyStoreTypes.MOCK)
    updater = DataUpdater(pubsub_url=UPDATES_URL, data_sources_config_url=DATA_CONFIG_URL,
                          policy_store=policy_store, fetch_on_connect=True, data_topics=DATA_TOPICS)
    # start the updater (terminate on exit)
    await updater.start()
    try:
        # wait until new data arrives into the strore via the updater
        await asyncio.wait_for(policy_store.wait_for_data(), 5)
    # cleanup
    finally:
        await updater.stop()