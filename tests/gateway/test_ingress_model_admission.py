"""R3-I (ruling t_094f82f5): a turn that names no model runs and stamps the OWNING PROFILE's
``model.default``, resolved from the same config its route comes from — never a custom provider
entry's declared ``default_model`` — and route and model are one PAIR.

Measured defect (2026-11-24 fleet ``pong`` sweep): a model-less turn whose route was a custom provider
stamped ``yaan-main`` (``custom_providers[yoyodine]``'s declared default) on five lanes whose own
default is ``deepseek-flash``. ``LocalSessionPolicy.effective()`` handed ``model=None`` to
``route_runtime``, and ``_apply_custom_provider_extras`` filled it from the entry.
"""
import pytest

DEEPSEEK = 'http://127.0.0.1:7778/v1'
LOCAL_7777 = 'http://127.0.0.1:7777/v1'


def _policy(tmp_path, config, **params):
    from gateway.session_policy import build_policy
    launch = {'source': 'cli', 'cwd': str(tmp_path), 'toolsets': [], **params}
    return build_policy(launch, config, profile_terminal=False)


def _route(policy):
    from gateway.session_worker_construct import route_runtime
    frozen, model = policy.effective()
    runtime, _ = route_runtime(policy, frozen, model, None, fallback=False)
    return model, runtime


PROFILE = {'model': {'provider': 'custom:deepseek', 'default': 'deepseek-flash', 'base_url': DEEPSEEK},
           'providers': {'deepseek': {'api': DEEPSEEK, 'default_model': 'deepseek-flash'},
                         'yoyodine': {'api': LOCAL_7777, 'default_model': 'yaan-main'}}}


def test_a_turn_naming_no_model_resolves_the_profiles_default(tmp_path):
    """(a) No model named anywhere: the profile's ``model.default`` is the model, and it is what runs."""
    policy = _policy(tmp_path, PROFILE)
    assert policy.model is None  # the launch named nothing: only the profile can supply the model
    model, runtime = _route(policy)
    assert model == 'deepseek-flash'
    assert runtime['model'] == 'deepseek-flash'


def test_a_model_less_custom_pick_never_takes_the_entrys_default_model(tmp_path):
    """(b) A model-less ``custom:<provider>`` pick runs the PROFILE's model, not the entry's default.

    The entry declares ``default_model: deepseek-v3`` and serves both models, so the pair is coherent
    and the profile's model must be the one that runs.
    """
    config = {'model': {'provider': 'custom:deepseek', 'default': 'deepseek-flash', 'base_url': DEEPSEEK},
              'providers': {'deepseek': {'api': DEEPSEEK, 'default_model': 'deepseek-v3',
                                         'models': {'deepseek-v3': {}, 'deepseek-flash': {}}}}}
    policy = _policy(tmp_path, config, provider='custom:deepseek')
    model, runtime = _route(policy)
    assert (model, runtime['model']) == ('deepseek-flash', 'deepseek-flash')


def test_a_model_less_pick_of_an_entry_serving_another_model_is_refused(tmp_path):
    """(b) The stamped shape: ``custom:yoyodine`` declares only ``yaan-main``, so a model-less turn that
    resolves the profile's ``deepseek-flash`` cannot run there — refused, naming both."""
    from hermes_state_runtime import RuntimeStoreError
    policy = _policy(tmp_path, PROFILE, provider='custom:yoyodine')
    with pytest.raises(RuntimeStoreError) as refused:
        policy.effective()
    assert (refused.value.reason, refused.value.field) == ('invalid_params', 'model')
    assert 'deepseek-flash' in refused.value.detail and 'custom:yoyodine' in refused.value.detail


def test_a_model_less_base_url_pick_is_judged_against_the_entry_owning_it(tmp_path):
    """(b) The false-clean door: ``--base-url`` 7777 with no model used to land ``deepseek-flash`` on
    the ``custom:yoyodine`` endpoint (the weights served were ``yaan-main``'s) — now refused."""
    from hermes_state_runtime import RuntimeStoreError
    policy = _policy(tmp_path, PROFILE, base_url=LOCAL_7777)
    with pytest.raises(RuntimeStoreError) as refused:
        policy.effective()
    assert (refused.value.reason, refused.value.field) == ('invalid_params', 'model')
    assert 'deepseek-flash' in refused.value.detail and 'custom:yoyodine' in refused.value.detail


def test_a_model_less_base_url_pick_on_the_profiles_own_entry_still_runs(tmp_path):
    """The pair rule refuses only a PROVEN conflict: the same door on the profile's own endpoint runs."""
    policy = _policy(tmp_path, PROFILE, base_url=DEEPSEEK)
    model, runtime = _route(policy)
    assert (model, runtime['model'], runtime['base_url'].rstrip('/')) == ('deepseek-flash', 'deepseek-flash', DEEPSEEK)


def test_a_model_less_pick_of_an_entry_declaring_nothing_is_not_judged(tmp_path):
    """An entry that declares no models is a generic endpoint: the pair cannot be proven incoherent."""
    config = {'model': {'provider': 'custom:deepseek', 'default': 'deepseek-flash', 'base_url': DEEPSEEK},
              'providers': {'deepseek': {'api': DEEPSEEK, 'default_model': 'deepseek-flash'},
                            'generic': {'api': LOCAL_7777}}}
    policy = _policy(tmp_path, config, provider='custom:generic')
    model, runtime = _route(policy)
    assert (model, runtime['model'], runtime['base_url'].rstrip('/')) == ('deepseek-flash', 'deepseek-flash', LOCAL_7777)


def test_a_following_session_still_runs_the_live_profiles_model(tmp_path, monkeypatch):
    """(c) Unchanged: a following session reads the profile's CURRENT model, route and model together."""
    from gateway.session_policy import LocalSessionPolicy
    policy = _policy(tmp_path, PROFILE, follow_profile_config=True)
    live = {'model': {'provider': 'custom:deepseek', 'default': 'deepseek-v3', 'base_url': DEEPSEEK},
            'providers': PROFILE['providers']}
    monkeypatch.setattr(LocalSessionPolicy, 'read_profile', lambda self, authority, for_turn=False: live)
    config, model = policy.effective()
    assert (model, config['model']['default'], config['model']['provider']) == (
        'deepseek-v3', 'deepseek-v3', 'custom:deepseek')


def test_a_launch_that_names_a_model_is_never_re_judged(tmp_path):
    """(c) Unchanged: an explicit model pick is the operator's pair, even on another entry's endpoint."""
    config = {'model': {'provider': 'custom:deepseek', 'default': 'deepseek-flash', 'base_url': DEEPSEEK},
              'providers': {'deepseek': {'api': DEEPSEEK, 'default_model': 'deepseek-flash'},
                            'yoyodine': {'api': LOCAL_7777, 'default_model': 'yaan-main'}}}
    policy = _policy(tmp_path, config, provider='custom:yoyodine', model='yaan-main')
    model, runtime = _route(policy)
    assert (model, runtime['model'], runtime['base_url'].rstrip('/')) == ('yaan-main', 'yaan-main', LOCAL_7777)
