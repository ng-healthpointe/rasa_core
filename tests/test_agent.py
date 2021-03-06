import asyncio
from typing import Text

import pytest
from async_generator import async_generator, yield_
from sanic import Sanic, response

import rasa_core
from rasa_core import jobs, utils
from rasa_core.agent import Agent
from rasa_core.interpreter import INTENT_MESSAGE_PREFIX
from rasa_core.policies.memoization import AugmentedMemoizationPolicy
from rasa_core.utils import EndpointConfig


@pytest.fixture(scope="session")
def loop():
    from pytest_sanic.plugin import loop as sanic_loop
    return utils.enable_async_loop_debugging(next(sanic_loop()))


def model_server_app(model_path: Text, model_hash: Text = "somehash"):
    app = Sanic(__name__)
    app.number_of_model_requests = 0

    @app.route("/model", methods=['GET'])
    async def model(request):
        """Simple HTTP model server responding with a trained model."""

        if model_hash == request.headers.get("If-None-Match"):
            return response.text("", 204)

        app.number_of_model_requests += 1

        return await response.file_stream(
            location=model_path,
            headers={'ETag': model_hash,
                     'filename': model_path},
            mime_type='application/zip')

    return app


@pytest.fixture
@async_generator
async def model_server(test_server, zipped_moodbot_model):
    server = await test_server(model_server_app(zipped_moodbot_model,
                               model_hash="somehash"))
    await yield_(server)  # python 3.5 compatibility
    await server.close()


async def test_agent_train(tmpdir, default_domain):
    training_data_file = 'examples/moodbot/data/stories.md'
    agent = Agent("examples/moodbot/domain.yml",
                  policies=[AugmentedMemoizationPolicy()])

    training_data = await agent.load_data(training_data_file)
    agent.train(training_data)
    agent.persist(tmpdir.strpath)

    loaded = Agent.load(tmpdir.strpath)

    # test domain
    assert loaded.domain.action_names == agent.domain.action_names
    assert loaded.domain.intents == agent.domain.intents
    assert loaded.domain.entities == agent.domain.entities
    assert loaded.domain.templates == agent.domain.templates
    assert [s.name for s in loaded.domain.slots] == \
           [s.name for s in agent.domain.slots]

    # test policies
    assert type(loaded.policy_ensemble) is type(
        agent.policy_ensemble)  # nopep8
    assert [type(p) for p in loaded.policy_ensemble.policies] == \
           [type(p) for p in agent.policy_ensemble.policies]


async def test_agent_handle_message(default_agent):
    message = INTENT_MESSAGE_PREFIX + 'greet{"name":"Rasa"}'
    result = await default_agent.handle_message(
        message,
        sender_id="test_agent_handle_message")
    assert result == [{'recipient_id': 'test_agent_handle_message',
                       'text': 'hey there Rasa!'}]


def test_agent_wrong_use_of_load(tmpdir, default_domain):
    training_data_file = 'examples/moodbot/data/stories.md'
    agent = Agent("examples/moodbot/domain.yml",
                  policies=[AugmentedMemoizationPolicy()])

    with pytest.raises(ValueError):
        # try to load a model file from a data path, which is nonsense and
        # should fail properly
        agent.load(training_data_file)


async def test_agent_with_model_server_in_thread(model_server, tmpdir,
                                                 zipped_moodbot_model,
                                                 moodbot_domain,
                                                 moodbot_metadata):
    model_endpoint_config = EndpointConfig.from_dict({
        "url": model_server.make_url('/model'),
        "wait_time_between_pulls": 2
    })

    agent = Agent()
    agent = await rasa_core.agent.load_from_server(
        agent, model_server=model_endpoint_config)

    await asyncio.sleep(3)

    assert agent.fingerprint == "somehash"

    assert agent.domain.as_dict() == moodbot_domain.as_dict()

    agent_policies = {utils.module_path_from_instance(p)
                      for p in agent.policy_ensemble.policies}
    moodbot_policies = set(moodbot_metadata["policy_names"])
    assert agent_policies == moodbot_policies
    assert model_server.app.number_of_model_requests == 1
    jobs.kill_scheduler()


async def test_wait_time_between_pulls_without_interval(model_server,
                                                        monkeypatch):

    monkeypatch.setattr("rasa_core.agent.schedule_model_pulling",
                        lambda *args: 1 / 0)   # will raise an exception

    model_endpoint_config = EndpointConfig.from_dict({
        "url": model_server.make_url('/model'),
        "wait_time_between_pulls": None
    })

    agent = Agent()
    # schould not call schedule_model_pulling, if it does, this will raise
    await rasa_core.agent.load_from_server(agent,
                                           model_server=model_endpoint_config)
    jobs.kill_scheduler()
